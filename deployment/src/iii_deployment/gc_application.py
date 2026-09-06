"""Host-native, offline, transactional GC and QGroundControl application slots."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from shutil import copyfile, disk_usage, rmtree
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .bundle import (
    COMPONENT_FILES,
    VerifiedBundle,
    extract_bundle,
    load_bundle_limits,
    verify_bundle,
)
from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .qgc_configuration import QGCConfigurationStore

STATE_SCHEMA = "iii.gc-application-state/v1"
JOURNAL_SCHEMA = "iii.gc-application-journal/v1"
AUDIT_SCHEMA = "iii.gc-application-audit/v1"
OVERRIDE_SCHEMA = "iii.gc-maintenance-override/v1"
HASH = __import__("re").compile(r"^[a-f0-9]{64}$")


class GCApplicationError(ContractError):
    code = "III_GC_APPLICATION_REJECTED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GCApplicationError(f"{label} is missing or unsafe")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GCApplicationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise GCApplicationError(f"{label} is not canonical JSON")
    return value


def _atomic_bytes(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise GCApplicationError(f"managed path is linked: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.partial-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _atomic_document(
    path: Path, value: Mapping[str, Any], *, mode: int = 0o600
) -> None:
    _atomic_bytes(path, canonical_json(value) + b"\n", mode=mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _initial_state() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "state_id": "0" * 64,
        "generation": 0,
        "active_release_id": None,
        "previous_release_id": None,
        "staged_release_id": None,
        "qualified_anchor_release_id": None,
        "active_qgc_sha256": None,
        "previous_qgc_sha256": None,
        "releases": {},
    }
    value["state_id"] = content_identity(
        {key: item for key, item in value.items() if key != "state_id"}
    )
    return value


def _version_tuple(value: str) -> tuple[int, ...]:
    stripped = value.removeprefix("v")
    try:
        result = tuple(int(item) for item in stripped.split("."))
    except ValueError as exc:
        raise GCApplicationError(
            f"unsupported compatibility version: {value!r}"
        ) from exc
    if not result or any(item < 0 for item in result):
        raise GCApplicationError(f"unsupported compatibility version: {value!r}")
    return result


def _range_bounds(value: str) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None]:
    lower = None
    upper = None
    for clause in value.split(","):
        clause = clause.strip()
        if clause.startswith(">="):
            lower = max(lower or (), _version_tuple(clause[2:]))
        elif clause.startswith("<") and not clause.startswith("<="):
            upper = min(upper or (10**9,), _version_tuple(clause[1:]))
        elif clause:
            if clause.startswith((">", "<=")):
                raise GCApplicationError(
                    f"unsupported compatibility range clause: {clause!r}"
                )
            parsed = _version_tuple(clause)
            lower = max(lower or (), parsed)
            upper = min(upper or (10**9,), parsed[:-1] + (parsed[-1] + 1,))
    return lower, upper


def compatibility_overlaps(first: str, second: str) -> bool:
    first_lower, first_upper = _range_bounds(first)
    second_lower, second_upper = _range_bounds(second)
    lower = max(first_lower or (), second_lower or ())
    upper_candidates = [
        item for item in (first_upper, second_upper) if item is not None
    ]
    return not upper_candidates or lower < min(upper_candidates)


def application_pair_compatible(
    gc_manifest: Mapping[str, Any], drone_manifest: Mapping[str, Any]
) -> bool:
    try:
        gc_compatibility = gc_manifest["compatibility"]
        drone_compatibility = drone_manifest["compatibility"]
        for group in ("api_ranges", "schema_ranges"):
            common = set(gc_compatibility[group])
            if (
                common != set(drone_compatibility[group])
                or not common
                or any(
                    not compatibility_overlaps(
                        gc_compatibility[group][name], drone_compatibility[group][name]
                    )
                    for name in common
                )
            ):
                return False
        selected_qgc = gc_manifest["qgc"].get("selected_version")
        return selected_qgc in gc_manifest["qgc"]["compatible_versions"]
    except (KeyError, TypeError, GCApplicationError):
        return False


class GCApplicationStore:
    """Own installed GC/QGC slots, selectors, cache, and recovery journal."""

    def __init__(
        self,
        *,
        application_root: Path,
        state_root: Path,
        cache_root: Path,
        policy_path: Path,
        schema_root: Path,
        trusted_signers: Path,
        operational_policy_path: Path,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        health_opener: Callable[..., Any] = urlopen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        disk_usage_provider: Callable[[Path], Any] = disk_usage,
        now: Callable[[], datetime] | None = None,
        failpoint: Callable[[str], None] | None = None,
        qgc_settings_path: Path | None = None,
        qgc_configuration_state_root: Path | None = None,
        create_roots: bool = True,
    ) -> None:
        for path in (application_root, state_root, cache_root):
            if path.is_symlink():
                raise GCApplicationError(f"GC application root is linked: {path}")
            if create_roots:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif not path.is_dir():
                raise GCApplicationError(f"GC application root is missing: {path}")
        self.application_root = application_root.resolve()
        self.state_root = state_root.resolve()
        self.cache_root = cache_root.resolve()
        self.releases_root = self.application_root / "releases"
        self.qgc_slots_root = self.application_root / "qgc/slots"
        self.control_root = self.state_root / "control"
        for path in (self.releases_root, self.qgc_slots_root):
            if create_roots:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif not path.is_dir() or path.is_symlink():
                raise GCApplicationError(f"GC application boundary is missing: {path}")
        # This is the sole host path mounted into the unprivileged, read-only
        # browser proxy.  It contains only the integrity-protected maintenance
        # marker, never credentials or application state.  The proxy must be
        # able to read it in order to fail closed during an update.
        if self.control_root.is_symlink():
            raise GCApplicationError(
                f"GC application boundary is linked: {self.control_root}"
            )
        if create_roots:
            self.control_root.mkdir(parents=True, exist_ok=True, mode=0o755)
            self.control_root.chmod(0o755)
        elif not self.control_root.is_dir():
            raise GCApplicationError(
                f"GC application boundary is missing: {self.control_root}"
            )
        self.state_path = self.state_root / "application-state.json"
        self.journal_path = self.state_root / "application-journal.json"
        self.lock_path = self.state_root / "application.lock"
        self.audit_path = self.state_root / "application-audit.jsonl"
        self.qgc_settings_path = (
            (qgc_settings_path or self.state_root / "qgc-user/QGroundControl.ini")
            .expanduser()
            .absolute()
        )
        self.qgc_configuration_state_root = (
            (qgc_configuration_state_root or self.state_root / "qgc-configuration")
            .expanduser()
            .absolute()
        )
        self.registry = ContractRegistry(schema_root)
        if policy_path.is_symlink() or not policy_path.is_file():
            raise GCApplicationError("GC application policy is missing or unsafe")
        try:
            self.policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GCApplicationError(
                f"cannot read GC application policy: {exc}"
            ) from exc
        self.registry.validate("gc-application-policy", self.policy)
        self.trusted_signers = trusted_signers
        self.bundle_limits = load_bundle_limits(operational_policy_path)
        self.runner = runner
        self.health_opener = health_opener
        self.monotonic = monotonic
        self.sleep = sleep
        self.disk_usage_provider = disk_usage_provider
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.failpoint = failpoint or (lambda _phase: None)
        self._validate_state(self._load_state())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(
            self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _validate_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        self.registry.validate("gc-application-state", state)
        expected = content_identity(
            {key: item for key, item in state.items() if key != "state_id"}
        )
        if state["state_id"] != expected:
            raise GCApplicationError("GC application state identity mismatch")
        releases = state["releases"]
        pointers = (
            state["active_release_id"],
            state["previous_release_id"],
            state["staged_release_id"],
            state["qualified_anchor_release_id"],
        )
        missing = sorted(
            {item for item in pointers if item is not None} - set(releases)
        )
        if missing:
            raise GCApplicationError(
                "GC application state references missing releases: "
                + ", ".join(missing)
            )
        qgc = {item["qgroundcontrol"]["sha256"] for item in releases.values()}
        for release_id, release in releases.items():
            if (
                release["release_id"] != release_id
                or release["slot"] != f"releases/{release_id}"
                or release["qgroundcontrol"]["slot"]
                != f"qgc/slots/{release['qgroundcontrol']['sha256']}"
            ):
                raise GCApplicationError(
                    "GC application release state is not self-consistent"
                )
            images = release["images"]
            if {image["name"] for image in images} != {"frontend", "proxy"} or any(
                image["tag"] != f"iii-drone-gc-{image['name']}:{release_id}"
                for image in images
            ):
                raise GCApplicationError(
                    "GC application image state is not self-consistent"
                )
        for selected in (state["active_qgc_sha256"], state["previous_qgc_sha256"]):
            if selected is not None and selected not in qgc:
                raise GCApplicationError(
                    "GC application state references a missing QGC slot"
                )
        return dict(state)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return _initial_state()
        return self._validate_state(
            _canonical(self.state_path, label="GC application state")
        )

    def _commit_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state["generation"] += 1
        state["state_id"] = content_identity(
            {key: item for key, item in state.items() if key != "state_id"}
        )
        self._validate_state(state)
        _atomic_document(self.state_path, state)
        return state

    def state(self) -> dict[str, Any]:
        with self._locked():
            return self._load_state()

    def _run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = self.runner(
            list(argv),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise GCApplicationError(
                f"GC application command failed ({argv[0]}): {detail}"
            )
        return result

    def _journal(self, value: Mapping[str, Any] | None) -> None:
        if value is None:
            if self.journal_path.exists() and not self.journal_path.is_symlink():
                self.journal_path.unlink()
                _fsync_directory(self.journal_path.parent)
            return
        self.registry.validate("gc-application-journal", value)
        self._validate_state(value["previous_state"])
        _atomic_document(self.journal_path, value)

    def _transition(self, journal: dict[str, Any], phase: str) -> dict[str, Any]:
        journal = {**journal, "phase": phase, "updated_at": self.now().isoformat()}
        self._journal(journal)
        self.failpoint(phase)
        return journal

    def _audit(self, event: str, outcome: str, **fields: Any) -> dict[str, Any]:
        previous = None
        if self.audit_path.exists() and not self.audit_path.is_symlink():
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
            if lines:
                previous = json.loads(lines[-1])["audit_id"]
        value = {
            "schema": AUDIT_SCHEMA,
            "audit_id": "0" * 64,
            "event": event,
            "outcome": outcome,
            "at": self.now().isoformat(),
            "previous_audit_id": previous,
            **fields,
        }
        value["audit_id"] = content_identity(
            {key: item for key, item in value.items() if key != "audit_id"}
        )
        descriptor = os.open(
            self.audit_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, canonical_json(value) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return value

    def _set_drain(self, *, operation_id: str, enabled: bool) -> None:
        path = self.control_root / "drain.json"
        if enabled:
            value = {
                "schema": "iii.gc-browser-drain/v1",
                "operation_id": operation_id,
                "enabled": True,
            }
            value["drain_id"] = content_identity(value)
            _atomic_document(path, value, mode=0o644)
        elif path.exists() and not path.is_symlink():
            path.unlink()
            _fsync_directory(path.parent)

    def _override(
        self, *, operation_id: str, reason: str | None, confirmation: str | None
    ) -> dict[str, Any] | None:
        if reason is None and confirmation is None:
            return None
        warning = self.policy["safety"]["override_warning"]
        if not reason or len(reason.strip()) < 12 or confirmation != warning:
            raise GCApplicationError(
                "maintenance override requires a reason and the exact loss-of-operator-surface warning"
            )
        value = {
            "schema": OVERRIDE_SCHEMA,
            "override_id": "0" * 64,
            "operation_id": operation_id,
            "reason": reason.strip(),
            "warning": warning,
            "confirmed": True,
            "at": self.now().isoformat(),
        }
        value["override_id"] = content_identity(
            {key: item for key, item in value.items() if key != "override_id"}
        )
        return value

    def _safety_gate(
        self, safety: Mapping[str, Any], override: Mapping[str, Any] | None
    ) -> None:
        if safety.get("connected") is False or safety.get("profile") in {"sim", "hil"}:
            if override is not None:
                raise GCApplicationError(
                    "maintenance override is invalid when the normal gate passes"
                )
            return
        known_active = []
        for field in (
            "armed",
            "in_air",
            "mission_active",
            "mission_control_owner",
            "custom_operation_active",
            "custom_operation_control_owner",
            "direct_operation_active",
            "reference_owner_active",
        ):
            if safety.get(field) is True:
                known_active.append(field)
        if known_active:
            raise GCApplicationError(
                "connected real target is not maintenance-safe: "
                + ", ".join(known_active)
            )
        safe = (
            safety.get("connected") is True
            and safety.get("profile") in {"real", "opti_track"}
            and safety.get("runtime_api_available") is True
            and safety.get("runtime_identity_matches") is True
            and safety.get("runtime_fresh") is True
            and safety.get("px4_available") is True
            and safety.get("px4_fresh") is True
            and safety.get("armed") is False
            and safety.get("in_air") is False
            and safety.get("mission_fresh") is True
            and safety.get("mission_active") is False
            and safety.get("operation_fresh") is True
            and safety.get("custom_operation_active") is False
            and safety.get("direct_operation_active") is False
            and safety.get("reference_owner_active") is False
            and safety.get("continuously_safe_for_s", 0)
            >= self.policy["safety"]["real_safe_continuity_seconds"]
        )
        if safe:
            if override is not None:
                raise GCApplicationError(
                    "maintenance override is invalid when the normal gate passes"
                )
            return
        if override is None:
            raise GCApplicationError(
                "connected real target lacks fresh landed/disarmed owner-free maintenance evidence"
            )

    def _bundle_bytes(self, component: Path) -> int:
        return sum((component / name).stat().st_size for name in COMPONENT_FILES)

    def _cache_entries(self) -> list[tuple[Path, dict[str, Any]]]:
        root = self.cache_root / "artifacts"
        index = self.cache_root / "artifact-index"
        if root.is_symlink():
            raise GCApplicationError("GC artifact cache root is linked")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if index.is_symlink():
            raise GCApplicationError("GC artifact cache index is linked")
        index.mkdir(parents=True, exist_ok=True, mode=0o700)
        entries = []
        indexed = set()
        for metadata in sorted(index.glob("*.json")):
            value = _canonical(metadata, label="GC cache entry")
            required = {
                "schema",
                "archive_sha256",
                "bytes",
                "last_used_at",
                "protected_domains",
            }
            try:
                datetime.fromisoformat(value.get("last_used_at", ""))
            except (TypeError, ValueError) as exc:
                raise GCApplicationError("GC cache entry timestamp is invalid") from exc
            domains = value.get("protected_domains")
            allowed_domains = set(self.policy["cache"]["protected_domains"])
            if (
                set(value) != required
                or value.get("schema") != "iii.gc-artifact-cache-entry/v1"
                or not isinstance(value.get("bytes"), int)
                or isinstance(value.get("bytes"), bool)
                or value["bytes"] < 1
                or not isinstance(domains, list)
                or len(domains) != len(set(domains))
                or not set(domains).issubset(allowed_domains)
            ):
                raise GCApplicationError("GC cache entry contract is invalid")
            path = root / value["archive_sha256"]
            if (
                not HASH.fullmatch(value["archive_sha256"])
                or metadata.name != value["archive_sha256"] + ".json"
                or path.is_symlink()
                or not path.is_dir()
            ):
                raise GCApplicationError(f"unsafe GC cache entry: {path}")
            indexed.add(path.name)
            entries.append((path, value))
        unindexed = {path.name for path in root.iterdir()} - indexed
        if unindexed:
            raise GCApplicationError(
                "GC artifact cache contains unindexed entries: "
                + ", ".join(sorted(unindexed))
            )
        return entries

    def prune_cache(self, *, incoming_bytes: int = 0) -> dict[str, Any]:
        quota = self.policy["cache"]["non_protected_quota_bytes"]
        usage = self.disk_usage_provider(self.cache_root)
        # The reserve protects the managed GC application store, not unrelated
        # research data elsewhere on a large operator workstation.  Scale for
        # small disks, but cap the percentage reserve at the policy's explicit
        # GC-storage maximum.
        reserve = max(
            self.policy["cache"]["minimum_free_bytes"],
            min(
                int(usage.total * self.policy["cache"]["minimum_free_fraction"]),
                self.policy["cache"]["maximum_free_bytes"],
            ),
        )
        entries = self._cache_entries()
        removable = sorted(
            (
                (path, value)
                for path, value in entries
                if not value.get("protected_domains")
            ),
            key=lambda item: (item[1]["last_used_at"], item[0].name),
        )
        non_protected = sum(
            value["bytes"] for _, value in entries if not value.get("protected_domains")
        )
        free = usage.free
        removed = []
        while removable and (
            non_protected + incoming_bytes > quota or free < reserve + incoming_bytes
        ):
            path, value = removable.pop(0)
            rmtree(path)
            (
                self.cache_root / "artifact-index" / f"{value['archive_sha256']}.json"
            ).unlink()
            removed.append(value["archive_sha256"])
            non_protected -= value["bytes"]
            free += value["bytes"]
        if non_protected + incoming_bytes > quota or free < reserve + incoming_bytes:
            raise GCApplicationError(
                "GC artifact cache cannot preserve its 50 GiB quota and free-space reserve"
            )
        return {
            "removed": removed,
            "non_protected_bytes": non_protected,
            "required_free_bytes": reserve,
        }

    def _cache_bundle(self, component: Path, archive_sha256: str) -> Path:
        destination = self.cache_root / "artifacts" / archive_sha256
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise GCApplicationError("existing GC cache entry is unsafe")
            index_path = self.cache_root / "artifact-index" / f"{archive_sha256}.json"
            if not index_path.exists() and not index_path.is_symlink():
                total = 0
                for name in COMPONENT_FILES:
                    cached = destination / name
                    source = component / name
                    if (
                        cached.is_symlink()
                        or not cached.is_file()
                        or _sha256(cached) != _sha256(source)
                    ):
                        raise GCApplicationError(
                            "interrupted GC cache entry differs from bundle"
                        )
                    total += cached.stat().st_size
                entry = {
                    "schema": "iii.gc-artifact-cache-entry/v1",
                    "archive_sha256": archive_sha256,
                    "bytes": total,
                    "last_used_at": self.now().isoformat(),
                    "protected_domains": [],
                }
            else:
                entry = _canonical(index_path, label="GC cache entry")
            if entry["archive_sha256"] != archive_sha256:
                raise GCApplicationError("existing GC cache entry has another identity")
            _atomic_document(
                index_path,
                {**entry, "last_used_at": self.now().isoformat()},
            )
            return destination
        incoming = self._bundle_bytes(component)
        self.prune_cache(incoming_bytes=incoming)
        partial = destination.parent / f".{archive_sha256}.partial-{os.getpid()}"
        partial.mkdir(mode=0o700)
        try:
            total = 0
            for name in COMPONENT_FILES:
                source = component / name
                target = partial / name
                copyfile(source, target, follow_symlinks=False)
                if _sha256(target) != _sha256(source):
                    raise GCApplicationError("GC cache copy changed bundle bytes")
                total += target.stat().st_size
            value = {
                "schema": "iii.gc-artifact-cache-entry/v1",
                "archive_sha256": archive_sha256,
                "bytes": total,
                "last_used_at": self.now().isoformat(),
                "protected_domains": [],
            }
            os.replace(partial, destination)
            _fsync_directory(destination.parent)
            _atomic_document(
                self.cache_root / "artifact-index" / f"{archive_sha256}.json",
                value,
            )
        finally:
            if partial.exists() and not partial.is_symlink():
                rmtree(partial)
        return destination

    def _protect_offline(self, archive_sha256: str) -> None:
        path = self.cache_root / "artifact-index" / f"{archive_sha256}.json"
        entry = _canonical(path, label="GC cache entry")
        if entry["archive_sha256"] != archive_sha256:
            raise GCApplicationError("GC cache protection identity differs")
        domains = sorted(set(entry.get("protected_domains", [])) | {"offline"})
        if entry.get("protected_domains") != domains:
            _atomic_document(path, {**entry, "protected_domains": domains})

    def _sync_cache_protection(self, state: Mapping[str, Any]) -> None:
        domains_by_release = (
            (state["qualified_anchor_release_id"], "qualified-anchor"),
            (state["active_release_id"], "active"),
            (state["previous_release_id"], "previous-field"),
            (state["staged_release_id"], "staged-candidate"),
        )
        domains_by_archive: dict[str, set[str]] = {}
        for release_id, domain in domains_by_release:
            if release_id is None:
                continue
            archive = state["releases"][release_id]["archive_sha256"]
            domains_by_archive.setdefault(archive, set()).add(domain)
        for path, entry in self._cache_entries():
            preserved = {
                domain
                for domain in entry.get("protected_domains", [])
                if domain == "offline"
            }
            expected = sorted(
                preserved | domains_by_archive.get(entry["archive_sha256"], set())
            )
            if entry.get("protected_domains") != expected:
                _atomic_document(
                    self.cache_root
                    / "artifact-index"
                    / f"{entry['archive_sha256']}.json",
                    {**entry, "protected_domains": expected},
                )

    def _verify_slot(
        self,
        slot: Path,
        release_id: str,
        verified_bundle: VerifiedBundle,
    ) -> dict[str, Any]:
        manifest = _canonical(
            slot / "META/release-manifest.json", label="GC slot manifest"
        )
        bundle_manifest = _canonical(
            slot / "META/bundle-manifest.json", label="GC slot bundle manifest"
        )
        if (
            manifest != verified_bundle.release_manifest
            or bundle_manifest != verified_bundle.bundle_manifest
            or manifest.get("release_id") != release_id
            or bundle_manifest.get("release_id") != release_id
            or bundle_manifest.get("component") != "gc"
            or "gc" not in manifest.get("components", [])
        ):
            raise GCApplicationError("GC slot release identity/component is invalid")
        self._verify_payload_tree(slot / "payload", bundle_manifest["content"])
        record = _canonical(slot / "payload/build-record.json", label="GC build record")
        self.registry.validate("gc-build-record", record)
        if record["build_id"] != content_identity(
            {key: item for key, item in record.items() if key != "build_id"}
        ):
            raise GCApplicationError("GC build record identity mismatch")
        if {image["name"] for image in record["images"]} != {
            "frontend",
            "proxy",
        } or any(
            image["archive"] != f"{image['name']}.oci" for image in record["images"]
        ):
            raise GCApplicationError(
                "GC build record must contain exact frontend/proxy images"
            )
        qgc = record["qgroundcontrol"]
        qgc_path = slot / "payload/qgc" / qgc["appimage"]
        if (
            qgc_path.is_symlink()
            or not qgc_path.is_file()
            or qgc_path.stat().st_size != qgc["bytes"]
            or _sha256(qgc_path) != qgc["sha256"]
            or qgc["appimage_update_information"] != ""
            or manifest["qgc"].get("selected_version", qgc["version"]) != qgc["version"]
            or qgc["version"] not in manifest["qgc"]["compatible_versions"]
        ):
            raise GCApplicationError(
                "GC slot QGroundControl contract differs from release"
            )
        configuration = qgc["configuration"]
        qgc_policy_path = slot / "payload" / configuration["policy"]
        qgc_baseline_path = slot / "payload" / configuration["baseline"]
        qgc_policy = _canonical(
            qgc_policy_path, label="GC slot QGroundControl key policy"
        )
        qgc_baseline = _canonical(
            qgc_baseline_path, label="GC slot QGroundControl managed settings"
        )
        self.registry.validate("qgc-key-policy", qgc_policy)
        self.registry.validate("qgc-managed-settings", qgc_baseline)
        if (
            _sha256(qgc_policy_path) != configuration["policy_sha256"]
            or _sha256(qgc_baseline_path) != configuration["baseline_sha256"]
            or qgc_policy["policy_id"] != configuration["policy_id"]
            or qgc_policy["policy_id"]
            != content_identity(
                {key: item for key, item in qgc_policy.items() if key != "policy_id"}
            )
            or qgc_baseline["settings_id"] != configuration["settings_id"]
            or qgc_baseline["settings_id"]
            != content_identity(
                {
                    key: item
                    for key, item in qgc_baseline.items()
                    if key != "settings_id"
                }
            )
            or qgc_baseline["policy_id"] != qgc_policy["policy_id"]
            or qgc["version"] not in qgc_policy["qgc_versions"]
            or qgc["version"] not in qgc_baseline["qgc_versions"]
        ):
            raise GCApplicationError(
                "GC slot QGroundControl configuration differs from build record"
            )
        compose = slot / "payload" / record["application"]["compose"]
        if (
            compose.is_symlink()
            or not compose.is_file()
            or _sha256(compose) != record["application"]["compose_sha256"]
        ):
            raise GCApplicationError("GC slot compose contract differs from release")
        return {
            "manifest": manifest,
            "record": record,
            "qgc_path": qgc_path,
            "qgc_policy_path": qgc_policy_path,
            "qgc_baseline_path": qgc_baseline_path,
        }

    def _verify_payload_tree(
        self, payload: Path, content: Sequence[Mapping[str, Any]]
    ) -> None:
        if payload.is_symlink() or not payload.is_dir():
            raise GCApplicationError("GC slot payload root is missing or unsafe")
        expected = {entry["path"]: entry for entry in content}
        if len(expected) != len(content):
            raise GCApplicationError(
                "GC slot payload contract contains duplicate paths"
            )
        actual: dict[str, Path] = {}
        for current, directories, files in os.walk(payload, followlinks=False):
            directories.sort()
            files.sort()
            parent = Path(current)
            for name in (*directories, *files):
                path = parent / name
                relative = "payload/" + path.relative_to(payload).as_posix()
                metadata = path.lstat()
                if not (
                    stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
                ):
                    raise GCApplicationError(
                        "GC slot payload contains a link or special file"
                    )
                actual[relative] = path
        if set(actual) != set(expected):
            raise GCApplicationError(
                "GC slot payload topology differs from signed bundle"
            )
        for relative, entry in expected.items():
            path = actual[relative]
            metadata = path.lstat()
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if (entry["type"] == "directory") != is_directory or stat.S_IMODE(
                metadata.st_mode
            ) != entry["mode"]:
                raise GCApplicationError("GC slot payload type or mode differs")
            if not is_directory and (
                metadata.st_size != entry["size"] or _sha256(path) != entry["sha256"]
            ):
                raise GCApplicationError(
                    "GC slot payload bytes differ from signed bundle"
                )

    def _authenticated_bundle(
        self, archive_sha256: str, release_id: str
    ) -> VerifiedBundle:
        component = self.cache_root / "artifacts" / archive_sha256
        verified = verify_bundle(
            component,
            self.trusted_signers,
            registry=self.registry,
            host_limits=self.bundle_limits,
        )
        if (
            verified.archive_sha256 != archive_sha256
            or verified.release_manifest["release_id"] != release_id
            or verified.bundle_manifest["component"] != "gc"
        ):
            raise GCApplicationError(
                "cached GC bundle identity differs from installed slot"
            )
        return verified

    def _install_qgc_slot(self, source: Path, digest: str) -> Path:
        destination = self.qgc_slots_root / digest
        if destination.exists() or destination.is_symlink():
            target = destination / "QGroundControl.AppImage"
            if (
                destination.is_symlink()
                or not destination.is_dir()
                or _sha256(target) != digest
            ):
                raise GCApplicationError(
                    "existing QGroundControl slot is unsafe or changed"
                )
            return destination
        partial = destination.parent / f".{digest}.partial-{os.getpid()}"
        partial.mkdir(mode=0o700)
        try:
            target = partial / "QGroundControl.AppImage"
            copyfile(source, target, follow_symlinks=False)
            target.chmod(0o555)
            if _sha256(target) != digest:
                raise GCApplicationError("QGroundControl slot copy changed bytes")
            os.replace(partial, destination)
            _fsync_directory(destination.parent)
        finally:
            if partial.exists() and not partial.is_symlink():
                rmtree(partial)
        return destination

    def _import_images(
        self, slot: Path, release_id: str, record: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        imported = []
        for image in sorted(record["images"], key=lambda item: item["name"]):
            archive = slot / "payload/images" / image["archive"]
            if (
                archive.is_symlink()
                or not archive.is_file()
                or _sha256(archive) != image["sha256"]
            ):
                raise GCApplicationError(f"GC {image['name']} OCI archive changed")
            tag = f"iii-drone-gc-{image['name']}:{release_id}"
            # Noble's supported skopeo (1.4) cannot use the modern Docker
            # daemon API transport.  Convert the already hash-verified OCI
            # archive locally, then let Docker's native loader import it.
            # The release archive remains the integrity boundary; the final
            # inspect proves the requested tag was actually installed.
            with tempfile.TemporaryDirectory(
                prefix=f".{image['name']}-docker-archive-", dir=slot
            ) as temporary:
                docker_archive = Path(temporary) / "image.tar"
                self._run(
                    [
                        "skopeo",
                        "copy",
                        f"oci-archive:{archive}",
                        f"docker-archive:{docker_archive}:{tag}",
                    ]
                )
                self._run(["docker", "load", "--input", str(docker_archive)])
            self._run(["docker", "image", "inspect", tag])
            imported.append(
                {
                    "name": image["name"],
                    "tag": tag,
                    "manifest_digest": image["manifest_digest"],
                    "archive_sha256": image["sha256"],
                }
            )
        return imported

    def stage(
        self, component: Path, *, protect_offline: bool = False
    ) -> dict[str, Any]:
        with self._locked():
            self._cleanup_partials()
            verified = verify_bundle(
                component,
                self.trusted_signers,
                registry=self.registry,
                host_limits=self.bundle_limits,
            )
            if verified.bundle_manifest["component"] != "gc":
                raise GCApplicationError("GC application store accepts only GC bundles")
            release_id = verified.release_manifest["release_id"]
            cache = self._cache_bundle(component, verified.archive_sha256)
            if protect_offline:
                self._protect_offline(verified.archive_sha256)
            slot = self.releases_root / release_id
            if not slot.exists() and not slot.is_symlink():
                partial = self.releases_root / f".{release_id}.partial-{os.getpid()}"
                try:
                    extract_bundle(
                        cache,
                        partial,
                        self.trusted_signers,
                        registry=self.registry,
                        host_limits=self.bundle_limits,
                    )
                    os.replace(partial, slot)
                    _fsync_directory(slot.parent)
                finally:
                    if partial.exists() and not partial.is_symlink():
                        rmtree(partial)
            if slot.is_symlink() or not slot.is_dir():
                raise GCApplicationError("GC application slot is unsafe")
            verified_slot = self._verify_slot(slot, release_id, verified)
            record = verified_slot["record"]
            qgc_slot = self._install_qgc_slot(
                verified_slot["qgc_path"], record["qgroundcontrol"]["sha256"]
            )
            images = self._import_images(slot, release_id, record)
            environment = (
                f"III_GC_RELEASE_ID={release_id}\n"
                f"III_GC_CONTROL_DIR={self.control_root}\n"
            ).encode()
            _atomic_bytes(slot / "application.env", environment, mode=0o400)
            state = self._load_state()
            state["releases"][release_id] = {
                "release_id": release_id,
                "release_class": verified.release_manifest["release_class"],
                "version": verified.release_manifest["version"],
                "slot": f"releases/{release_id}",
                "archive_sha256": verified.archive_sha256,
                "compatibility": verified.release_manifest["compatibility"],
                "qgroundcontrol": {
                    "version": record["qgroundcontrol"]["version"],
                    "sha256": record["qgroundcontrol"]["sha256"],
                    "slot": f"qgc/slots/{qgc_slot.name}",
                },
                "images": images,
                "installed_at": self.now().isoformat(),
            }
            state["staged_release_id"] = release_id
            self._commit_state(state)
            self._sync_cache_protection(state)
            self._audit("stage", "success", release_id=release_id)
            return {
                "state": "staged",
                "release_id": release_id,
                "slot": str(slot),
                "qgc_sha256": record["qgroundcontrol"]["sha256"],
                "cache": str(cache),
                "offline_protected": protect_offline,
            }

    def _replace_selector(self, name: str, target: Path | None) -> None:
        selector = self.application_root / name
        if selector.exists() and not selector.is_symlink():
            raise GCApplicationError(f"GC selector is not a symbolic link: {selector}")
        selector.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = selector.parent / f".{selector.name}.partial-{os.getpid()}"
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        if target is None:
            if selector.is_symlink():
                selector.unlink()
                _fsync_directory(selector.parent)
            return
        relative = os.path.relpath(target, selector.parent)
        temporary.symlink_to(relative)
        os.replace(temporary, selector)
        _fsync_directory(selector.parent)

    def _cleanup_partials(self) -> None:
        cache_artifacts = self.cache_root / "artifacts"
        if cache_artifacts.is_symlink():
            raise GCApplicationError("GC artifact cache root is linked")
        cache_artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        roots = (
            self.application_root,
            self.application_root / "qgc",
            self.releases_root,
            self.qgc_slots_root,
            cache_artifacts,
        )
        for root in roots:
            if root.is_symlink() or not root.is_dir():
                raise GCApplicationError(f"GC managed partial root is unsafe: {root}")
            changed = False
            for path in root.iterdir():
                if not path.name.startswith(".") or ".partial-" not in path.name:
                    continue
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    rmtree(path)
                changed = True
            if changed:
                _fsync_directory(root)

    def _service(self, verb: str, unit: str) -> None:
        self._run(["systemctl", "--user", verb, unit])

    def _qgc_running(self) -> bool:
        result = self.runner(
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                self.policy["application"]["qgc_unit"],
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.returncode == 0

    def _qgc_configuration_store(
        self, verified_slot: Mapping[str, Any]
    ) -> QGCConfigurationStore:
        return QGCConfigurationStore(
            settings_path=self.qgc_settings_path,
            state_root=self.qgc_configuration_state_root,
            policy_path=verified_slot["qgc_policy_path"],
            baseline_path=verified_slot["qgc_baseline_path"],
            schema_root=self.registry.schema_root,
            now=lambda: self.now().isoformat(),
        )

    def _verified_release_slot(self, release_id: str) -> dict[str, Any]:
        state = self._load_state()
        candidate = state["releases"].get(release_id)
        if candidate is None:
            raise GCApplicationError(
                "GC recovery candidate is absent from retained state"
            )
        authenticated = self._authenticated_bundle(
            candidate["archive_sha256"], release_id
        )
        return self._verify_slot(
            self.releases_root / release_id, release_id, authenticated
        )

    def _restore_qgc_configuration(
        self, journal: Mapping[str, Any], verified_slot: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        backup_id = journal["qgc_config_backup_id"]
        if backup_id is None:
            return None
        if self._qgc_running():
            self._service("stop", self.policy["application"]["qgc_unit"])
        return self._qgc_configuration_store(verified_slot).restore(backup_id)

    def _health(self) -> None:
        for url in self.policy["application"]["health_urls"]:
            # A systemd target's Wants= units start asynchronously.  Do not
            # undo a valid selector merely because the first TCP probe races
            # their container startup; wait a bounded, deterministic window.
            deadline = self.monotonic() + 20.0
            failure: Exception | None = None
            while self.monotonic() < deadline:
                request = Request(url, headers={"Accept": "application/json"})
                try:
                    with self.health_opener(request, timeout=2) as response:
                        response.read(1024 * 1024 + 1)
                        status_code = getattr(response, "status", 200)
                    if 200 <= status_code < 400:
                        failure = None
                        break
                    failure = GCApplicationError(
                        f"GC application health returned HTTP {status_code}"
                    )
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    failure = exc
                self.sleep(0.25)
            if failure is not None:
                raise GCApplicationError(
                    f"GC application health failed at {url}: {failure}"
                ) from failure

    def _restore_selectors(
        self, previous_release: str | None, previous_qgc: str | None
    ) -> None:
        self._replace_selector(
            "current",
            self.releases_root / previous_release if previous_release else None,
        )
        self._replace_selector(
            "qgc/current",
            self.qgc_slots_root / previous_qgc if previous_qgc else None,
        )

    def activate(
        self,
        release_id: str,
        *,
        operation_id: str,
        safety: Mapping[str, Any],
        override_reason: str | None = None,
        override_confirmation: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            self._reconcile_locked()
            if not HASH.fullmatch(release_id):
                raise GCApplicationError("GC release identity is invalid")
            state = self._load_state()
            candidate = state["releases"].get(release_id)
            if candidate is None or state["staged_release_id"] != release_id:
                raise GCApplicationError("GC release is not the exact staged candidate")
            authenticated = self._authenticated_bundle(
                candidate["archive_sha256"], release_id
            )
            verified_slot = self._verify_slot(
                self.releases_root / release_id, release_id, authenticated
            )
            override = self._override(
                operation_id=operation_id,
                reason=override_reason,
                confirmation=override_confirmation,
            )
            self._set_drain(operation_id=operation_id, enabled=True)
            previous = state["active_release_id"]
            previous_qgc = state["active_qgc_sha256"]
            qgc_was_running = self._qgc_running()
            journal = {
                "schema": JOURNAL_SCHEMA,
                "transaction_id": "0" * 64,
                "operation_id": operation_id,
                "phase": "planned",
                "candidate_release_id": release_id,
                "previous_release_id": previous,
                "previous_qgc_sha256": previous_qgc,
                "qgc_was_running": qgc_was_running,
                "qgc_config_backup_id": None,
                "previous_state": state,
                "override_id": override and override["override_id"],
                "created_at": self.now().isoformat(),
                "updated_at": self.now().isoformat(),
            }
            journal["transaction_id"] = content_identity(
                {
                    key: item
                    for key, item in journal.items()
                    if key not in {"transaction_id", "phase", "updated_at"}
                }
            )
            self._journal(journal)
            try:
                self._safety_gate(safety, override)
                if override is not None:
                    self._audit("maintenance-override", "authorized", override=override)
                imported = self._import_images(
                    self.releases_root / release_id,
                    release_id,
                    verified_slot["record"],
                )
                if imported != candidate["images"]:
                    raise GCApplicationError(
                        "GC runtime image identities differ from staged state"
                    )
                qgc_configuration = self._qgc_configuration_store(verified_slot)
                if qgc_was_running:
                    self._service("stop", self.policy["application"]["qgc_unit"])

                def retain_qgc_backup(backup: Mapping[str, Any]) -> None:
                    nonlocal journal
                    journal = {
                        **journal,
                        "qgc_config_backup_id": backup["backup_id"],
                    }
                    journal = self._transition(journal, "qgc-backed-up")

                qgc_merge = qgc_configuration.apply(
                    qgc_version=verified_slot["record"]["qgroundcontrol"]["version"],
                    release_id=release_id,
                    profile=(
                        "sim"
                        if safety.get("profile") in {"sim", "hil"}
                        else "real"
                    ),
                    qgc_running=False,
                    backup_callback=retain_qgc_backup,
                )
                journal = self._transition(journal, "qgc-configured")
                journal = self._transition(journal, "switching")
                self._replace_selector("current", self.releases_root / release_id)
                qgc_sha = candidate["qgroundcontrol"]["sha256"]
                self._replace_selector("qgc/current", self.qgc_slots_root / qgc_sha)
                journal = self._transition(journal, "selected")
                self._service("restart", self.policy["application"]["gc_unit"])
                if qgc_was_running:
                    self._service("start", self.policy["application"]["qgc_unit"])
                self._health()
                journal = self._transition(journal, "services-started")
                state = self._load_state()
                old = state["active_release_id"]
                if (
                    old
                    and state["releases"][old]["release_class"] == "field-development"
                ):
                    state["previous_release_id"] = old
                state["previous_qgc_sha256"] = state["active_qgc_sha256"]
                state["active_release_id"] = release_id
                state["active_qgc_sha256"] = qgc_sha
                state["staged_release_id"] = None
                if candidate["release_class"] == "qualified":
                    state["qualified_anchor_release_id"] = release_id
                self._commit_state(state)
                self._sync_cache_protection(state)
                journal = self._transition(journal, "committed")
                self._journal(None)
                self._set_drain(operation_id=operation_id, enabled=False)
                self._audit(
                    "activate",
                    "success",
                    operation_id=operation_id,
                    release_id=release_id,
                    previous_release_id=previous,
                    override_id=override and override["override_id"],
                    qgc_settings_id=qgc_merge["settings_id"],
                    qgc_backup_id=qgc_merge["backup_id"],
                )
                try:
                    cleanup = self._garbage_collect_locked()
                except Exception as cleanup_error:
                    cleanup = {"state": "deferred", "error": str(cleanup_error)}
                    self._audit(
                        "garbage-collect",
                        "deferred",
                        operation_id=operation_id,
                        release_id=release_id,
                        error=str(cleanup_error),
                    )
                return {
                    "state": "active",
                    "release_id": release_id,
                    "previous_release_id": previous,
                    "qgc_sha256": qgc_sha,
                    "transaction_id": journal["transaction_id"],
                    "override_id": override and override["override_id"],
                    "qgc_configuration": qgc_merge,
                    "cleanup": cleanup,
                }
            except Exception as exc:
                try:
                    self._restore_qgc_configuration(journal, verified_slot)
                    self._restore_selectors(previous, previous_qgc)
                    current = self._load_state()
                    if current["state_id"] != state["state_id"]:
                        restored = json.loads(json.dumps(state))
                        restored["generation"] = max(
                            int(restored["generation"]), int(current["generation"])
                        )
                        self._commit_state(restored)
                        self._sync_cache_protection(restored)
                    if previous is not None:
                        self._service("restart", self.policy["application"]["gc_unit"])
                    else:
                        # No prior selector exists.  Stop the candidate units
                        # before removing their ConditionPathExists input so a
                        # failed first activation cannot leave a proxy running
                        # against a now-unselected release.
                        self._service("stop", self.policy["application"]["gc_unit"])
                    if qgc_was_running:
                        self._service("start", self.policy["application"]["qgc_unit"])
                    journal = self._transition(journal, "rolled-back")
                    self._journal(None)
                    self._set_drain(operation_id=operation_id, enabled=False)
                except Exception:
                    # The durable journal deliberately remains for login/reboot recovery.
                    pass
                self._audit(
                    "activate",
                    "failed",
                    operation_id=operation_id,
                    release_id=release_id,
                    error=str(exc),
                )
                raise

    def _reconcile_locked(self) -> dict[str, Any]:
        self._cleanup_partials()
        if not self.journal_path.exists() and not self.journal_path.is_symlink():
            return {"state": "clean"}
        journal = _canonical(self.journal_path, label="GC application journal")
        self.registry.validate("gc-application-journal", journal)
        self._validate_state(journal["previous_state"])
        if journal["phase"] in {"committed", "rolled-back"}:
            self._journal(None)
            self._set_drain(operation_id=journal["operation_id"], enabled=False)
            return {"state": "cleaned", "transaction_id": journal["transaction_id"]}
        verified_slot = self._verified_release_slot(journal["candidate_release_id"])
        self._restore_qgc_configuration(journal, verified_slot)
        self._restore_selectors(
            journal["previous_release_id"], journal["previous_qgc_sha256"]
        )
        current = self._load_state()
        previous_state = journal["previous_state"]
        if current["state_id"] != previous_state["state_id"]:
            restored = json.loads(json.dumps(previous_state))
            restored["generation"] = max(
                int(restored["generation"]), int(current["generation"])
            )
            self._commit_state(restored)
            self._sync_cache_protection(restored)
        if journal["previous_release_id"] is not None:
            self._service("restart", self.policy["application"]["gc_unit"])
        else:
            self._service("stop", self.policy["application"]["gc_unit"])
        if journal["qgc_was_running"]:
            self._service("start", self.policy["application"]["qgc_unit"])
        self._journal(None)
        self._set_drain(operation_id=journal["operation_id"], enabled=False)
        self._audit(
            "reconcile",
            "rolled-back",
            operation_id=journal["operation_id"],
            transaction_id=journal["transaction_id"],
        )
        return {"state": "rolled-back", "transaction_id": journal["transaction_id"]}

    def reconcile(self) -> dict[str, Any]:
        with self._locked():
            return self._reconcile_locked()

    def rollback(
        self,
        *,
        operation_id: str,
        safety: Mapping[str, Any],
        override_reason: str | None = None,
        override_confirmation: str | None = None,
    ) -> dict[str, Any]:
        state = self.state()
        previous = state["previous_release_id"]
        if previous is None:
            raise GCApplicationError("no previous field GC release is retained")
        with self._locked():
            current = self._load_state()
            current["staged_release_id"] = previous
            self._commit_state(current)
        return self.activate(
            previous,
            operation_id=operation_id,
            safety=safety,
            override_reason=override_reason,
            override_confirmation=override_confirmation,
        )

    def restore_release(
        self,
        release_id: str,
        *,
        operation_id: str,
        safety: Mapping[str, Any],
        override_reason: str | None = None,
        override_confirmation: str | None = None,
    ) -> dict[str, Any]:
        """Restore an exact installed release after a paired drone failure."""
        with self._locked():
            self._reconcile_locked()
            current = self._load_state()
            if release_id not in current["releases"]:
                raise GCApplicationError("paired rollback release is not installed")
            current["staged_release_id"] = release_id
            self._commit_state(current)
            self._sync_cache_protection(current)
        return self.activate(
            release_id,
            operation_id=operation_id,
            safety=safety,
            override_reason=override_reason,
            override_confirmation=override_confirmation,
        )

    def _garbage_collect_locked(self) -> dict[str, Any]:
        self._cleanup_partials()
        state = self._load_state()
        protected = {
            item
            for item in (
                state["active_release_id"],
                state["previous_release_id"],
                state["staged_release_id"],
                state["qualified_anchor_release_id"],
            )
            if item is not None
        }
        removed = []
        for release_id in sorted(set(state["releases"]) - protected):
            path = self.releases_root / release_id
            if path.is_symlink() or not path.is_dir():
                raise GCApplicationError("GC garbage collection found an unsafe slot")
            rmtree(path)
            state["releases"].pop(release_id)
            removed.append(release_id)
        referenced_qgc = {
            state["releases"][release_id]["qgroundcontrol"]["sha256"]
            for release_id in protected
        }
        for path in sorted(self.qgc_slots_root.iterdir()):
            if path.name not in referenced_qgc:
                if path.is_symlink() or not path.is_dir():
                    raise GCApplicationError(
                        "QGroundControl garbage collection found an unsafe slot"
                    )
                rmtree(path)
        if removed:
            self._commit_state(state)
        self._sync_cache_protection(state)
        self.prune_cache()
        return {
            "removed_release_ids": removed,
            "protected_release_ids": sorted(protected),
        }

    def garbage_collect(self) -> dict[str, Any]:
        with self._locked():
            return self._garbage_collect_locked()

    def release_manifest(self, release_id: str) -> dict[str, Any]:
        with self._locked():
            state = self._load_state()
            if release_id not in state["releases"]:
                raise GCApplicationError("GC release is not installed")
            authenticated = self._authenticated_bundle(
                state["releases"][release_id]["archive_sha256"], release_id
            )
            return self._verify_slot(
                self.releases_root / release_id, release_id, authenticated
            )["manifest"]
