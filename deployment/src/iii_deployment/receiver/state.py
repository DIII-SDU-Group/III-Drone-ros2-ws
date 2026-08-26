"""Durable receiver control state, operation journals, and hash-chained audit."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable, Mapping

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import Action, IDENTITY, OPERATION_ID, validate_mutation_plan


CONTROL_SCHEMA = "iii.receiver-control-state/v1"
JOURNAL_SCHEMA = "iii.receiver-operation-journal/v1"
AUDIT_SCHEMA = "iii.receiver-audit/v1"
TERMINAL_STATES = frozenset({"cancelled", "completed", "failed"})


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


def atomic_bytes(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.partial-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise ContractError(f"stale receiver state partial requires reconciliation: {temporary.name}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
    )
    try:
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


def atomic_document(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    atomic_bytes(path, canonical_json(value) + b"\n", mode=mode)


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def read_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ContractError(f"cannot read host boot identity: {exc}") from exc
    if not value:
        raise ContractError("host boot identity is empty")
    return value


@dataclass
class ReceiverControlStore:
    root: Path
    receiver_generation: int
    nonce_expiry_s: int
    monotonic: Callable[[], float]
    boot_id: Callable[[], str] = read_boot_id

    @property
    def path(self) -> Path:
        return self.root / "receiver-control.json"

    def _initial(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": CONTROL_SCHEMA,
            "control_id": "",
            "receiver_generation": self.receiver_generation,
            "lease": None,
            "nonces": {},
        }
        value["control_id"] = _identity(value, "control_id")
        return value

    def load(self) -> dict[str, Any]:
        if not self.path.exists() and not self.path.is_symlink():
            return self._initial()
        value = _canonical_document(self.path, label="receiver control state")
        if value.get("schema") != CONTROL_SCHEMA or value.get("control_id") != _identity(
            value, "control_id"
        ):
            raise ContractError("receiver control-state identity mismatch")
        if set(value) != {
            "schema",
            "control_id",
            "receiver_generation",
            "lease",
            "nonces",
        }:
            raise ContractError("receiver control-state fields are malformed")
        if value.get("receiver_generation") != self.receiver_generation:
            raise ContractError("receiver control state belongs to another receiver generation")
        if not isinstance(value.get("nonces"), dict):
            raise ContractError("receiver control nonces are malformed")
        for nonce_hash, record in value["nonces"].items():
            if not IDENTITY.fullmatch(nonce_hash) or not isinstance(record, dict) or set(record) != {
                "operation_id",
                "client_id",
                "plan_id",
                "issued_boot_id",
                "issued_monotonic",
                "expires_monotonic",
                "consumed",
            }:
                raise ContractError("receiver control nonce record is malformed")
            if not OPERATION_ID.fullmatch(str(record["operation_id"])):
                raise ContractError("receiver control nonce operation ID is invalid")
            if not IDENTITY.fullmatch(str(record["client_id"])) or not IDENTITY.fullmatch(
                str(record["plan_id"])
            ):
                raise ContractError("receiver control nonce binding is invalid")
            if not isinstance(record["consumed"], bool):
                raise ContractError("receiver control nonce consumption state is invalid")
            if not isinstance(record["issued_boot_id"], str) or not all(
                isinstance(record[field], (int, float)) and not isinstance(record[field], bool)
                for field in ("issued_monotonic", "expires_monotonic")
            ):
                raise ContractError("receiver control nonce clock binding is invalid")
            if record["expires_monotonic"] <= record["issued_monotonic"]:
                raise ContractError("receiver control nonce expiry is invalid")
        lease = value.get("lease")
        if lease is not None:
            if not isinstance(lease, dict) or set(lease) != {
                "operation_id",
                "client_id",
                "action",
                "plan_id",
                "acquired_boot_id",
                "acquired_monotonic",
            }:
                raise ContractError("receiver operation lease is malformed")
            if (
                not OPERATION_ID.fullmatch(str(lease["operation_id"]))
                or not IDENTITY.fullmatch(str(lease["client_id"]))
                or not IDENTITY.fullmatch(str(lease["plan_id"]))
                or lease["action"] not in {item.value for item in Action}
                or not isinstance(lease["acquired_boot_id"], str)
                or not isinstance(lease["acquired_monotonic"], (int, float))
                or isinstance(lease["acquired_monotonic"], bool)
            ):
                raise ContractError("receiver operation lease binding is invalid")
        return value

    def _save(self, value: dict[str, Any]) -> dict[str, Any]:
        value["control_id"] = _identity(value, "control_id")
        atomic_document(self.path, value)
        return value

    def issue_nonce(
        self,
        *,
        operation_id: str,
        client_id: str,
        plan_id: str,
    ) -> tuple[str, dict[str, Any]]:
        value = self.load()
        now = self.monotonic()
        boot = self.boot_id()
        nonce = secrets.token_hex(32)
        nonce_hash = hashlib.sha256(bytes.fromhex(nonce)).hexdigest()
        value["nonces"][nonce_hash] = {
            "operation_id": operation_id,
            "client_id": client_id,
            "plan_id": plan_id,
            "issued_boot_id": boot,
            "issued_monotonic": now,
            "expires_monotonic": now + self.nonce_expiry_s,
            "consumed": False,
        }
        self._prune_nonces(value, now=now, boot=boot)
        return nonce, self._save(value)

    @staticmethod
    def _prune_nonces(value: dict[str, Any], *, now: float, boot: str) -> None:
        value["nonces"] = {
            key: record
            for key, record in value["nonces"].items()
            if record["issued_boot_id"] == boot
            and (not record["consumed"] or now - record["issued_monotonic"] <= 3600)
            and now <= record["expires_monotonic"] + 3600
        }

    def consume_and_acquire(
        self,
        *,
        nonce: str,
        operation_id: str,
        client_id: str,
        action: Action,
        plan_id: str,
    ) -> dict[str, Any]:
        if not IDENTITY.fullmatch(nonce):
            raise ContractError("receiver nonce is malformed")
        value = self.load()
        lease = value["lease"]
        if lease is not None:
            if lease["operation_id"] == operation_id and lease["client_id"] == client_id:
                raise ContractError("receiver operation is already durably accepted")
            raise ContractError(
                "receiver mutation lease is held by "
                f"operation {lease['operation_id']} for client {lease['client_id']}"
            )
        nonce_hash = hashlib.sha256(bytes.fromhex(nonce)).hexdigest()
        record = value["nonces"].get(nonce_hash)
        now = self.monotonic()
        boot = self.boot_id()
        if record is None:
            raise ContractError("receiver nonce is unknown or expired")
        if record["consumed"]:
            raise ContractError("receiver nonce was already consumed")
        if record["issued_boot_id"] != boot or now > record["expires_monotonic"]:
            raise ContractError("receiver nonce expired before mutation lock acquisition")
        expected = (operation_id, client_id, plan_id)
        observed = (record["operation_id"], record["client_id"], record["plan_id"])
        if observed != expected:
            raise ContractError("receiver nonce is bound to another operation, client, or plan")
        record["consumed"] = True
        value["lease"] = {
            "operation_id": operation_id,
            "client_id": client_id,
            "action": action.value,
            "plan_id": plan_id,
            "acquired_boot_id": boot,
            "acquired_monotonic": now,
        }
        return self._save(value)

    def release(self, operation_id: str) -> dict[str, Any]:
        value = self.load()
        lease = value["lease"]
        if lease is None:
            return value
        if lease["operation_id"] != operation_id:
            raise ContractError("receiver lease release operation ID mismatch")
        value["lease"] = None
        return self._save(value)

    def recover_stale_lease(self, operation_id: str) -> dict[str, Any]:
        """Release only a lease whose owning journal is terminal or absent."""

        return self.release(operation_id)


@dataclass
class OperationJournalStore:
    root: Path
    monotonic: Callable[[], float]
    boot_id: Callable[[], str] = read_boot_id

    def path(self, operation_id: str) -> Path:
        if not OPERATION_ID.fullmatch(operation_id):
            raise ContractError("invalid receiver operation ID")
        return self.root / "operations" / f"{operation_id}.json"

    def load(self, operation_id: str) -> dict[str, Any] | None:
        path = self.path(operation_id)
        if not path.exists() and not path.is_symlink():
            return None
        value = _canonical_document(path, label="receiver operation journal")
        self._validate(value)
        return value

    def list(self) -> list[dict[str, Any]]:
        directory = self.root / "operations"
        if not directory.exists() and not directory.is_symlink():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ContractError("receiver operation journal directory is linked or invalid")
        values: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ContractError("receiver operation journal directory contains an unsafe entry")
            operation_id = path.stem
            value = self.load(operation_id)
            assert value is not None
            values.append(value)
        return values

    def create(
        self,
        *,
        plan: Mapping[str, Any],
        target_acceptance_s: int = 60,
        hard_deadline_s: int = 120,
        rollback_target_s: int = 60,
    ) -> dict[str, Any]:
        validate_mutation_plan(plan)
        if self.load(plan["operation_id"]) is not None:
            raise ContractError("receiver operation ID already has a durable journal")
        if (target_acceptance_s, hard_deadline_s, rollback_target_s) != (60, 120, 60):
            raise ContractError("receiver deadlines differ from the fixed 60/120/60 policy")
        boot = self.boot_id()
        now = self.monotonic()
        value: dict[str, Any] = {
            "schema": JOURNAL_SCHEMA,
            "journal_id": "",
            "operation_id": plan["operation_id"],
            "client_id": plan["client_id"],
            "action": plan["action"],
            "plan": dict(plan),
            "state": "accepted",
            "checkpoint": "accepted",
            "sequence": 1,
            "accepted_boot_id": boot,
            "accepted_monotonic": now,
            "deadlines": {
                "target_acceptance_s": target_acceptance_s,
                "hard_deadline_s": hard_deadline_s,
                "rollback_target_s": rollback_target_s,
            },
            "remaining_hard_s": float(hard_deadline_s),
            "budget_boot_id": boot,
            "budget_monotonic": now,
            "cancellation_safe": True,
            "cancel_requested": False,
            "events": [
                {
                    "sequence": 1,
                    "event": "accepted",
                    "checkpoint": "accepted",
                    "boot_id": boot,
                    "monotonic": now,
                    "evidence_hash": None,
                }
            ],
            "result": None,
            "failure": None,
        }
        value["journal_id"] = _identity(value, "journal_id")
        self._validate(value)
        atomic_document(self.path(plan["operation_id"]), value)
        return value

    def transition(
        self,
        operation_id: str,
        *,
        state: str,
        checkpoint: str,
        cancellation_safe: bool,
        event: str,
        evidence_hash: str | None = None,
        result: Mapping[str, Any] | None = None,
        failure: Mapping[str, Any] | None = None,
        cancel_requested: bool | None = None,
    ) -> dict[str, Any]:
        value = self.load(operation_id)
        if value is None:
            raise ContractError("receiver operation journal is missing")
        if value["state"] in TERMINAL_STATES:
            raise ContractError("receiver operation journal is already terminal")
        allowed = {
            "accepted": {"running", "cancel-requested", "cancelled", "failed"},
            "cancel-requested": {"cancelled", "failed"},
            "running": {"completed", "failed"},
        }
        if state not in allowed[value["state"]]:
            raise ContractError(
                f"invalid receiver journal transition {value['state']} -> {state}"
            )
        self._charge_budget(value)
        value["sequence"] += 1
        value["state"] = state
        value["checkpoint"] = checkpoint
        value["cancellation_safe"] = cancellation_safe
        if cancel_requested is not None:
            value["cancel_requested"] = cancel_requested
        value["result"] = None if result is None else dict(result)
        value["failure"] = None if failure is None else dict(failure)
        value["events"].append(
            {
                "sequence": value["sequence"],
                "event": event,
                "checkpoint": checkpoint,
                "boot_id": self.boot_id(),
                "monotonic": self.monotonic(),
                "evidence_hash": evidence_hash,
            }
        )
        value["journal_id"] = _identity(value, "journal_id")
        self._validate(value)
        atomic_document(self.path(operation_id), value)
        return value

    def remaining_budget(self, operation_id: str) -> float:
        value = self.load(operation_id)
        if value is None:
            raise ContractError("receiver operation journal is missing")
        self._charge_budget(value)
        return float(value["remaining_hard_s"])

    def _charge_budget(self, value: dict[str, Any]) -> None:
        boot = self.boot_id()
        now = self.monotonic()
        if value["budget_boot_id"] == boot:
            elapsed = max(0.0, now - float(value["budget_monotonic"]))
            value["remaining_hard_s"] = max(
                0.0, float(value["remaining_hard_s"]) - elapsed
            )
        value["budget_boot_id"] = boot
        value["budget_monotonic"] = now

    def request_cancel(self, operation_id: str) -> dict[str, Any]:
        value = self.load(operation_id)
        if value is None:
            raise ContractError("receiver cancellation target is unknown")
        if value["state"] in TERMINAL_STATES:
            return value
        if not value["cancellation_safe"]:
            raise ContractError("receiver operation has passed its cancellation-safe checkpoint")
        return self.transition(
            operation_id,
            state="cancel-requested",
            checkpoint=value["checkpoint"],
            cancellation_safe=True,
            event="cancel-requested",
            cancel_requested=True,
        )

    def _validate(self, value: Mapping[str, Any]) -> None:
        required = {
            "schema",
            "journal_id",
            "operation_id",
            "client_id",
            "action",
            "plan",
            "state",
            "checkpoint",
            "sequence",
            "accepted_boot_id",
            "accepted_monotonic",
            "deadlines",
            "remaining_hard_s",
            "budget_boot_id",
            "budget_monotonic",
            "cancellation_safe",
            "cancel_requested",
            "events",
            "result",
            "failure",
        }
        if set(value) != required or value.get("schema") != JOURNAL_SCHEMA:
            raise ContractError("receiver operation journal fields are malformed")
        if value.get("journal_id") != _identity(value, "journal_id"):
            raise ContractError("receiver operation journal identity mismatch")
        if not OPERATION_ID.fullmatch(str(value["operation_id"])):
            raise ContractError("receiver operation journal has invalid operation ID")
        if not IDENTITY.fullmatch(str(value["client_id"])):
            raise ContractError("receiver operation journal has invalid client ID")
        validate_mutation_plan(
            value["plan"],
            operation_id=value["operation_id"],
            client_id=value["client_id"],
        )
        if value["action"] != value["plan"]["action"]:
            raise ContractError("receiver journal action differs from retained plan")
        if value["state"] not in {
            "accepted",
            "running",
            "cancel-requested",
            "cancelled",
            "completed",
            "failed",
        }:
            raise ContractError("receiver operation journal has invalid state")
        if not isinstance(value["sequence"], int) or value["sequence"] < 1:
            raise ContractError("receiver operation journal sequence is invalid")
        if value["deadlines"] != {
            "target_acceptance_s": 60,
            "hard_deadline_s": 120,
            "rollback_target_s": 60,
        }:
            raise ContractError("receiver operation journal deadlines are invalid")
        if (
            not isinstance(value["remaining_hard_s"], (int, float))
            or isinstance(value["remaining_hard_s"], bool)
            or not 0 <= value["remaining_hard_s"] <= 120
        ):
            raise ContractError("receiver operation remaining deadline is invalid")
        if not isinstance(value["accepted_boot_id"], str) or not isinstance(
            value["budget_boot_id"], str
        ):
            raise ContractError("receiver operation boot binding is invalid")
        if not all(
            isinstance(value[field], (int, float)) and not isinstance(value[field], bool)
            for field in ("accepted_monotonic", "budget_monotonic")
        ):
            raise ContractError("receiver operation monotonic clock is invalid")
        if len(value["events"]) != value["sequence"]:
            raise ContractError("receiver operation journal event sequence is incomplete")
        for index, event in enumerate(value["events"], start=1):
            if not isinstance(event, dict) or set(event) != {
                "sequence",
                "event",
                "checkpoint",
                "boot_id",
                "monotonic",
                "evidence_hash",
            }:
                raise ContractError("receiver operation journal event fields are malformed")
            if event.get("sequence") != index:
                raise ContractError("receiver operation journal events are not contiguous")
            if not isinstance(event["event"], str) or not isinstance(event["checkpoint"], str):
                raise ContractError("receiver operation journal event labels are invalid")
            if not isinstance(event["boot_id"], str) or not isinstance(
                event["monotonic"], (int, float)
            ):
                raise ContractError("receiver operation journal event clock is invalid")
            if event["evidence_hash"] is not None and not IDENTITY.fullmatch(
                str(event["evidence_hash"])
            ):
                raise ContractError("receiver operation journal event evidence is invalid")
        if value["events"][-1]["checkpoint"] != value["checkpoint"]:
            raise ContractError("receiver journal checkpoint differs from latest event")
        if not isinstance(value["cancellation_safe"], bool) or not isinstance(
            value["cancel_requested"], bool
        ):
            raise ContractError("receiver journal cancellation state is malformed")
        if value["state"] in {"accepted", "running", "cancel-requested"} and (
            value["result"] is not None or value["failure"] is not None
        ):
            raise ContractError("nonterminal receiver journal contains a terminal outcome")
        if value["state"] == "completed" and (
            not isinstance(value["result"], dict) or value["failure"] is not None
        ):
            raise ContractError("completed receiver journal lacks one result")
        if value["state"] == "failed" and (
            value["result"] is not None
            or not isinstance(value["failure"], dict)
            or set(value["failure"]) != {"code", "message"}
        ):
            raise ContractError("failed receiver journal lacks one fixed failure")
        if value["state"] == "cancelled" and (
            value["result"] is not None or value["failure"] is not None
        ):
            raise ContractError("cancelled receiver journal contains an outcome")


@dataclass
class AuditLog:
    path: Path
    monotonic: Callable[[], float]
    boot_id: Callable[[], str] = read_boot_id

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists() and not self.path.is_symlink():
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise ContractError("receiver audit path is linked or not a file")
        entries: list[dict[str, Any]] = []
        previous: str | None = None
        try:
            lines = self.path.read_bytes().splitlines(keepends=True)
        except OSError as exc:
            raise ContractError(f"cannot read receiver audit log: {exc}") from exc
        for line in lines:
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError(f"receiver audit log is malformed: {exc}") from exc
            if line != canonical_json(value) + b"\n" or not isinstance(value, dict):
                raise ContractError("receiver audit entry is not canonical JSON")
            if set(value) != {
                "schema",
                "event_id",
                "sequence",
                "previous_event_id",
                "boot_id",
                "monotonic",
                "event",
                "outcome",
                "operation_id",
                "client_id",
                "action",
                "detail_code",
                "evidence_hash",
            }:
                raise ContractError("receiver audit entry fields are malformed")
            if value.get("schema") != AUDIT_SCHEMA or value.get("previous_event_id") != previous:
                raise ContractError("receiver audit chain is discontinuous")
            if value.get("event_id") != _identity(value, "event_id"):
                raise ContractError("receiver audit event identity mismatch")
            if value.get("sequence") != len(entries) + 1:
                raise ContractError("receiver audit sequence is not contiguous")
            if value["evidence_hash"] is not None and not IDENTITY.fullmatch(
                str(value["evidence_hash"])
            ):
                raise ContractError("receiver audit evidence identity is invalid")
            previous = value["event_id"]
            entries.append(value)
        return entries

    def append(
        self,
        *,
        event: str,
        outcome: str,
        operation_id: str | None,
        client_id: str | None,
        action: str | None,
        detail_code: str,
        evidence_hash: str | None = None,
    ) -> dict[str, Any]:
        if evidence_hash is not None and not IDENTITY.fullmatch(evidence_hash):
            raise ContractError("receiver audit evidence hash is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o640,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            entries = self._load()
            value: dict[str, Any] = {
                "schema": AUDIT_SCHEMA,
                "event_id": "",
                "sequence": len(entries) + 1,
                "previous_event_id": entries[-1]["event_id"] if entries else None,
                "boot_id": self.boot_id(),
                "monotonic": self.monotonic(),
                "event": event,
                "outcome": outcome,
                "operation_id": operation_id,
                "client_id": client_id,
                "action": action,
                "detail_code": detail_code,
                "evidence_hash": evidence_hash,
            }
            value["event_id"] = _identity(value, "event_id")
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o640,
            )
            try:
                with os.fdopen(descriptor, "ab", closefd=False) as stream:
                    stream.write(canonical_json(value) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            _fsync_directory(self.path.parent)
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
        return value

    def entries(self) -> list[dict[str, Any]]:
        return self._load()
