"""Receiver-owned boot clock gate and narrow privileged synchronization."""

from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from iii_deployment.contracts import ContractError, content_identity
from iii_deployment.receiver.state import atomic_document, read_boot_id

CLOCK_STATE_SCHEMA = "iii.receiver-clock-state/v1"
MAX_RTT_NS = 500_000_000
MAX_COMMIT_OFFSET_NS = 250_000_000


@dataclass
class ClockController:
    state_path: Path
    boot_id: Callable[[], str] = read_boot_id
    monotonic_ns: Callable[[], int] = time.monotonic_ns
    wall_ns: Callable[[], int] = time.time_ns
    set_wall_ns: Callable[[int], None] | None = None
    gate_opened: Callable[[], Mapping[str, Any] | None] | None = None

    def _initial(self) -> dict[str, Any]:
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
        }
        value["state_id"] = content_identity(
            {key: item for key, item in value.items() if key != "state_id"}
        )
        return value

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return self._initial()
        from iii_deployment.receiver.state import _canonical_document

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
        if value.get("gate") not in {
            "DEGRADED_CLOCK",
            "OPERATIONAL",
            "CLOCK_FAULT_ACTIVE",
        }:
            raise ContractError("receiver clock gate is invalid")
        return value

    def status(self) -> dict[str, Any]:
        state = self.load()
        return {
            "schema": "iii.receiver-clock-status/v1",
            "boot_id": self.boot_id(),
            "gate": state["gate"],
            "target_monotonic_ns": self.monotonic_ns(),
            "target_wall_ns": self.wall_ns(),
            "state_id": state["state_id"],
        }

    @staticmethod
    def validate_samples(samples: Sequence[Mapping[str, Any]]) -> None:
        if len(samples) < 5:
            raise ContractError("clock synchronization requires at least five samples")
        boot_ids = set()
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
                raise ContractError("clock synchronization sample RTT exceeds 500 ms")
            observed = sample["target_wall_ns"] - sample["operator_midpoint_utc_ns"]
            if sample["offset_ns"] != observed:
                raise ContractError(
                    "clock synchronization sample offset is inconsistent"
                )
        if len(boot_ids) != 1:
            raise ContractError("clock samples span multiple target boots")

    def synchronize(
        self,
        *,
        operation_id: str,
        samples: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self.validate_samples(samples)
        state = self.load()
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
        if state["gate"] == "OPERATIONAL":
            return {
                "kind": "clock-sync",
                "mode": "measure-only",
                "gate": "OPERATIONAL",
                "offset_ns": current_offset,
                "warning": abs(current_offset) > MAX_COMMIT_OFFSET_NS,
                "state_id": state["state_id"],
            }
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
        committed: dict[str, Any] = {
            "schema": CLOCK_STATE_SCHEMA,
            "state_id": "0" * 64,
            "boot_id": self.boot_id(),
            "gate": "OPERATIONAL",
            "synchronized_monotonic_ns": synchronized_monotonic_ns,
            "synchronized_utc_ns": synchronized_utc_ns,
            "uncertainty_ns": selected["rtt_ns"] // 2 + abs(verified_offset),
            "verified_offset_ns": verified_offset,
            "operation_id": operation_id,
        }
        committed["state_id"] = content_identity(
            {key: item for key, item in committed.items() if key != "state_id"}
        )
        atomic_document(self.state_path, committed)
        startup = self.gate_opened() if self.gate_opened is not None else None
        return {
            "kind": "clock-sync",
            "mode": "gate-open",
            "gate": "OPERATIONAL",
            "offset_ns": verified_offset,
            "state_id": committed["state_id"],
            "runtime_start": startup,
        }
