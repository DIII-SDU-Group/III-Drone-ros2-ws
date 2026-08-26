"""Fail-closed onboard release staging, retention, and status bookkeeping."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Any, Callable, Iterator, Mapping

from .bundle import VerifiedBundle, extract_bundle, validate_release_metadata, verify_bundle
from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .filesystem import StorageProjection, assert_regular_safe_tree, ensure_storage_reserve
from .release_status import require_fetchable_status, verify_status_index
from .signers import load_trusted_signers


STATE_SCHEMA = "iii.onboard-release-state/v1"
RECEIPT_SCHEMA = "iii.staged-release/v1"
STATUS_INDEX_NAME = "release-status-index.json"
STATE_NAME = "release-state.json"
LOCK_NAME = "release-state.lock"
RELEASE_ID_LENGTH = 64
DEPLOYABLE_STATUSES = frozenset({"qualified", "field-development"})


@dataclass(frozen=True)
class StageResult:
    release_id: str
    release_class: str
    staged: bool
    candidate_release_id: str
    remaining_bytes: int | None
    state_id: str


@dataclass(frozen=True)
class ActivationAuthorization:
    authorization_id: str
    release_id: str
    release_class: str
    state_id: str
    state_generation: int
    status_index_id: str | None
    status_statement_id: str | None
    recovery_only: bool
    flight_capable: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _state_identity(state: Mapping[str, Any]) -> str:
    return content_identity({key: value for key, value in state.items() if key != "state_id"})


def _authorization_identity(value: Mapping[str, Any]) -> str:
    return content_identity(
        {key: item for key, item in value.items() if key != "authorization_id"}
    )


def _canonical_document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_document(
    path: Path,
    value: Mapping[str, Any],
    *,
    mode: int = 0o640,
    owner: tuple[int, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.partial-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise ContractError(f"stale atomic state path requires reconciliation: {temporary.name}")
    raw = canonical_json(value) + b"\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
        if owner is not None:
            os.fchown(descriptor, *owner)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _initial_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "state_id": "",
        "generation": 0,
        "active_release_id": None,
        "rollback_release_id": None,
        "candidate_release_id": None,
        "qualified_anchor_release_id": None,
        "field_history": [],
        "status_index_id": None,
        "status_sequence": 0,
        "recovery": {
            "recovery_only": False,
            "flight_capable": True,
            "reason": None,
        },
        "releases": {},
    }
    state["state_id"] = _state_identity(state)
    return state


class ReleaseStore:
    """Own immutable release slots and durable selector-independent release state."""

    def __init__(
        self,
        target_root: Path,
        *,
        bundle_trust: Path | Mapping[str, Any],
        status_trust: Path | Mapping[str, Any],
        registry: ContractRegistry,
        host_limits: Mapping[str, int],
        minimum_reserve_bytes: int = 2 * 1024**3,
        minimum_reserve_percent: float = 10.0,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        runtime_group: str = "iii",
    ) -> None:
        self.target_root = target_root.resolve()
        if self.target_root == Path("/") and os.geteuid() != 0:
            raise ContractError("onboard release staging requires receiver root authority")
        self.releases_root = self.target_root / "opt/iii/releases"
        self.state_root = self.target_root / "var/lib/iii/deployment"
        self.state_path = self.state_root / STATE_NAME
        self.status_path = self.state_root / STATUS_INDEX_NAME
        self.lock_path = self.state_root / LOCK_NAME
        self.bundle_trust = bundle_trust
        self.status_trust = status_trust
        self.registry = registry
        self.host_limits = dict(host_limits)
        self.minimum_reserve_bytes = minimum_reserve_bytes
        self.minimum_reserve_percent = minimum_reserve_percent
        self.disk_usage = disk_usage
        try:
            self.runtime_gid = (
                grp.getgrnam(runtime_group).gr_gid
                if self.target_root == Path("/")
                else os.getgid()
            )
        except KeyError as exc:
            raise ContractError(f"runtime group is not provisioned: {runtime_group}") from exc
        self.state_owner = (0, self.runtime_gid) if self.target_root == Path("/") else None
        self.releases_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.releases_root.chmod(0o750)
        self.state_root.chmod(0o750)
        if self.target_root == Path("/"):
            os.chown(self.releases_root, 0, self.runtime_gid)
            os.chown(self.state_root, 0, self.runtime_gid)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _validate_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        self.registry.validate("onboard-release-state", state)
        if state.get("state_id") != _state_identity(state):
            raise ContractError("onboard release-state identity mismatch")
        releases = state["releases"]
        references = [
            state["active_release_id"],
            state["rollback_release_id"],
            state["candidate_release_id"],
            state["qualified_anchor_release_id"],
            *state["field_history"],
        ]
        missing = sorted({value for value in references if value is not None} - set(releases))
        if missing:
            raise ContractError("onboard release state references unknown releases: " + ", ".join(missing))
        if len(state["field_history"]) != len(set(state["field_history"])):
            raise ContractError("onboard release state repeats field history")
        for release_id, release in releases.items():
            if release["release_id"] != release_id:
                raise ContractError("onboard release-state key/identity disagreement")
            if release["release_class"] == "field-development" and release["status"] != "field-development":
                raise ContractError("field release acquired a qualified status classification")
        anchor = state["qualified_anchor_release_id"]
        if anchor is not None and releases[anchor]["release_class"] != "qualified":
            raise ContractError("protected qualified anchor is not a qualified release")
        return dict(state)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return _initial_state()
        return self._validate_state(_canonical_document(self.state_path, label="onboard release state"))

    def state(self) -> dict[str, Any]:
        with self._locked():
            return self._load_state()

    def _commit_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state["generation"] = int(state["generation"]) + 1
        state["state_id"] = _state_identity(state)
        self._validate_state(state)
        _atomic_document(self.state_path, state, owner=self.state_owner)
        return state

    def _load_status_index(self) -> dict[str, Any] | None:
        if not self.status_path.exists() and not self.status_path.is_symlink():
            return None
        index = _canonical_document(self.status_path, label="onboard release-status index")
        trust = (
            load_trusted_signers(self.status_trust, self.registry)
            if isinstance(self.status_trust, Path)
            else self.status_trust
        )
        verify_status_index(index, trust, self.registry)
        return index

    def _verified_status_update(
        self,
        incoming: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
        current = self._load_status_index()
        selected = current
        trust = (
            load_trusted_signers(self.status_trust, self.registry)
            if isinstance(self.status_trust, Path)
            else self.status_trust
        )
        if incoming is not None:
            latest = verify_status_index(incoming, trust, self.registry)
            if current is not None:
                current_sequence = int(current["sequence"])
                incoming_sequence = int(incoming["sequence"])
                if incoming_sequence < current_sequence:
                    raise ContractError("stale release-status index cannot replace onboard safety state")
                if incoming_sequence == current_sequence and incoming["index_id"] != current["index_id"]:
                    raise ContractError("conflicting release-status index at current sequence")
                retained = current["statements"]
                if incoming["statements"][: len(retained)] != retained:
                    raise ContractError("release-status index does not extend the onboard signed chain")
            selected = dict(incoming)
            return selected, latest
        if selected is None:
            return None, {}
        return selected, verify_status_index(selected, trust, self.registry)

    @staticmethod
    def _apply_statuses(
        state: dict[str, Any],
        index: Mapping[str, Any] | None,
        latest: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if index is not None:
            state["status_index_id"] = index["index_id"]
            state["status_sequence"] = index["sequence"]
        for release_id, release in state["releases"].items():
            if release["release_class"] != "qualified":
                continue
            statement = latest.get(release_id)
            if statement is None or statement["version"] != release["version"]:
                continue
            release["status"] = statement["status"]
            release["status_statement_id"] = statement["statement_id"]
        ReleaseStore._set_recovery_state(state)

    @staticmethod
    def _set_recovery_state(state: dict[str, Any]) -> None:
        unsafe: list[str] = []
        for role in ("active_release_id", "qualified_anchor_release_id"):
            release_id = state[role]
            if release_id is not None and state["releases"][release_id]["status"] == "unsafe":
                unsafe.append(f"{role.removesuffix('_release_id')}={release_id}")
        state["recovery"] = {
            "recovery_only": bool(unsafe),
            "flight_capable": not unsafe,
            "reason": (
                "unsafe installed release requires maintenance-safe recovery: " + ", ".join(unsafe)
                if unsafe
                else None
            ),
        }

    @staticmethod
    def _release_metadata(
        verified: VerifiedBundle,
        *,
        staged_at: str,
        status_statement: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        release = verified.release_manifest
        return {
            "release_id": release["release_id"],
            "release_class": release["release_class"],
            "version": release.get("version"),
            "component": verified.bundle_manifest["component"],
            "archive_sha256": verified.archive_sha256,
            "bundle_manifest_sha256": _sha256(verified.paths.bundle_manifest),
            "release_manifest_sha256": _sha256(verified.paths.release_manifest),
            "staged_at": staged_at,
            "accepted_sequence": None,
            "status": (
                status_statement["status"]
                if status_statement is not None
                else "field-development"
            ),
            "status_statement_id": (
                status_statement["statement_id"] if status_statement is not None else None
            ),
        }

    def _receipt(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": "",
            **{
                key: metadata[key]
                for key in (
                    "release_id",
                    "release_class",
                    "version",
                    "component",
                    "archive_sha256",
                    "bundle_manifest_sha256",
                    "release_manifest_sha256",
                    "staged_at",
                    "status",
                    "status_statement_id",
                )
            },
        }
        receipt["receipt_id"] = content_identity(
            {key: value for key, value in receipt.items() if key != "receipt_id"}
        )
        return receipt

    @staticmethod
    def _receipt_matches(receipt: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
        if receipt.get("schema") != RECEIPT_SCHEMA:
            return False
        expected_id = content_identity(
            {key: value for key, value in receipt.items() if key != "receipt_id"}
        )
        if receipt.get("receipt_id") != expected_id:
            return False
        for key in (
            "release_id",
            "release_class",
            "version",
            "component",
            "archive_sha256",
            "bundle_manifest_sha256",
            "release_manifest_sha256",
        ):
            if receipt.get(key) != metadata.get(key):
                return False
        return True

    def _freeze_release(self, root: Path) -> None:
        assert_regular_safe_tree(root)
        if self.target_root == Path("/"):
            for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                base = Path(current)
                os.chown(base, 0, self.runtime_gid)
                for name in (*directories, *files):
                    os.chown(base / name, 0, self.runtime_gid)
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            base = Path(current)
            for name in files:
                path = base / name
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o550 if mode & 0o111 else 0o440)
            for name in directories:
                (base / name).chmod(0o550)
        root.chmod(0o550)

    def _install_release(self, verified: VerifiedBundle, metadata: Mapping[str, Any]) -> None:
        release_id = metadata["release_id"]
        destination = self.releases_root / release_id
        staging = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=self.releases_root))
        try:
            extracted = staging / "extracted"
            extracted_verified = extract_bundle(
                verified.paths.directory,
                extracted,
                self.bundle_trust,
                registry=self.registry,
                host_limits=self.host_limits,
            )
            if (
                extracted_verified.archive_sha256 != verified.archive_sha256
                or extracted_verified.bundle_manifest != verified.bundle_manifest
                or extracted_verified.release_manifest != verified.release_manifest
            ):
                raise ContractError("bundle identity changed between verification and extraction")
            payload = extracted / "payload"
            if payload.is_symlink() or not payload.is_dir():
                raise ContractError("verified bundle did not contain a release payload directory")
            for child in sorted(payload.iterdir(), key=lambda value: value.name.encode("utf-8")):
                os.replace(child, staging / child.name)
            meta = extracted / "META"
            os.replace(meta / "bundle-manifest.json", staging / "bundle-manifest.json")
            os.replace(meta / "release-manifest.json", staging / "release-manifest.json")
            shutil.rmtree(extracted)
            receipt = self._receipt(metadata)
            (staging / "manifest.json").write_bytes(canonical_json(receipt) + b"\n")
            with (staging / "manifest.json").open("rb") as stream:
                os.fsync(stream.fileno())
            self._freeze_release(staging)
            self._verify_release_tree(staging, receipt)
            _fsync_directory(staging)
            os.replace(staging, destination)
            _fsync_directory(self.releases_root)
        except Exception:
            if staging.exists() and not staging.is_symlink():
                self._make_removable(staging)
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _verify_release_tree(
        self,
        root: Path,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_regular_safe_tree(root)
        observed_receipt = (
            dict(receipt)
            if receipt is not None
            else _canonical_document(root / "manifest.json", label="staged release receipt")
        )
        if not self._receipt_matches(observed_receipt, observed_receipt):
            raise ContractError("staged release receipt identity is invalid")
        bundle = _canonical_document(root / "bundle-manifest.json", label="staged bundle manifest")
        release = _canonical_document(root / "release-manifest.json", label="staged release manifest")
        self.registry.validate("bundle-manifest", bundle)
        validate_release_metadata(release, self.registry)
        if (
            _sha256(root / "bundle-manifest.json") != observed_receipt["bundle_manifest_sha256"]
            or _sha256(root / "release-manifest.json") != observed_receipt["release_manifest_sha256"]
            or bundle["release_manifest_sha256"] != observed_receipt["release_manifest_sha256"]
        ):
            raise ContractError("staged release metadata differs from its receipt")
        if (
            release["release_id"] != observed_receipt["release_id"]
            or release["release_class"] != observed_receipt["release_class"]
            or release.get("version") != observed_receipt["version"]
            or bundle["release_id"] != observed_receipt["release_id"]
            or bundle["component"] != observed_receipt["component"]
        ):
            raise ContractError("staged release logical identity differs from its receipt")
        expected: dict[str, Mapping[str, Any]] = {}
        for item in bundle["content"]:
            archive_path = PurePosixPath(item["path"])
            if archive_path.parts[0] != "payload" or len(archive_path.parts) < 2:
                raise ContractError("staged bundle content escapes the release root")
            relative = PurePosixPath(*archive_path.parts[1:]).as_posix()
            if relative in expected:
                raise ContractError("staged bundle content repeats a release path")
            expected[relative] = item
        observed: dict[str, str] = {}
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            directories.sort()
            files.sort()
            base = Path(current)
            for name in directories:
                observed[(base / name).relative_to(root).as_posix()] = "directory"
            for name in files:
                observed[(base / name).relative_to(root).as_posix()] = "file"
        metadata_files = {"manifest.json", "bundle-manifest.json", "release-manifest.json"}
        if set(observed) != set(expected) | metadata_files:
            raise ContractError("staged release has missing or extra immutable content")
        if stat.S_IMODE(root.stat().st_mode) != 0o550:
            raise ContractError("staged release root is not immutable")
        for relative, item in expected.items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            if observed[relative] != item["type"]:
                raise ContractError(f"staged release type differs from signed index: {relative}")
            expected_mode = 0o550 if item["type"] == "directory" or item["mode"] & 0o111 else 0o440
            if stat.S_IMODE(path.stat().st_mode) != expected_mode:
                raise ContractError(f"staged release mode differs from immutable policy: {relative}")
            if item["type"] == "file" and (
                path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]
            ):
                raise ContractError(f"staged release content differs from signed index: {relative}")
        for name in metadata_files:
            if stat.S_IMODE((root / name).stat().st_mode) != 0o440:
                raise ContractError(f"staged release metadata is writable: {name}")
        return observed_receipt

    @staticmethod
    def _make_removable(root: Path) -> None:
        if root.is_symlink() or not root.exists():
            return
        for current, directories, files in os.walk(root, topdown=False, followlinks=False):
            base = Path(current)
            for name in files:
                path = base / name
                if not path.is_symlink():
                    path.chmod(0o600)
            for name in directories:
                path = base / name
                if not path.is_symlink():
                    path.chmod(0o700)
        root.chmod(0o700)

    def _existing_release(self, metadata: Mapping[str, Any]) -> bool:
        destination = self.releases_root / metadata["release_id"]
        if not destination.exists() and not destination.is_symlink():
            return False
        if destination.is_symlink() or not destination.is_dir():
            raise ContractError("staged release path exists with an unsafe type")
        receipt = self._verify_release_tree(destination)
        if not self._receipt_matches(receipt, metadata):
            raise ContractError("existing staged release differs from the verified bundle identity")
        return True

    def stage(
        self,
        component_directory: Path,
        *,
        status_index: Mapping[str, Any] | None,
        staged_at: str,
    ) -> StageResult:
        """Verify and immutably stage a drone bundle without touching active state."""

        verified = verify_bundle(
            component_directory,
            self.bundle_trust,
            registry=self.registry,
            host_limits=self.host_limits,
        )
        if verified.bundle_manifest["component"] != "drone":
            raise ContractError("onboard release staging accepts only the drone component")
        release = verified.release_manifest
        with self._locked():
            state = self._load_state()
            selected_index, latest = self._verified_status_update(status_index)
            self._apply_statuses(state, selected_index, latest)
            if selected_index is not None:
                _atomic_document(self.status_path, selected_index, owner=self.state_owner)
                state = self._commit_state(state)
            statement = latest.get(release["release_id"])
            if release["release_class"] == "qualified":
                if statement is None or statement["version"] != release.get("version"):
                    raise ContractError("qualified release lacks an exact verified status statement")
                require_fetchable_status(statement)
            metadata = self._release_metadata(
                verified,
                staged_at=staged_at,
                status_statement=statement,
            )
            existing = self._existing_release(metadata)
            remaining: int | None = None
            if not existing:
                usage = self.disk_usage(self.releases_root)
                projection = StorageProjection(
                    incoming_bytes=verified.compressed_bytes,
                    extracted_bytes=int(verified.bundle_manifest["limits"]["unpacked_bytes"]),
                    receiver_bytes=64 * 1024,
                    checkpoint_bytes=64 * 1024,
                    diagnostics_bytes=64 * 1024,
                    retained_bytes=0,
                )
                remaining = ensure_storage_reserve(
                    projection,
                    available_bytes=int(usage.free),
                    filesystem_bytes=int(usage.total),
                    minimum_bytes=self.minimum_reserve_bytes,
                    minimum_percent=self.minimum_reserve_percent,
                )
                self._install_release(verified, metadata)
            previous_candidate = state["candidate_release_id"]
            observed = state["releases"].get(release["release_id"])
            if observed is not None:
                immutable = {
                    key: metadata[key]
                    for key in (
                        "release_id",
                        "release_class",
                        "version",
                        "component",
                        "archive_sha256",
                        "bundle_manifest_sha256",
                        "release_manifest_sha256",
                    )
                }
                if any(observed[key] != value for key, value in immutable.items()):
                    raise ContractError("onboard release metadata changed for an immutable release ID")
            else:
                state["releases"][release["release_id"]] = metadata
            if state["active_release_id"] != release["release_id"]:
                state["candidate_release_id"] = release["release_id"]
            state = self._commit_state(state)
            if (
                previous_candidate is not None
                and previous_candidate != release["release_id"]
                and previous_candidate not in self._protected_release_ids(state)
            ):
                self._remove_release(previous_candidate, state)
                state = self._commit_state(state)
            return StageResult(
                release_id=release["release_id"],
                release_class=release["release_class"],
                staged=not existing,
                candidate_release_id=state["candidate_release_id"] or release["release_id"],
                remaining_bytes=remaining,
                state_id=state["state_id"],
            )

    def _protected_release_ids(self, state: Mapping[str, Any]) -> set[str]:
        values = {
            state["active_release_id"],
            state["rollback_release_id"],
            state["candidate_release_id"],
            state["qualified_anchor_release_id"],
            *state["field_history"],
        }
        return {value for value in values if value is not None}

    def _remove_release(self, release_id: str, state: dict[str, Any]) -> None:
        if release_id in self._protected_release_ids(state):
            raise ContractError(f"release {release_id} is protected from garbage collection")
        destination = self.releases_root / release_id
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise ContractError("garbage collection found an unsafe release path")
            receipt = self._verify_release_tree(destination)
            if receipt.get("release_id") != release_id:
                raise ContractError("garbage collection release receipt identity mismatch")
            self._make_removable(destination)
            shutil.rmtree(destination)
            _fsync_directory(self.releases_root)
        state["releases"].pop(release_id, None)

    def garbage_collect(self) -> list[str]:
        """Collect only known unprotected releases and persist the reduced inventory."""

        with self._locked():
            state = self._load_state()
            protected = self._protected_release_ids(state)
            removed: list[str] = []
            for release_id in sorted(set(state["releases"]) - protected):
                self._remove_release(release_id, state)
                removed.append(release_id)
            if removed:
                self._commit_state(state)
            return removed

    def refresh_status(self, status_index: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a monotonic signed status index and expose installed safety state."""

        with self._locked():
            state = self._load_state()
            selected_index, latest = self._verified_status_update(status_index)
            assert selected_index is not None
            self._apply_statuses(state, selected_index, latest)
            _atomic_document(self.status_path, selected_index, owner=self.state_owner)
            return self._commit_state(state)

    def authorize_activation(
        self,
        release_id: str,
        *,
        status_index: Mapping[str, Any] | None,
        allow_unsafe_recovery: bool = False,
    ) -> ActivationAuthorization:
        """Recheck current signed status and bind authorization to exact staged state."""

        with self._locked():
            state = self._load_state()
            if state["candidate_release_id"] != release_id:
                raise ContractError("activation release is not the staged candidate")
            selected_index, latest = self._verified_status_update(status_index)
            self._apply_statuses(state, selected_index, latest)
            release = state["releases"][release_id]
            statement = latest.get(release_id)
            status_statement_id = None
            recovery_only = bool(state["recovery"]["recovery_only"])
            if selected_index is not None:
                _atomic_document(self.status_path, selected_index, owner=self.state_owner)
                state = self._commit_state(state)
            if release["release_class"] == "qualified":
                if statement is None or statement["version"] != release["version"]:
                    raise ContractError("qualified activation lacks an exact verified status statement")
                status_statement_id = statement["statement_id"]
                if statement["status"] in {"withdrawn", "unsafe"}:
                    if statement["status"] == "withdrawn":
                        raise ContractError("withdrawn release cannot be activated")
                    viable = [
                        item["release_id"]
                        for item in state["releases"].values()
                        if item["accepted_sequence"] is not None
                        and item["status"] in DEPLOYABLE_STATUSES
                        and item["release_id"] != release_id
                    ]
                    if not allow_unsafe_recovery or viable:
                        raise ContractError("unsafe release is not eligible for last-resort recovery")
                    recovery_only = True
            if selected_index is None:
                state = self._commit_state(state)
            value = {
                "authorization_id": "",
                "release_id": release_id,
                "release_class": release["release_class"],
                "state_id": state["state_id"],
                "state_generation": state["generation"],
                "status_index_id": state["status_index_id"],
                "status_statement_id": status_statement_id,
                "recovery_only": recovery_only,
                "flight_capable": not recovery_only,
            }
            value["authorization_id"] = _authorization_identity(value)
            return ActivationAuthorization(**value)

    def record_acceptance(
        self,
        authorization: ActivationAuthorization,
        *,
        explicit_qualified_action: bool,
    ) -> dict[str, Any]:
        """Record receiver acceptance without switching the runtime selector."""

        value = dict(authorization.__dict__)
        if value["authorization_id"] != _authorization_identity(value):
            raise ContractError("activation authorization identity mismatch")
        with self._locked():
            state = self._load_state()
            if (
                state["state_id"] != authorization.state_id
                or state["generation"] != authorization.state_generation
                or state["candidate_release_id"] != authorization.release_id
                or state["status_index_id"] != authorization.status_index_id
            ):
                raise ContractError("activation authorization is stale")
            release = state["releases"][authorization.release_id]
            if release["release_class"] == "qualified":
                if not explicit_qualified_action:
                    raise ContractError("qualified acceptance requires explicit qualified authority")
                if release["status"] == "unsafe" and not authorization.recovery_only:
                    raise ContractError("unsafe qualified release lacks recovery-only authorization")
                if release["status"] not in {"qualified", "unsafe"}:
                    raise ContractError("qualified release status no longer permits acceptance")
            elif explicit_qualified_action:
                raise ContractError("field-development release cannot use qualified authority")
            previous = state["active_release_id"]
            state["rollback_release_id"] = previous
            state["active_release_id"] = authorization.release_id
            state["candidate_release_id"] = None
            accepted = [
                int(item["accepted_sequence"])
                for item in state["releases"].values()
                if item["accepted_sequence"] is not None
            ]
            release["accepted_sequence"] = max(accepted, default=0) + 1
            if release["release_class"] == "field-development":
                state["field_history"] = [
                    authorization.release_id,
                    *(
                        item
                        for item in state["field_history"]
                        if item != authorization.release_id
                    ),
                ][:2]
            elif release["status"] == "qualified":
                state["qualified_anchor_release_id"] = authorization.release_id
            self._set_recovery_state(state)
            return self._commit_state(state)
