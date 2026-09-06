"""Receiver-owned boot clock gate and narrow privileged synchronization."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.state import atomic_document, read_boot_id

CLOCK_STATE_SCHEMA = "iii.receiver-clock-state/v1"
CLOCK_FLUSH_SCHEMA = "iii.clock-flush-commit/v1"
# Historical, already-journalled plans may retain the prior 2 s transport
# envelope.  New samples are constrained to the operational 500 ms policy by
# the client before planning; this broader replay bound prevents an update from
# refusing to start solely because it is reconciling immutable old evidence.
MAX_RTT_NS = 2_000_000_000
MAX_SAMPLE_SPREAD_NS = 250_000_000
MAX_COMMIT_OFFSET_NS = 250_000_000
ACTIVE_RESYNC_OFFSET_NS = 1_000_000_000
BACKWARD_DISCONTINUITY_NS = 250_000_000
FORWARD_DISCONTINUITY_NS = 2_000_000_000


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


class ClockFlushCoordinator:
    """Wait for exact process-local pre-clock rings to commit durably."""

    def __init__(
        self,
        root: Path = Path("/run/iii/clock-flush"),
        *,
        required_services: Callable[[], Sequence[str]] = lambda: (
            "system-daemon",
            "runtime-api",
        ),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 15.0,
    ) -> None:
        self.root = root
        self.required_services = required_services
        self.monotonic = monotonic
        self.sleep = sleep
        self.timeout_s = timeout_s

    def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        services = tuple(sorted(set(self.required_services())))
        if any(
            service not in {"runtime-api", "system-daemon"}
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", service) is None
            for service in services
        ):
            raise ContractError("clock flush service inventory is invalid")
        deadline = self.monotonic() + self.timeout_s
        commits: dict[str, str] = {}
        while self.monotonic() < deadline:
            commits.clear()
            for service in services:
                path = self.root / f"{service}.json"
                try:
                    value = _canonical_document(
                        path, label=f"{service} clock flush commit"
                    )
                except ContractError:
                    break
                expected = content_identity(
                    {key: item for key, item in value.items() if key != "commit_id"}
                )
                if (
                    set(value)
                    != {
                        "schema",
                        "commit_id",
                        "service",
                        "boot_id",
                        "clock_state_id",
                        "records_flushed",
                        "dropped_records",
                        "committed_monotonic_ns",
                    }
                    or value.get("schema") != CLOCK_FLUSH_SCHEMA
                    or value.get("commit_id") != expected
                    or value.get("service") != service
                    or value.get("boot_id") != state["boot_id"]
                    or value.get("clock_state_id") != state["state_id"]
                    or not isinstance(value.get("records_flushed"), int)
                    or isinstance(value.get("records_flushed"), bool)
                    or value["records_flushed"] < 0
                    or not isinstance(value.get("dropped_records"), int)
                    or isinstance(value.get("dropped_records"), bool)
                    or value["dropped_records"] < 0
                    or not isinstance(value.get("committed_monotonic_ns"), int)
                    or isinstance(value.get("committed_monotonic_ns"), bool)
                    or value["committed_monotonic_ns"] < 0
                ):
                    break
                commits[service] = value["commit_id"]
            if len(commits) == len(services):
                return {
                    "schema": "iii.clock-flush-barrier/v1",
                    "clock_state_id": state["state_id"],
                    "commits": commits,
                }
            self.sleep(0.05)
        missing = sorted(set(services) - set(commits))
        raise ContractError("pre-clock log flush did not commit: " + ", ".join(missing))


class ClockRecoveryAudit:
    """Append the minimal time-untrusted recovery trail allowed before trust."""

    def __init__(self, path: Path = Path("/var/log/iii/deployment/clock-audit.jsonl")):
        self.path = path

    def __call__(self, event: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.path.is_symlink():
            raise ContractError("clock recovery audit is linked")
        row = {
            "schema": "iii.clock-recovery-audit/v1",
            "time_trusted": False,
            **dict(event),
        }
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o640,
        )
        try:
            raw = canonical_json(row) + b"\n"
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("clock recovery audit append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass
class ClockController:
    state_path: Path
    boot_id: Callable[[], str] = read_boot_id
    monotonic_ns: Callable[[], int] = time.monotonic_ns
    wall_ns: Callable[[], int] = time.time_ns
    set_wall_ns: Callable[[int], None] | None = None
    flush_before_open: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    gate_opened: Callable[[], Mapping[str, Any] | None] | None = None
    maintenance_safe: Callable[[], bool] | None = None
    stop_runtime: Callable[[], None] | None = None
    audit: Callable[[Mapping[str, Any]], None] | None = None

    def _initial(self, *, start_required: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": CLOCK_STATE_SCHEMA,
            "state_id": "0" * 64,
            "boot_id": self.boot_id(),
            "gate": "DEGRADED_CLOCK",
            "synchronized_monotonic_ns": None,
            "synchronized_utc_ns": None,
            "uncertainty_ns": None,
            "verified_offset_ns": None,
            "operation_id": None,
            "flush_barrier": None,
            "start_required": start_required,
            "fault": None,
        }
        value["state_id"] = content_identity(
            {key: item for key, item in value.items() if key != "state_id"}
        )
        return value

    @staticmethod
    def _with_identity(value: Mapping[str, Any]) -> dict[str, Any]:
        committed = dict(value)
        committed["state_id"] = "0" * 64
        committed["state_id"] = content_identity(
            {key: item for key, item in committed.items() if key != "state_id"}
        )
        return committed

    def _write(self, value: Mapping[str, Any]) -> dict[str, Any]:
        committed = self._with_identity(value)
        atomic_document(self.state_path, committed)
        return committed

    def _audit(self, *, event: str, state: Mapping[str, Any]) -> None:
        if self.audit is not None:
            self.audit(
                {
                    "event": event,
                    "boot_id": self.boot_id(),
                    "monotonic_ns": self.monotonic_ns(),
                    "gate": state["gate"],
                    "state_id": state["state_id"],
                    "operation_id": state.get("operation_id"),
                }
            )

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return self._initial()
        value = _canonical_document(self.state_path, label="receiver clock state")
        if value.get("schema") != CLOCK_STATE_SCHEMA:
            raise ContractError("receiver clock-state schema is unsupported")
        expected = content_identity(
            {key: item for key, item in value.items() if key != "state_id"}
        )
        if value.get("state_id") != expected:
            raise ContractError("receiver clock-state identity mismatch")
        if value.get("boot_id") != self.boot_id():
            return self._initial()
        if set(value) != {
            "schema",
            "state_id",
            "boot_id",
            "gate",
            "synchronized_monotonic_ns",
            "synchronized_utc_ns",
            "uncertainty_ns",
            "verified_offset_ns",
            "operation_id",
            "flush_barrier",
            "start_required",
            "fault",
        }:
            raise ContractError("receiver clock state fields are invalid")
        if value.get("gate") not in {
            "DEGRADED_CLOCK",
            "FLUSHING_CLOCK",
            "OPERATIONAL",
            "CLOCK_FAULT_ACTIVE",
        }:
            raise ContractError("receiver clock gate is invalid")
        if not isinstance(value.get("start_required"), bool):
            raise ContractError("receiver clock restart requirement is invalid")
        if value.get("operation_id") is not None and not isinstance(
            value.get("operation_id"), str
        ):
            raise ContractError("receiver clock operation identity is invalid")
        for field in (
            "synchronized_monotonic_ns",
            "synchronized_utc_ns",
            "uncertainty_ns",
        ):
            field_value = value.get(field)
            if field_value is not None and (
                not isinstance(field_value, int)
                or isinstance(field_value, bool)
                or field_value < 0
            ):
                raise ContractError(f"receiver clock {field} is invalid")
        verified_offset = value.get("verified_offset_ns")
        if verified_offset is not None and (
            not isinstance(verified_offset, int) or isinstance(verified_offset, bool)
        ):
            raise ContractError("receiver clock verified offset is invalid")
        gate = value["gate"]
        mapping_fields = (
            value["synchronized_monotonic_ns"],
            value["synchronized_utc_ns"],
            value["uncertainty_ns"],
            value["verified_offset_ns"],
        )
        if gate == "DEGRADED_CLOCK" and (
            any(item is not None for item in mapping_fields)
            or value["flush_barrier"] is not None
            or value["fault"] is not None
        ):
            raise ContractError("degraded clock state retains trusted clock data")
        if gate != "DEGRADED_CLOCK" and any(item is None for item in mapping_fields):
            raise ContractError("trusted clock state has no complete mapping")
        if gate == "FLUSHING_CLOCK" and value["flush_barrier"] is not None:
            raise ContractError("flushing clock state already carries a barrier")
        if gate == "OPERATIONAL" and not isinstance(value["flush_barrier"], dict):
            raise ContractError("operational clock state has no flush barrier")
        fault = value["fault"]
        if gate == "CLOCK_FAULT_ACTIVE":
            if (
                not isinstance(fault, dict)
                or set(fault) != {"direction", "delta_ns"}
                or fault.get("direction") not in {"backward", "forward", "unknown"}
                or not isinstance(fault.get("delta_ns"), int)
                or isinstance(fault.get("delta_ns"), bool)
            ):
                raise ContractError("active clock fault evidence is invalid")
        elif fault is not None:
            raise ContractError("non-fault clock state carries fault evidence")
        return value

    def _discontinuity(self, state: Mapping[str, Any]) -> tuple[str, int] | None:
        if state["gate"] not in {"OPERATIONAL", "CLOCK_FAULT_ACTIVE"}:
            return None
        synchronized_monotonic = state.get("synchronized_monotonic_ns")
        synchronized_utc = state.get("synchronized_utc_ns")
        if not isinstance(synchronized_monotonic, int) or not isinstance(
            synchronized_utc, int
        ):
            raise ContractError("operational clock state has no trusted mapping")
        expected = synchronized_utc + (self.monotonic_ns() - synchronized_monotonic)
        delta = self.wall_ns() - expected
        if delta < -BACKWARD_DISCONTINUITY_NS:
            return "backward", delta
        if delta > FORWARD_DISCONTINUITY_NS:
            return "forward", delta
        return None

    def evaluate_discontinuity(self) -> dict[str, Any]:
        state = self.load()
        observed = self._discontinuity(state)
        if observed is None and state["gate"] != "CLOCK_FAULT_ACTIVE":
            return state
        safe = self.maintenance_safe() if self.maintenance_safe is not None else False
        if state["gate"] == "CLOCK_FAULT_ACTIVE" and not safe:
            return state
        direction, delta = observed or (
            state.get("fault", {}).get("direction", "unknown"),
            state.get("fault", {}).get("delta_ns", 0),
        )
        if safe:
            if self.stop_runtime is None:
                raise ContractError(
                    "maintenance-safe clock recovery cannot stop the runtime graph"
                )
            self.stop_runtime()
            degraded = self._write(self._initial(start_required=True))
            self._audit(event="clock-discontinuity-degraded", state=degraded)
            return degraded
        fault = self._write(
            {
                **state,
                "gate": "CLOCK_FAULT_ACTIVE",
                "fault": {"direction": direction, "delta_ns": delta},
                "start_required": True,
            }
        )
        self._audit(event="clock-discontinuity-active", state=fault)
        return fault

    def status(self) -> dict[str, Any]:
        state = self.evaluate_discontinuity()
        return {
            "schema": "iii.receiver-clock-status/v1",
            "boot_id": self.boot_id(),
            "gate": state["gate"],
            "target_monotonic_ns": self.monotonic_ns(),
            "target_wall_ns": self.wall_ns(),
            "state_id": state["state_id"],
            "start_required": bool(state.get("start_required", False)),
            "fault": state.get("fault"),
        }

    @staticmethod
    def validate_samples(samples: Sequence[Mapping[str, Any]]) -> None:
        if len(samples) < 5:
            raise ContractError("clock synchronization requires at least five samples")
        boot_ids = set()
        offsets: list[int] = []
        monotonic_values: list[int] = []
        for sample in samples:
            if set(sample) != {
                "target_boot_id",
                "target_monotonic_ns",
                "target_wall_ns",
                "operator_midpoint_utc_ns",
                "rtt_ns",
                "offset_ns",
            }:
                raise ContractError("clock sample fields are invalid")
            if (
                not isinstance(sample["target_boot_id"], str)
                or not sample["target_boot_id"]
            ):
                raise ContractError("clock sample boot identity is invalid")
            boot_ids.add(sample["target_boot_id"])
            for field in (
                "target_monotonic_ns",
                "target_wall_ns",
                "operator_midpoint_utc_ns",
                "rtt_ns",
                "offset_ns",
            ):
                if not isinstance(sample[field], int) or isinstance(
                    sample[field], bool
                ):
                    raise ContractError(f"clock sample {field} is invalid")
            if sample["rtt_ns"] < 0 or sample["rtt_ns"] > MAX_RTT_NS:
                raise ContractError("clock synchronization sample RTT exceeds 2 seconds")
            observed = sample["target_wall_ns"] - sample["operator_midpoint_utc_ns"]
            if sample["offset_ns"] != observed:
                raise ContractError(
                    "clock synchronization sample offset is inconsistent"
                )
            offsets.append(sample["offset_ns"])
            monotonic_values.append(sample["target_monotonic_ns"])
        if len(boot_ids) != 1:
            raise ContractError("clock samples span multiple target boots")
        if monotonic_values != sorted(monotonic_values) or len(
            set(monotonic_values)
        ) != len(monotonic_values):
            raise ContractError(
                "clock synchronization samples are not settled in order"
            )
        if max(offsets) - min(offsets) > MAX_SAMPLE_SPREAD_NS:
            raise ContractError("clock synchronization samples are not settled")

    def synchronize(
        self,
        *,
        operation_id: str,
        samples: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self.validate_samples(samples)
        state = self.evaluate_discontinuity()
        if {item["target_boot_id"] for item in samples} != {self.boot_id()}:
            raise ContractError("clock samples belong to another target boot")
        selected = min(
            samples, key=lambda item: (item["rtt_ns"], item["target_monotonic_ns"])
        )
        now_monotonic = self.monotonic_ns()
        desired_wall = selected["operator_midpoint_utc_ns"] + (
            now_monotonic - selected["target_monotonic_ns"]
        )
        current_offset = self.wall_ns() - desired_wall
        if state["gate"] in {"OPERATIONAL", "CLOCK_FAULT_ACTIVE"}:
            return {
                "kind": "clock-sync",
                "mode": "measure-only",
                "gate": state["gate"],
                "offset_ns": current_offset,
                "warning": abs(current_offset) > MAX_COMMIT_OFFSET_NS,
                "resync_required": abs(current_offset) > ACTIVE_RESYNC_OFFSET_NS,
                "state_id": state["state_id"],
            }
        if state["gate"] == "FLUSHING_CLOCK":
            if state.get("operation_id") != operation_id:
                raise ContractError(
                    "clock flush can resume only for its retained operation"
                )
            flushing = state
        else:
            setter = self.set_wall_ns or (
                lambda value: time.clock_settime_ns(time.CLOCK_REALTIME, value)
            )
            setter(desired_wall)
            synchronized_monotonic_ns = self.monotonic_ns()
            synchronized_utc_ns = selected["operator_midpoint_utc_ns"] + (
                synchronized_monotonic_ns - selected["target_monotonic_ns"]
            )
            verified_offset = self.wall_ns() - synchronized_utc_ns
            if abs(verified_offset) > MAX_COMMIT_OFFSET_NS:
                raise ContractError("clock remained more than 250 ms from operator UTC")
            flushing = self._write(
                {
                    "schema": CLOCK_STATE_SCHEMA,
                    "state_id": "0" * 64,
                    "boot_id": self.boot_id(),
                    "gate": "FLUSHING_CLOCK",
                    "synchronized_monotonic_ns": synchronized_monotonic_ns,
                    "synchronized_utc_ns": synchronized_utc_ns,
                    "uncertainty_ns": selected["rtt_ns"] // 2 + abs(verified_offset),
                    "verified_offset_ns": verified_offset,
                    "operation_id": operation_id,
                    "flush_barrier": None,
                    "start_required": bool(state.get("start_required", False)),
                    "fault": None,
                }
            )
            self._audit(event="clock-flush-started", state=flushing)
        barrier = (
            self.flush_before_open(flushing)
            if self.flush_before_open is not None
            else {
                "schema": "iii.clock-flush-barrier/v1",
                "clock_state_id": flushing["state_id"],
                "commits": {},
            }
        )
        if barrier.get("clock_state_id") != flushing["state_id"]:
            raise ContractError("pre-clock flush barrier is bound to another state")
        expected_wall_ns = int(flushing["synchronized_utc_ns"]) + (
            self.monotonic_ns() - int(flushing["synchronized_monotonic_ns"])
        )
        verified_offset = self.wall_ns() - expected_wall_ns
        if abs(verified_offset) > MAX_COMMIT_OFFSET_NS:
            raise ContractError(
                "clock moved more than 250 ms from trusted UTC during log flush"
            )
        committed = self._write(
            {
                **flushing,
                "gate": "OPERATIONAL",
                "verified_offset_ns": verified_offset,
                "flush_barrier": dict(barrier),
                "fault": None,
            }
        )
        self._audit(event="clock-gate-opened", state=committed)
        startup = None
        if not committed.get("start_required") and self.gate_opened is not None:
            startup = self.gate_opened()
        return {
            "kind": "clock-sync",
            "mode": "gate-open",
            "gate": "OPERATIONAL",
            "offset_ns": verified_offset,
            "state_id": committed["state_id"],
            "flush_barrier": barrier,
            "start_required": bool(committed.get("start_required", False)),
            "runtime_start": startup,
        }
