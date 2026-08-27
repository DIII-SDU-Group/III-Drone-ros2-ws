"""Transactional, release-bound QGroundControl configuration ownership."""

from __future__ import annotations

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from shutil import copyfile, rmtree
import stat
import sys
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractRegistry, canonical_json, content_identity

HASH = re.compile(r"^[a-f0-9]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CLASS_ORDER = (
    "sensitive",
    "prohibited",
    "managed",
    "generated_cache",
    "local_preference",
)


class QGCConfigurationError(RuntimeError):
    """Fail-closed QGroundControl configuration error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise QGCConfigurationError(f"managed QGC directory is linked: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise QGCConfigurationError(f"managed QGC path is not a directory: {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise QGCConfigurationError(f"managed QGC directory has another owner: {path}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    parent = _private_directory(path.parent)
    if path.is_symlink():
        raise QGCConfigurationError(f"QGC configuration target is linked: {path}")
    temporary = parent / f".{path.name}.{os.getpid()}.partial"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, canonical_json(value) + b"\n")


def _load_document(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QGCConfigurationError(f"{label} is missing or linked")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QGCConfigurationError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise QGCConfigurationError(f"{label} must be a JSON object")
    return value


def _identity(document: Mapping[str, Any], field: str) -> str:
    return content_identity(
        {key: value for key, value in document.items() if key != field}
    )


def _parser(payload: bytes | None) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    if payload:
        try:
            parser.read_string(payload.decode("utf-8"))
        except (UnicodeDecodeError, configparser.Error) as exc:
            raise QGCConfigurationError(
                f"QGroundControl INI is malformed: {exc}"
            ) from exc
    return parser


def _serialize(parser: configparser.RawConfigParser) -> bytes:
    stream = io.StringIO(newline="\n")
    parser.write(stream, space_around_delimiters=False)
    return stream.getvalue().encode("utf-8")


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".17g")
    return str(value)


def _typed(raw: str, expected: Any) -> Any:
    if isinstance(expected, bool):
        lowered = raw.strip().lower()
        if lowered not in {"true", "false"}:
            raise QGCConfigurationError(f"managed QGC boolean is malformed: {raw!r}")
        return lowered == "true"
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise QGCConfigurationError(
                f"managed QGC integer is malformed: {raw!r}"
            ) from exc
    if isinstance(expected, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise QGCConfigurationError(
                f"managed QGC number is malformed: {raw!r}"
            ) from exc
    return raw


def _split_key(key: str) -> tuple[str, str]:
    if key.startswith("/") or "/" not in key:
        raise QGCConfigurationError(
            f"QGroundControl key is not section-qualified: {key}"
        )
    section, option = key.split("/", 1)
    if not section or not option or "\x00" in key:
        raise QGCConfigurationError(f"QGroundControl key is unsafe: {key!r}")
    return section, option


class QGCConfigurationStore:
    """Own release merge, redacted capture, promotion input, and generated caches."""

    def __init__(
        self,
        *,
        settings_path: Path,
        state_root: Path,
        policy_path: Path,
        baseline_path: Path,
        schema_root: Path,
        now: callable | None = None,
    ) -> None:
        self.settings_path = settings_path.expanduser().absolute()
        self.state_root = _private_directory(state_root.expanduser().absolute())
        self.backup_root = _private_directory(self.state_root / "backups")
        self.capture_root = _private_directory(self.state_root / "captures")
        self.generated_root = _private_directory(self.state_root / "generated-cache")
        self.registry = ContractRegistry(schema_root)
        self.policy_path = policy_path
        self.baseline_path = baseline_path
        self.policy = _load_document(policy_path, "QGroundControl key policy")
        self.baseline = _load_document(baseline_path, "QGroundControl baseline")
        self.registry.validate("qgc-key-policy", self.policy)
        self.registry.validate("qgc-managed-settings", self.baseline)
        if self.policy["policy_id"] != _identity(self.policy, "policy_id"):
            raise QGCConfigurationError("QGroundControl key policy identity mismatch")
        if self.baseline["settings_id"] != _identity(self.baseline, "settings_id"):
            raise QGCConfigurationError("QGroundControl baseline identity mismatch")
        if self.baseline["policy_id"] != self.policy["policy_id"]:
            raise QGCConfigurationError(
                "QGroundControl baseline uses another key policy"
            )
        if set(self.baseline["settings"]) != set(
            self.policy["classes"]["managed"]["exact"]
        ):
            raise QGCConfigurationError(
                "QGroundControl baseline must define every exact managed key"
            )
        for key, value in self.baseline["settings"].items():
            if value not in self.policy["value_guards"].get(key, {}).get("allowed", []):
                raise QGCConfigurationError(
                    f"QGroundControl baseline violates guard: {key}"
                )
        self.now = now or _now

    def _classify(self, key: str) -> str:
        for name in CLASS_ORDER:
            matchers = self.policy["classes"][name]
            if key in matchers["exact"] or any(
                key.startswith(prefix) for prefix in matchers["prefixes"]
            ):
                return name
            for pattern in matchers["patterns"]:
                try:
                    if re.search(pattern, key):
                        return name
                except re.error as exc:
                    raise QGCConfigurationError(
                        f"invalid QGroundControl policy expression: {pattern}"
                    ) from exc
        return "unknown"

    def _load_settings(self) -> tuple[bytes | None, configparser.RawConfigParser]:
        if self.settings_path.is_symlink():
            raise QGCConfigurationError("QGroundControl settings file is linked")
        if not self.settings_path.exists():
            return None, _parser(None)
        if not self.settings_path.is_file():
            raise QGCConfigurationError("QGroundControl settings is not a file")
        metadata = self.settings_path.stat(follow_symlinks=False)
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise QGCConfigurationError("QGroundControl settings has another owner")
        payload = self.settings_path.read_bytes()
        if len(payload) > 16 * 1024 * 1024:
            raise QGCConfigurationError("QGroundControl settings exceeds 16 MiB")
        return payload, _parser(payload)

    def _managed_values(self, parser: configparser.RawConfigParser) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, expected in self.baseline["settings"].items():
            section, option = _split_key(key)
            if parser.has_option(section, option):
                values[key] = _typed(parser.get(section, option), expected)
        return values

    def _backup(self, payload: bytes | None, *, release_id: str) -> dict[str, Any]:
        digest = hashlib.sha256(payload or b"").hexdigest()
        backup_id = content_identity(
            {
                "release_id": release_id,
                "settings_sha256": digest,
                "settings_present": payload is not None,
            }
        )
        data_path = self.backup_root / f"{backup_id}.ini"
        record_path = self.backup_root / f"{backup_id}.json"
        if payload is not None and not data_path.exists():
            _atomic_bytes(data_path, payload, mode=0o600)
        record = {
            "schema": "iii.qgc-settings-backup/v1",
            "backup_id": backup_id,
            "release_id": release_id,
            "settings_present": payload is not None,
            "settings_sha256": digest,
        }
        if not record_path.exists():
            _atomic_json(record_path, record)
        return record

    def restore(self, backup_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(backup_id):
            raise QGCConfigurationError("QGroundControl backup identity is invalid")
        record = _load_document(
            self.backup_root / f"{backup_id}.json", "QGroundControl backup record"
        )
        if record.get("backup_id") != backup_id:
            raise QGCConfigurationError(
                "QGroundControl backup record identity mismatch"
            )
        if record.get("settings_present"):
            source = self.backup_root / f"{backup_id}.ini"
            if (
                source.is_symlink()
                or not source.is_file()
                or _sha256(source) != record.get("settings_sha256")
            ):
                raise QGCConfigurationError("QGroundControl backup bytes changed")
            _parser(source.read_bytes())
            _atomic_bytes(self.settings_path, source.read_bytes())
        elif self.settings_path.exists() or self.settings_path.is_symlink():
            if self.settings_path.is_symlink() or not self.settings_path.is_file():
                raise QGCConfigurationError("QGroundControl settings target is unsafe")
            self.settings_path.unlink()
            _fsync_directory(self.settings_path.parent)
        return {"restored": True, "backup_id": backup_id}

    def apply(
        self,
        *,
        qgc_version: str,
        release_id: str,
        profile: str,
        qgc_running: bool,
        backup_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if qgc_running:
            raise QGCConfigurationError(
                "QGroundControl must be stopped before settings merge"
            )
        if qgc_version not in self.baseline["qgc_versions"]:
            raise QGCConfigurationError(
                "QGroundControl version is incompatible with baseline"
            )
        if profile not in self.baseline["profiles"]:
            raise QGCConfigurationError("QGroundControl profile is unsupported")
        if not HASH.fullmatch(release_id):
            raise QGCConfigurationError("QGroundControl release identity is invalid")
        payload, parser = self._load_settings()
        backup = self._backup(payload, release_id=release_id)
        if backup_callback is not None:
            # The transaction owner must durably retain this identity before the
            # first settings mutation. A callback failure therefore leaves the
            # live settings untouched and the immutable backup available.
            backup_callback(dict(backup))
        try:
            for key, value in self.baseline["settings"].items():
                section, option = _split_key(key)
                if not parser.has_section(section):
                    parser.add_section(section)
                parser.set(section, option, _stringify(value))
            merged = _serialize(parser)
            _atomic_bytes(self.settings_path, merged)
            _, verified = self._load_settings()
            if self._managed_values(verified) != self.baseline["settings"]:
                raise QGCConfigurationError(
                    "QGroundControl settings merge did not verify"
                )
        except Exception:
            self.restore(backup["backup_id"])
            raise
        return {
            "schema": "iii.qgc-config-merge-result/v1",
            "release_id": release_id,
            "qgc_version": qgc_version,
            "profile": profile,
            "settings_id": self.baseline["settings_id"],
            "backup_id": backup["backup_id"],
            "settings_sha256": _sha256(self.settings_path),
        }

    def capture(
        self,
        *,
        qgc_version: str,
        release_id: str,
        clean_exit: bool,
        expected_settings_sha256: str | None = None,
    ) -> dict[str, Any]:
        if qgc_version not in self.policy["qgc_versions"]:
            raise QGCConfigurationError(
                "QGroundControl version is incompatible with policy"
            )
        if not HASH.fullmatch(release_id):
            raise QGCConfigurationError("QGroundControl release identity is invalid")
        payload, parser = self._load_settings()
        observed_sha256 = hashlib.sha256(payload or b"").hexdigest()
        if (
            expected_settings_sha256 is not None
            and observed_sha256 != expected_settings_sha256
        ):
            raise QGCConfigurationError(
                "QGroundControl settings changed after capture planning"
            )
        classified = {name: [] for name in (*CLASS_ORDER, "unknown")}
        violations = []
        for section in parser.sections():
            for option, raw in parser.items(section, raw=True):
                key = f"{section}/{option}"
                category = self._classify(key)
                classified[category].append(key)
                guard = self.policy["value_guards"].get(key)
                if guard is not None:
                    expected = self.baseline["settings"].get(key, "")
                    try:
                        value = _typed(raw, expected)
                    except QGCConfigurationError:
                        violations.append(
                            {"key": key, "reason": "malformed-managed-value"}
                        )
                    else:
                        if value not in guard["allowed"]:
                            violations.append({"key": key, "reason": "value-guard"})
        for values in classified.values():
            values.sort()
        content: dict[str, Any] = {
            "schema": "iii.qgc-config-capture/v1",
            "capture_id": "0" * 64,
            "captured_at": self.now(),
            "clean_exit": clean_exit,
            "release_id": release_id,
            "qgc_version": qgc_version,
            "policy_id": self.policy["policy_id"],
            "baseline_id": self.baseline["settings_id"],
            "managed": self._managed_values(parser),
            "classification": classified,
            "violations": sorted(
                violations, key=lambda item: (item["key"], item["reason"])
            ),
        }
        content["capture_id"] = _identity(content, "capture_id")
        self.registry.validate("qgc-config-capture", content)
        destination = self.capture_root / f"{content['capture_id']}.json"
        if not destination.exists():
            _atomic_json(destination, content)
        elif _load_document(destination, "QGroundControl capture") != content:
            raise QGCConfigurationError("QGroundControl capture identity collision")
        return content

    def load_capture(self, capture_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(capture_id):
            raise QGCConfigurationError("QGroundControl capture identity is invalid")
        value = _load_document(
            self.capture_root / f"{capture_id}.json", "QGroundControl capture"
        )
        self.registry.validate("qgc-config-capture", value)
        if value["capture_id"] != _identity(value, "capture_id"):
            raise QGCConfigurationError("QGroundControl capture content changed")
        if value["policy_id"] != self.policy["policy_id"]:
            raise QGCConfigurationError("QGroundControl capture uses another policy")
        return value

    def diff(self, capture_id: str) -> dict[str, Any]:
        capture = self.load_capture(capture_id)
        keys = sorted(set(self.baseline["settings"]) | set(capture["managed"]))
        changes = [
            {
                "key": key,
                "baseline": self.baseline["settings"].get(key),
                "captured": capture["managed"].get(key),
            }
            for key in keys
            if self.baseline["settings"].get(key) != capture["managed"].get(key)
        ]
        return {
            "schema": "iii.qgc-config-diff/v1",
            "capture_id": capture_id,
            "baseline_id": self.baseline["settings_id"],
            "changes": changes,
            "violations": capture["violations"],
            "promotable": clean_exit_and_safe(capture) and bool(changes),
        }

    def promoted_baseline(
        self, capture_id: str, accepted_keys: Sequence[str]
    ) -> dict[str, Any]:
        capture = self.load_capture(capture_id)
        if not clean_exit_and_safe(capture):
            raise QGCConfigurationError("QGroundControl capture is not clean and safe")
        accepted = set(accepted_keys)
        changed = {item["key"] for item in self.diff(capture_id)["changes"]}
        if not accepted or not accepted <= changed:
            raise QGCConfigurationError("promotion keys must be changed managed keys")
        for key in accepted:
            if self._classify(key) != "managed" or key not in capture["managed"]:
                raise QGCConfigurationError(
                    f"QGroundControl key is not promotable: {key}"
                )
            guard = self.policy["value_guards"].get(key)
            if guard and capture["managed"][key] not in guard["allowed"]:
                raise QGCConfigurationError(
                    f"QGroundControl promotion violates guard: {key}"
                )
        promoted = json.loads(json.dumps(self.baseline))
        for key in accepted:
            promoted["settings"][key] = capture["managed"][key]
        promoted["settings_id"] = _identity(promoted, "settings_id")
        self.registry.validate("qgc-managed-settings", promoted)
        return promoted

    def cache_generated(
        self,
        source: Path,
        *,
        qgc_version: str,
        px4_firmware: str,
        parameter_manifest_id: str,
    ) -> dict[str, Any]:
        if qgc_version not in self.policy["qgc_versions"]:
            raise QGCConfigurationError("generated QGC cache version is incompatible")
        if not HASH.fullmatch(parameter_manifest_id):
            raise QGCConfigurationError("PX4 parameter manifest identity is invalid")
        source = source.expanduser().absolute()
        if source.is_symlink() or not source.is_dir():
            raise QGCConfigurationError(
                "QGroundControl generated cache source is unsafe"
            )
        entries = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise QGCConfigurationError(
                    "QGroundControl generated cache contains a link"
                )
            if path.is_dir():
                continue
            if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
                raise QGCConfigurationError(
                    "QGroundControl generated cache entry is unsafe"
                )
            entries.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
        if not entries:
            raise QGCConfigurationError("QGroundControl generated cache is empty")
        content = {
            "schema": "iii.qgc-generated-cache/v1",
            "cache_id": "0" * 64,
            "qgc_version": qgc_version,
            "px4_firmware": px4_firmware,
            "parameter_manifest_id": parameter_manifest_id,
            "entries": entries,
        }
        content["cache_id"] = _identity(content, "cache_id")
        self.registry.validate("qgc-generated-cache", content)
        destination = self.generated_root / content["cache_id"]
        if not destination.exists():
            partial = destination.parent / f".{destination.name}.{os.getpid()}.partial"
            partial.mkdir(mode=0o700)
            try:
                for entry in entries:
                    target = partial / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    copyfile(source / entry["path"], target, follow_symlinks=False)
                    target.chmod(0o600)
                    if _sha256(target) != entry["sha256"]:
                        raise QGCConfigurationError("generated QGC cache copy changed")
                _atomic_json(partial / "cache-record.json", content)
                os.replace(partial, destination)
                _fsync_directory(destination.parent)
            finally:
                if partial.exists() and not partial.is_symlink():
                    rmtree(partial)
        return content

    def verify_generated(
        self,
        cache_id: str,
        *,
        qgc_version: str,
        px4_firmware: str,
        parameter_manifest_id: str,
    ) -> dict[str, Any]:
        if not HASH.fullmatch(cache_id):
            raise QGCConfigurationError("QGroundControl cache identity is invalid")
        root = self.generated_root / cache_id
        record = _load_document(
            root / "cache-record.json", "QGroundControl cache record"
        )
        self.registry.validate("qgc-generated-cache", record)
        if record["cache_id"] != _identity(record, "cache_id"):
            raise QGCConfigurationError("QGroundControl cache record changed")
        expected = (qgc_version, px4_firmware, parameter_manifest_id)
        observed = (
            record["qgc_version"],
            record["px4_firmware"],
            record["parameter_manifest_id"],
        )
        if observed != expected:
            raise QGCConfigurationError("QGroundControl cache compatibility differs")
        allowed = {item["path"]: item for item in record["entries"]}
        actual = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.name != "cache-record.json"
        }
        if set(actual) != set(allowed):
            raise QGCConfigurationError("QGroundControl cache topology changed")
        for name, entry in allowed.items():
            path = actual[name]
            if (
                path.is_symlink()
                or path.stat().st_size != entry["bytes"]
                or _sha256(path) != entry["sha256"]
            ):
                raise QGCConfigurationError("QGroundControl cache content changed")
        return record


def clean_exit_and_safe(capture: Mapping[str, Any]) -> bool:
    return bool(capture.get("clean_exit")) and not capture.get("violations")


def clean_exit_main(argv: Sequence[str] | None = None) -> int:
    """Capture redacted QGC settings after a clean user-unit stop."""

    parser = argparse.ArgumentParser(description=clean_exit_main.__doc__)
    parser.add_argument("--application-state", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--schemas", type=Path)
    args = parser.parse_args(argv)
    prefix = Path(os.environ.get("III_DEPLOYMENT_PREFIX", sys.prefix))
    application_state = (
        args.application_state
        or Path(
            os.environ.get(
                "III_GC_APPLICATION_STATE_ROOT",
                str(Path.home() / ".local/state/iii/gc"),
            )
        )
        / "application-state.json"
    )
    settings = args.settings or Path(
        os.environ.get(
            "III_QGC_SETTINGS_PATH",
            str(Path.home() / ".config/QGroundControl.org/QGroundControl.ini"),
        )
    )
    state_root = args.state_root or Path(
        os.environ.get(
            "III_QGC_CONFIGURATION_STATE_ROOT",
            str(Path.home() / ".local/state/iii/qgc-configuration"),
        )
    )
    policy = args.policy or prefix / "share/iii-deployment/qgc/key-policy.json"
    baseline = (
        args.baseline or prefix / "share/iii-deployment/qgc/managed-settings.json"
    )
    schemas = args.schemas or prefix / "share/iii-deployment/schemas/v1"
    try:
        application = _load_document(application_state, "GC application state")
        state_id = application.get("state_id")
        if state_id != content_identity(
            {key: value for key, value in application.items() if key != "state_id"}
        ):
            raise QGCConfigurationError("GC application state identity mismatch")
        release_id = application.get("active_release_id")
        release = application.get("releases", {}).get(release_id, {})
        qgc_version = release.get("qgroundcontrol", {}).get("version")
        if not isinstance(release_id, str) or not HASH.fullmatch(release_id):
            raise QGCConfigurationError("no active GC release for clean-exit capture")
        if not isinstance(qgc_version, str) or not VERSION.fullmatch(qgc_version):
            raise QGCConfigurationError("active QGroundControl version is unavailable")
        store = QGCConfigurationStore(
            settings_path=settings,
            state_root=state_root,
            policy_path=policy,
            baseline_path=baseline,
            schema_root=schemas,
        )
        capture = store.capture(
            qgc_version=qgc_version,
            release_id=release_id,
            clean_exit=True,
        )
        print(json.dumps(capture, sort_keys=True, separators=(",", ":")))
        return 0
    except (QGCConfigurationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": "iii.qgc-clean-exit-result/v1",
                    "outcome": "failed",
                    "error": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 20
