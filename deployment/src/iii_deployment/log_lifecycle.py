"""Boot/session log retention, pre-clock buffering, and receipt-bound transfer."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_identity
from .receiver.state import atomic_document

SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
SOURCE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
MAX_CHUNK_BYTES = 512 * 1024
PROTECTED_PATH_PARTS = frozenset(
    {
        "rosbag",
        "rosbags",
        "dataset",
        "datasets",
        "tuning",
        "configuration",
        "config-checkpoints",
        "shadow-checkpoints",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError("log timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _document(path: Path, *, label: str) -> dict[str, Any]:
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


def _file_identity(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise ContractError("log inventory contains a symbolic link")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError("log inventory contains a non-regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), metadata.st_size


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ContractError("log locator is not a safe relative path")
    return path


def _identity_without(value: Mapping[str, Any], *fields: str) -> str:
    omitted = set(fields)
    return content_identity(
        {key: item for key, item in value.items() if key not in omitted}
    )


@dataclass(frozen=True)
class LogPolicy:
    retention_days: int = 14
    maximum_bytes: int = 1024**3
    maximum_filesystem_percent: int = 5
    protected_completed_sessions: int = 4
    debug_session_max_bytes: int = 256 * 1024**2
    degraded_max_records: int = 10_000
    degraded_max_bytes: int = 16 * 1024**2
    failed_diagnostics_days: int = 30

    @classmethod
    def from_operational_policy(cls, value: Mapping[str, Any]) -> "LogPolicy":
        logging = value["logging"]
        return cls(
            retention_days=logging["retention_days"],
            maximum_bytes=logging["maximum_bytes"],
            maximum_filesystem_percent=logging["maximum_filesystem_percent"],
            protected_completed_sessions=logging["protected_completed_sessions"],
            debug_session_max_bytes=logging["debug_session_max_bytes"],
            degraded_max_records=logging["degraded_max_records"],
            degraded_max_bytes=logging["degraded_max_bytes"],
            failed_diagnostics_days=logging["failed_diagnostics_days"],
        )


class SessionLogStore:
    """Own immutable completed session logs and exact retention planning."""

    def __init__(self, root: Path, policy: LogPolicy):
        self.root = root
        self.policy = policy

    def session_root(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise ContractError("invalid runtime session ID")
        return self.root / "sessions" / session_id

    def begin(
        self,
        *,
        session_id: str,
        boot_id: str,
        started_monotonic_ns: int,
        debug_enabled: bool = False,
        started_utc: str | None = None,
    ) -> dict[str, Any]:
        if any(item["state"] == "current" for item in self.sessions()):
            raise ContractError("another runtime log session is current")
        path = self.session_root(session_id)
        if path.exists() or path.is_symlink():
            raise ContractError("runtime log session already exists")
        value = {
            "schema": "iii.log-session/v1",
            "session_id": session_id,
            "sequence": max(
                (int(item.get("sequence", 0)) for item in self.sessions()), default=0
            )
            + 1,
            "boot_id": boot_id,
            "state": "current",
            "started_monotonic_ns": started_monotonic_ns,
            "started_utc": started_utc,
            "completed_utc": None,
            "completion_reason": None,
            "debug_enabled": debug_enabled,
            "last_transitions": {},
        }
        value["session_identity"] = content_identity(value)
        atomic_document(path / "session.json", value, mode=0o640)
        return value

    def complete(
        self,
        session_id: str,
        *,
        completed_utc: str | None,
        reason: str = "clean-shutdown",
    ) -> dict[str, Any]:
        value = self._session(session_id)
        if value["state"] != "current":
            raise ContractError("only the current log session can be completed")
        value["state"] = "completed"
        value["completed_utc"] = completed_utc
        value["completion_reason"] = reason
        value["session_identity"] = content_identity(
            {key: item for key, item in value.items() if key != "session_identity"}
        )
        atomic_document(
            self.session_root(session_id) / "session.json", value, mode=0o440
        )
        return value

    def recover_interrupted(self, *, boot_id: str) -> list[str]:
        recovered = []
        for value in self.sessions():
            if value["state"] != "current":
                continue
            reason = (
                "process-restart" if value["boot_id"] == boot_id else "boot-interrupted"
            )
            self.complete(value["session_id"], completed_utc=None, reason=reason)
            recovered.append(value["session_id"])
        return recovered

    def append(
        self,
        session_id: str,
        *,
        source: str,
        record: Mapping[str, Any],
        debug: bool = False,
        transition_key: str | None = None,
        transition_value: str | None = None,
    ) -> bool:
        if not SOURCE_ID.fullmatch(source):
            raise ContractError("invalid log source ID")
        session = self._session(session_id)
        if session["state"] != "current":
            raise ContractError("completed runtime logs are immutable")
        if debug and not session["debug_enabled"]:
            raise ContractError("debug logging is not enabled for this session")
        if transition_key is not None:
            observed = session["last_transitions"].get(transition_key)
            if observed == transition_value:
                return False
            session["last_transitions"][transition_key] = transition_value
            session["session_identity"] = content_identity(
                {
                    key: item
                    for key, item in session.items()
                    if key != "session_identity"
                }
            )
            atomic_document(
                self.session_root(session_id) / "session.json", session, mode=0o640
            )
        directory = self.session_root(session_id) / ("debug" if debug else "logs")
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        path = directory / f"{source}.jsonl"
        if path.is_symlink():
            raise ContractError("runtime log path is linked")
        encoded = canonical_json(dict(record)) + b"\n"
        if debug:
            used = 0
            for item in directory.glob("*.jsonl"):
                if item.is_symlink() or not item.is_file():
                    raise ContractError("debug session log inventory is unsafe")
                used += item.stat().st_size
            if used + len(encoded) > self.policy.debug_session_max_bytes:
                raise ContractError("debug session log cap would be exceeded")
        descriptor = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o640
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("runtime log append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True

    def sessions(self) -> list[dict[str, Any]]:
        root = self.root / "sessions"
        if not root.exists() and not root.is_symlink():
            return []
        if root.is_symlink() or not root.is_dir():
            raise ContractError("runtime session root is unsafe")
        values = []
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_dir():
                raise ContractError(
                    "runtime session inventory contains an unsafe entry"
                )
            values.append(self._session(path.name))
        return values

    def _session(self, session_id: str) -> dict[str, Any]:
        value = _document(
            self.session_root(session_id) / "session.json", label="runtime log session"
        )
        identity = value.pop("session_identity", None)
        expected = content_identity(value)
        value["session_identity"] = identity
        if identity != expected or value.get("session_id") != session_id:
            raise ContractError("runtime log session identity mismatch")
        if value.get("state") not in {"current", "completed"}:
            raise ContractError("runtime log session state is invalid")
        return value

    def _files(self, session_id: str) -> list[dict[str, Any]]:
        root = self.session_root(session_id)
        rows = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise ContractError("runtime log session contains a symbolic link")
            if path.is_dir():
                continue
            digest, size = _file_identity(path)
            relative = path.relative_to(self.root).as_posix()
            protected_domain = bool(PROTECTED_PATH_PARTS.intersection(path.parts))
            rows.append(
                {
                    "locator": relative,
                    "content_id": digest,
                    "size": size,
                    "protected_domain": protected_domain,
                }
            )
        return rows

    def retention_plan(
        self,
        *,
        now: datetime,
        filesystem_total_bytes: int,
        filesystem_free_bytes: int,
        deployment_reserve_bytes: int,
    ) -> dict[str, Any]:
        sessions = self.sessions()
        completed = sorted(
            (item for item in sessions if item["state"] == "completed"),
            key=lambda item: int(item.get("sequence", 0)),
            reverse=True,
        )
        mandatory = {
            item["session_id"] for item in sessions if item["state"] == "current"
        }
        mandatory.update(
            item["session_id"]
            for item in completed[: self.policy.protected_completed_sessions]
        )
        rows = []
        for session in sessions:
            files = self._files(session["session_id"])
            rows.append(
                {
                    **session,
                    "files": files,
                    "bytes": sum(item["size"] for item in files),
                }
            )
        cap = min(
            self.policy.maximum_bytes,
            filesystem_total_bytes * self.policy.maximum_filesystem_percent // 100,
            sum(item["bytes"] for item in rows)
            + max(0, filesystem_free_bytes - deployment_reserve_bytes),
        )
        remove: list[dict[str, Any]] = []
        retained = list(rows)
        deadline = now - timedelta(days=self.policy.retention_days)
        for row in sorted(rows, key=lambda item: int(item.get("sequence", 0))):
            if (
                row["session_id"] in mandatory
                or row["state"] != "completed"
                or any(file["protected_domain"] for file in row["files"])
            ):
                continue
            if row["completed_utc"] and _parse_utc(row["completed_utc"]) < deadline:
                remove.append(row)
                retained.remove(row)
        for row in sorted(
            list(retained), key=lambda item: int(item.get("sequence", 0))
        ):
            if sum(item["bytes"] for item in retained) <= cap:
                break
            if (
                row["session_id"] in mandatory
                or row["state"] != "completed"
                or any(file["protected_domain"] for file in row["files"])
            ):
                continue
            remove.append(row)
            retained.remove(row)
        protected = set(mandatory)
        protected.update(
            row["session_id"]
            for row in rows
            if any(item["protected_domain"] for item in row["files"])
        )
        value = {
            "schema": "iii.log-retention-plan/v1",
            "cap_bytes": cap,
            "current_bytes": sum(item["bytes"] for item in rows),
            "projected_bytes": sum(item["bytes"] for item in retained),
            "protected_session_ids": sorted(protected),
            "remove": [
                {
                    "session_id": item["session_id"],
                    "session_identity": item["session_identity"],
                    "bytes": item["bytes"],
                    "files": item["files"],
                }
                for item in remove
            ],
            "protected_overage": sum(item["bytes"] for item in retained) > cap,
        }
        value["plan_id"] = content_identity(value)
        return value

    def apply_retention(self, plan: Mapping[str, Any]) -> list[str]:
        expected = content_identity(
            {key: item for key, item in plan.items() if key != "plan_id"}
        )
        if (
            plan.get("schema") != "iii.log-retention-plan/v1"
            or plan.get("plan_id") != expected
        ):
            raise ContractError("log retention plan identity mismatch")
        targets: list[tuple[Mapping[str, Any], Path]] = []
        for item in plan["remove"]:
            current = self._session(item["session_id"])
            if (
                current["state"] != "completed"
                or current["session_identity"] != item["session_identity"]
            ):
                raise ContractError("log session changed after retention planning")
            if self._files(item["session_id"]) != item["files"]:
                raise ContractError(
                    "log session content changed after retention planning"
                )
            if any(file["protected_domain"] for file in item["files"]):
                raise ContractError("log retention plan includes protected evidence")
            root = self.session_root(item["session_id"])
            if root.is_symlink() or root.parent != self.root / "sessions":
                raise ContractError("log retention target is unsafe")
            targets.append((item, root))
        removed = []
        for item, root in targets:
            shutil.rmtree(root)
            removed.append(item["session_id"])
        if removed:
            descriptor = os.open(self.root / "sessions", os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return removed


class DegradedClockRing:
    def __init__(self, *, boot_id: str, policy: LogPolicy):
        self.boot_id = boot_id
        self.policy = policy
        self._rows: deque[tuple[dict[str, Any], int]] = deque()
        self._bytes = 0
        self.dropped_records = 0
        self._flushed = False

    def append(
        self,
        *,
        monotonic_ns: int,
        source: str,
        severity: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if self._flushed:
            raise ContractError("pre-clock log ring was already flushed")
        row = {
            "boot_id": self.boot_id,
            "monotonic_ns": monotonic_ns,
            "source": source,
            "severity": severity,
            "message": message,
            "details": dict(details or {}),
        }
        size = len(canonical_json(row)) + 1
        self._rows.append((row, size))
        self._bytes += size
        while (
            len(self._rows) > self.policy.degraded_max_records
            or self._bytes > self.policy.degraded_max_bytes
        ):
            _old, removed = self._rows.popleft()
            self._bytes -= removed
            self.dropped_records += 1

    def flush(
        self,
        store: SessionLogStore,
        session_id: str,
        *,
        synchronized_monotonic_ns: int,
        synchronized_utc_ns: int,
        uncertainty_ns: int,
    ) -> dict[str, Any]:
        if self._flushed:
            raise ContractError("pre-clock log ring was already flushed")
        written = 0
        for row, _size in self._rows:
            reconstructed = (
                synchronized_utc_ns + row["monotonic_ns"] - synchronized_monotonic_ns
            )
            store.append(
                session_id,
                source="preclock",
                record={
                    **row,
                    "utc_estimate_ns": reconstructed,
                    "utc_lower_ns": reconstructed - uncertainty_ns,
                    "utc_upper_ns": reconstructed + uncertainty_ns,
                    "utc_reconstructed": True,
                    "utc_uncertainty_ns": uncertainty_ns,
                },
            )
            written += 1
        store.append(
            session_id,
            source="preclock",
            record={
                "boot_id": self.boot_id,
                "kind": "preclock-flush",
                "records_flushed": written,
                "dropped_records": self.dropped_records,
                "utc_reconstructed": True,
                "utc_uncertainty_ns": uncertainty_ns,
            },
        )
        self._rows.clear()
        self._bytes = 0
        self._flushed = True
        return {
            "schema": "iii.preclock-flush/v1",
            "records_flushed": written,
            "dropped_records": self.dropped_records,
        }


class LogInventory:
    """Build receiver-owned inventories from fixed host roots and live protection state."""

    def __init__(
        self,
        *,
        source_root: Path,
        logs_root: Path,
        deployment_state_root: Path,
        activation_root: Path,
        audit_path: Path,
        transfer: "LogTransferStore",
        active_operation_ids: Callable[[], Iterable[str]],
        retained_release_ids: Callable[[], Iterable[str]],
        audit_operation_ids: Callable[[], Sequence[str]],
        deployment_audits: int = 50,
    ) -> None:
        self.source_root = source_root.absolute()
        self.logs_root = logs_root.absolute()
        self.deployment_state_root = deployment_state_root.absolute()
        self.activation_root = activation_root.absolute()
        self.audit_path = audit_path.absolute()
        self.transfer = transfer
        self.active_operation_ids = active_operation_ids
        self.retained_release_ids = retained_release_ids
        self.audit_operation_ids = audit_operation_ids
        self.deployment_audits = deployment_audits
        for path in (
            self.logs_root,
            self.deployment_state_root,
            self.activation_root,
            self.audit_path,
        ):
            if not path.is_relative_to(self.source_root):
                raise ContractError("log inventory root escapes the fixed source root")

    def _locator(self, path: Path) -> str:
        return path.relative_to(self.source_root).as_posix()

    @staticmethod
    def _walk(root: Path) -> list[Path]:
        if not root.exists() and not root.is_symlink():
            return []
        if root.is_symlink() or not root.is_dir():
            raise ContractError("log inventory root is linked or invalid")
        files = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise ContractError("log inventory contains a symbolic link")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ContractError("log inventory contains a special file")
            files.append(path)
        return files

    @staticmethod
    def _references(value: Any, identities: set[str]) -> bool:
        if isinstance(value, str):
            return value in identities
        if isinstance(value, dict):
            return any(
                LogInventory._references(item, identities) for item in value.values()
            )
        if isinstance(value, list):
            return any(LogInventory._references(item, identities) for item in value)
        return False

    def _operation_for(self, path: Path) -> str | None:
        if path.parent in {
            self.activation_root / "transactions",
            self.deployment_state_root / "operations",
        }:
            return path.stem if SESSION_ID.fullmatch(path.stem) else None
        evidence_root = self.activation_root / "evidence"
        if path.is_relative_to(evidence_root):
            relative = path.relative_to(evidence_root)
            if len(relative.parts) >= 2 and SESSION_ID.fullmatch(relative.parts[0]):
                return relative.parts[0]
        return None

    def _recent_operations(self) -> set[str]:
        recent: list[str] = []
        for operation_id in reversed(self.audit_operation_ids()):
            if operation_id not in recent:
                recent.append(operation_id)
            if len(recent) == self.deployment_audits:
                break
        return set(recent)

    def _current_session_locators(self, paths: Sequence[Path]) -> set[str]:
        protected: set[str] = set()
        session_roots = set()
        for path in paths:
            if path.name != "session.json":
                continue
            value = _document(path, label="runtime log session")
            if value.get("session_identity") != _identity_without(
                value, "session_identity"
            ):
                raise ContractError("runtime log session identity mismatch")
            if value.get("state") == "current":
                session_roots.add(path.parent)
        for path in paths:
            if any(path.is_relative_to(root) for root in session_roots):
                protected.add(self._locator(path))
        return protected

    def create_manifest(self, domain: str) -> dict[str, Any]:
        if domain == "logs":
            paths = [
                path
                for path in self._walk(self.logs_root)
                if not PROTECTED_PATH_PARTS.intersection(path.parts)
                and not path.is_relative_to(self.audit_path.parent)
            ]
            protected = self._current_session_locators(paths)
        elif domain == "diagnostics":
            paths = [
                *self._walk(self.activation_root),
                *self._walk(self.deployment_state_root / "operations"),
            ]
            if self.audit_path.exists() or self.audit_path.is_symlink():
                if self.audit_path.is_symlink() or not self.audit_path.is_file():
                    raise ContractError("receiver audit path is linked or invalid")
                paths.append(self.audit_path)
            active = set(self.active_operation_ids())
            recent = self._recent_operations()
            releases = set(self.retained_release_ids())
            protected = (
                {self._locator(self.audit_path)} if self.audit_path in paths else set()
            )
            for path in paths:
                operation = self._operation_for(path)
                if operation in active or operation in recent:
                    protected.add(self._locator(path))
                    continue
                if releases and path.suffix == ".json":
                    value = _document(path, label="deployment diagnostic")
                    if self._references(value, releases):
                        protected.add(self._locator(path))
        else:
            raise ContractError("unsupported log export domain")
        locators = [self._locator(path) for path in paths]
        return self.transfer.create_manifest(
            domain=domain,
            locators=locators,
            protected=sorted(protected),
        )

    def protected_locators(self, manifest: Mapping[str, Any]) -> list[str]:
        protected = {item["locator"] for item in manifest["files"] if item["protected"]}
        paths = {
            item["locator"]: self.source_root.joinpath(
                *_safe_relative(item["locator"]).parts
            )
            for item in manifest["files"]
        }
        if manifest["domain"] == "logs":
            protected.update(self._current_session_locators(list(paths.values())))
        elif manifest["domain"] == "diagnostics":
            active = set(self.active_operation_ids())
            recent = self._recent_operations()
            releases = set(self.retained_release_ids())
            for locator, path in paths.items():
                operation = self._operation_for(path)
                if (
                    path == self.audit_path
                    or operation in active
                    or operation in recent
                ):
                    protected.add(locator)
                    continue
                if releases and path.suffix == ".json":
                    value = _document(path, label="deployment diagnostic")
                    if self._references(value, releases):
                        protected.add(locator)
        else:
            raise ContractError("unsupported log export domain")
        return sorted(protected)

    def prune_plan(self, receipt_id: str) -> dict[str, Any]:
        receipt = self.transfer.load_receipt(receipt_id)
        manifest = self.transfer.manifest(receipt["manifest_id"])
        return self.transfer.prune_plan(
            receipt_id=receipt_id,
            additionally_protected=self.protected_locators(manifest),
        )

    def apply_prune(self, plan: Mapping[str, Any]) -> list[str]:
        if self.transfer.prune_started(str(plan.get("plan_id", ""))):
            return self.transfer.apply_prune(plan)
        current = self.prune_plan(str(plan.get("receipt_id", "")))
        if current != plan:
            raise ContractError(
                "log prune plan is stale against current protection state"
            )
        return self.transfer.apply_prune(plan)


class LogTransferStore:
    """Create immutable manifests, bounded chunks, receipts, and exact prune plans."""

    def __init__(
        self,
        *,
        source_root: Path,
        state_root: Path,
        minimum_reserve_bytes: int = 0,
        minimum_reserve_percent: float = 0,
    ):
        self.source_root = source_root
        self.state_root = state_root
        self.minimum_reserve_bytes = minimum_reserve_bytes
        self.minimum_reserve_percent = minimum_reserve_percent

    def _snapshot(self, path: Path) -> tuple[str, int]:
        if path.is_symlink():
            raise ContractError("log export source is linked")
        blobs = self.state_root / "blobs"
        blobs.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = blobs / f".snapshot.partial-{os.getpid()}-{os.urandom(8).hex()}"
        source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        output = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o440,
        )
        digest = hashlib.sha256()
        size = 0
        try:
            try:
                metadata = os.fstat(source)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ContractError("log export source is not a regular file")
                while True:
                    block = os.read(source, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            raise OSError("log snapshot write made no progress")
                        view = view[written:]
                os.fsync(output)
            finally:
                os.close(source)
                os.close(output)
            identity = digest.hexdigest()
            destination = blobs / identity
            if destination.exists() or destination.is_symlink():
                observed, observed_size = _file_identity(destination)
                if observed != identity or observed_size != size:
                    raise ContractError("log snapshot content identity collision")
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            return identity, size
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()

    def _ensure_snapshot_reserve(self, projected_bytes: int) -> None:
        usage = shutil.disk_usage(self.state_root.parent)
        reserve = max(
            self.minimum_reserve_bytes,
            int(usage.total * self.minimum_reserve_percent / 100.0),
        )
        if usage.free - projected_bytes < reserve:
            raise ContractError(
                "log export snapshot would violate the deployment storage reserve"
            )

    def create_manifest(
        self, *, domain: str, locators: Sequence[str], protected: Sequence[str] = ()
    ) -> dict[str, Any]:
        if domain not in {"logs", "diagnostics"}:
            raise ContractError("unsupported log export domain")
        protected_set = set(protected)
        files = []
        candidates: list[tuple[str, PurePosixPath, Path]] = []
        for locator in sorted(set(locators)):
            relative = _safe_relative(locator)
            path = self.source_root.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                raise ContractError(
                    "log export source is missing, linked, or not regular"
                )
            candidates.append((locator, relative, path))
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._ensure_snapshot_reserve(
            sum(
                path.stat(follow_symlinks=False).st_size
                for _locator, _relative, path in candidates
            )
        )
        for locator, relative, path in candidates:
            digest, size = self._snapshot(path)
            protected_domain = bool(PROTECTED_PATH_PARTS.intersection(relative.parts))
            files.append(
                {
                    "locator": relative.as_posix(),
                    "content_id": digest,
                    "size": size,
                    "protected": locator in protected_set or protected_domain,
                }
            )
        identity_value = {
            "schema": "iii.log-export-manifest/v1",
            "domain": domain,
            "files": files,
            "total_bytes": sum(item["size"] for item in files),
        }
        manifest_id = content_identity(identity_value)
        path = self.state_root / "exports" / f"{manifest_id}.json"
        if path.exists() or path.is_symlink():
            value = _document(path, label="log export manifest")
            if self._manifest_identity(value) != manifest_id:
                raise ContractError("log export manifest identity collision")
            return value
        value = {**identity_value, "manifest_id": manifest_id, "created_at": _utc_now()}
        atomic_document(path, value, mode=0o440)
        return value

    @staticmethod
    def _manifest_identity(value: Mapping[str, Any]) -> str:
        return _identity_without(value, "manifest_id", "created_at")

    def manifest(self, manifest_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(manifest_id):
            raise ContractError("invalid log export manifest identity")
        value = _document(
            self.state_root / "exports" / f"{manifest_id}.json",
            label="log export manifest",
        )
        expected = self._manifest_identity(value)
        if value.get("manifest_id") != expected:
            raise ContractError("log export manifest content identity mismatch")
        return value

    def chunk(
        self, *, manifest_id: str, content_id: str, offset: int, length: int
    ) -> dict[str, Any]:
        if offset < 0 or length < 1 or length > MAX_CHUNK_BYTES:
            raise ContractError("log chunk bounds are invalid")
        manifest = self.manifest(manifest_id)
        matches = [
            item for item in manifest["files"] if item["content_id"] == content_id
        ]
        if not matches:
            raise ContractError("log chunk content is not in the export manifest")
        item = matches[0]
        if any(match["size"] != item["size"] for match in matches):
            raise ContractError(
                "log manifest reuses a content identity with another size"
            )
        path = self.state_root / "blobs" / content_id
        observed, size = _file_identity(path)
        if observed != content_id or size != item["size"] or offset > size:
            raise ContractError("log content changed after export planning")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            data = os.pread(descriptor, min(length, size - offset), offset)
        finally:
            os.close(descriptor)
        return {
            "schema": "iii.log-chunk/v1",
            "manifest_id": manifest_id,
            "content_id": content_id,
            "offset": offset,
            "data": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) == size,
        }

    def receipt(
        self,
        *,
        manifest_id: str,
        client_id: str,
        verified_files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        planned = self.receipt_plan(
            manifest_id=manifest_id,
            client_id=client_id,
            verified_files=verified_files,
        )
        receipt_id = planned["receipt_id"]
        identity_value = {
            key: item for key, item in planned.items() if key != "receipt_id"
        }
        path = self.state_root / "receipts" / f"{receipt_id}.json"
        if path.exists() or path.is_symlink():
            value = _document(path, label="log pull receipt")
            if self._receipt_identity(value) != receipt_id:
                raise ContractError("log pull receipt identity collision")
            return value
        value = {**identity_value, "receipt_id": receipt_id, "verified_at": _utc_now()}
        atomic_document(path, value, mode=0o440)
        return value

    def receipt_plan(
        self,
        *,
        manifest_id: str,
        client_id: str,
        verified_files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        manifest = self.manifest(manifest_id)
        expected = [
            {
                "locator": item["locator"],
                "content_id": item["content_id"],
                "size": item["size"],
            }
            for item in manifest["files"]
        ]
        observed = sorted(
            (dict(item) for item in verified_files), key=lambda item: item["locator"]
        )
        if observed != sorted(expected, key=lambda item: item["locator"]):
            raise ContractError(
                "local verification receipt is incomplete or mismatched"
            )
        identity_value = {
            "schema": "iii.log-pull-receipt/v1",
            "manifest_id": manifest_id,
            "client_id": client_id,
            "domain": manifest["domain"],
            "files": observed,
        }
        receipt_id = content_identity(identity_value)
        return {**identity_value, "receipt_id": receipt_id}

    @staticmethod
    def _receipt_identity(value: Mapping[str, Any]) -> str:
        return _identity_without(value, "receipt_id", "verified_at")

    def load_receipt(self, receipt_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(receipt_id):
            raise ContractError("invalid log receipt identity")
        value = _document(
            self.state_root / "receipts" / f"{receipt_id}.json",
            label="log pull receipt",
        )
        if value.get("receipt_id") != self._receipt_identity(value):
            raise ContractError("log pull receipt content identity mismatch")
        manifest = self.manifest(value["manifest_id"])
        if value.get("domain") != manifest["domain"]:
            raise ContractError("log pull receipt domain mismatch")
        return value

    def prune_plan(
        self, *, receipt_id: str, additionally_protected: Sequence[str] = ()
    ) -> dict[str, Any]:
        receipt = self.load_receipt(receipt_id)
        manifest = self.manifest(receipt["manifest_id"])
        protected = set(additionally_protected)
        remove = [
            item
            for item in manifest["files"]
            if not item["protected"] and item["locator"] not in protected
        ]
        value = {
            "schema": "iii.log-prune-plan/v1",
            "plan_id": "0" * 64,
            "receipt_id": receipt_id,
            "manifest_id": manifest["manifest_id"],
            "remove": remove,
            "protected": [item for item in manifest["files"] if item not in remove],
        }
        value["plan_id"] = content_identity(
            {key: item for key, item in value.items() if key != "plan_id"}
        )
        return value

    def _prune_transaction(self, plan_id: str) -> dict[str, Any] | None:
        if not HASH.fullmatch(plan_id):
            raise ContractError("invalid log prune transaction identity")
        root = self.state_root / "prunes" / plan_id
        path = root / "state.json"
        if not root.exists() and not root.is_symlink():
            return None
        if root.is_symlink() or not root.is_dir():
            raise ContractError("log prune transaction root is unsafe")
        value = _document(path, label="log prune transaction")
        identity = value.get("transaction_id")
        if (
            set(value)
            != {
                "schema",
                "transaction_id",
                "plan_id",
                "state",
                "plan",
                "removed",
            }
            or value.get("schema") != "iii.log-prune-transaction/v1"
            or value.get("plan_id") != plan_id
            or value.get("state") not in {"moving", "completed"}
            or not isinstance(value.get("plan"), dict)
            or not isinstance(value.get("removed"), list)
            or identity != _identity_without(value, "transaction_id")
        ):
            raise ContractError("log prune transaction state is invalid")
        return value

    def prune_started(self, plan_id: str) -> bool:
        return self._prune_transaction(plan_id) is not None

    def _write_prune_transaction(
        self,
        plan: Mapping[str, Any],
        *,
        state: str,
        removed: Sequence[str],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": "iii.log-prune-transaction/v1",
            "transaction_id": "0" * 64,
            "plan_id": plan["plan_id"],
            "state": state,
            "plan": dict(plan),
            "removed": list(removed),
        }
        value["transaction_id"] = _identity_without(value, "transaction_id")
        atomic_document(
            self.state_root / "prunes" / plan["plan_id"] / "state.json",
            value,
            mode=0o600,
        )
        return value

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_prune_quarantine(self, plan_id: str) -> None:
        root = self.state_root / "prunes" / plan_id / "files"
        if not root.exists() and not root.is_symlink():
            return
        if root.is_symlink() or not root.is_dir():
            raise ContractError("log prune quarantine is unsafe")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ContractError("log prune quarantine contains a link")
        shutil.rmtree(root)
        self._fsync_directory(root.parent)

    def apply_prune(self, plan: Mapping[str, Any]) -> list[str]:
        expected = content_identity(
            {key: item for key, item in plan.items() if key != "plan_id"}
        )
        if (
            plan.get("schema") != "iii.log-prune-plan/v1"
            or plan.get("plan_id") != expected
        ):
            raise ContractError("log prune plan identity mismatch")
        receipt = self.load_receipt(plan["receipt_id"])
        if receipt["manifest_id"] != plan["manifest_id"]:
            raise ContractError("log prune plan is not bound to its receipt")
        expected_plan = self.prune_plan(
            receipt_id=plan["receipt_id"],
            additionally_protected=[item["locator"] for item in plan["protected"]],
        )
        if expected_plan != plan:
            raise ContractError(
                "log prune plan differs from receiver protection policy"
            )
        transaction = self._prune_transaction(plan["plan_id"])
        expected_removed = [item["locator"] for item in plan["remove"]]
        if transaction is not None:
            if transaction["plan"] != plan:
                raise ContractError("log prune transaction carries another plan")
            if transaction["state"] == "completed":
                if transaction["removed"] != expected_removed:
                    raise ContractError("completed log prune inventory is invalid")
                self._cleanup_prune_quarantine(plan["plan_id"])
                return expected_removed

        transaction_root = self.state_root / "prunes" / plan["plan_id"]
        files_root = transaction_root / "files"
        if transaction is None:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
            prunes_root = self.state_root / "prunes"
            prunes_root.mkdir(exist_ok=True, mode=0o700)
            quarantine_device = prunes_root.stat(follow_symlinks=False).st_dev
            for item in plan["remove"]:
                path = self.source_root.joinpath(*_safe_relative(item["locator"]).parts)
                digest, size = _file_identity(path)
                metadata = path.stat(follow_symlinks=False)
                if (
                    digest != item["content_id"]
                    or size != item["size"]
                    or metadata.st_dev != quarantine_device
                ):
                    raise ContractError(
                        "receipt-backed log changed before prune or cannot be "
                        "atomically quarantined"
                    )
            transaction = self._write_prune_transaction(
                plan, state="moving", removed=[]
            )

        removed = list(transaction["removed"])
        for item in plan["remove"]:
            relative = _safe_relative(item["locator"])
            source = self.source_root.joinpath(*relative.parts)
            destination = files_root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.is_symlink():
                raise ContractError("log prune quarantine target is linked")
            if destination.exists():
                if source.exists() or source.is_symlink():
                    raise ContractError("log prune target exists on both sides")
                digest, size = _file_identity(destination)
            else:
                digest, size = _file_identity(source)
                if digest != item["content_id"] or size != item["size"]:
                    raise ContractError("receipt-backed log changed during prune")
                os.replace(source, destination)
                self._fsync_directory(source.parent)
                self._fsync_directory(destination.parent)
                digest, size = _file_identity(destination)
            if digest != item["content_id"] or size != item["size"]:
                raise ContractError("quarantined log content identity mismatch")
            if item["locator"] not in removed:
                removed.append(item["locator"])
                transaction = self._write_prune_transaction(
                    plan, state="moving", removed=removed
                )
        if removed != expected_removed:
            raise ContractError("log prune transaction removal order is invalid")
        self._write_prune_transaction(plan, state="completed", removed=removed)
        self._cleanup_prune_quarantine(plan["plan_id"])
        return removed
