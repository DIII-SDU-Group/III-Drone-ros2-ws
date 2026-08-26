from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.receiver.clock import ClockController


class FakeClock:
    def __init__(self):
        self.boot = "boot-a"
        self.monotonic_value = 10_000_000_000
        self.wall_value = 1_000_000_000

    def monotonic_ns(self):
        return self.monotonic_value

    def wall_ns(self):
        return self.wall_value

    def set_wall_ns(self, value):
        self.wall_value = value


def samples(clock: FakeClock, *, count: int = 5, rtt_ns: int = 10_000_000):
    operator = 2_000_000_000
    return [
        {
            "target_boot_id": clock.boot,
            "target_monotonic_ns": clock.monotonic_value,
            "target_wall_ns": clock.wall_value,
            "operator_midpoint_utc_ns": operator,
            "rtt_ns": rtt_ns + index,
            "offset_ns": clock.wall_value - operator,
        }
        for index in range(count)
    ]


def controller(tmp_path: Path, clock: FakeClock, starts: list):
    return ClockController(
        tmp_path / "clock-state.json",
        boot_id=lambda: clock.boot,
        monotonic_ns=clock.monotonic_ns,
        wall_ns=clock.wall_ns,
        set_wall_ns=clock.set_wall_ns,
        gate_opened=lambda: starts.append("runtime") or {"started": True},
    )


def test_boot_starts_degraded_and_five_settled_samples_open_gate(tmp_path: Path):
    clock = FakeClock()
    starts = []
    gate = controller(tmp_path, clock, starts)
    assert gate.status()["gate"] == "DEGRADED_CLOCK"
    result = gate.synchronize(
        operation_id="clock-operation-0001", samples=samples(clock)
    )
    assert result["gate"] == "OPERATIONAL"
    assert abs(result["offset_ns"]) <= 250_000_000
    assert starts == ["runtime"]
    assert gate.status()["gate"] == "OPERATIONAL"


def test_high_rtt_or_too_few_samples_fail_closed(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    with pytest.raises(ContractError, match="five samples"):
        gate.synchronize(
            operation_id="clock-operation-0001", samples=samples(clock, count=4)
        )
    with pytest.raises(ContractError, match="500 ms"):
        gate.synchronize(
            operation_id="clock-operation-0001",
            samples=samples(clock, rtt_ns=500_000_001),
        )


def test_new_boot_reenters_degraded_clock(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    gate.synchronize(operation_id="clock-operation-0001", samples=samples(clock))
    clock.boot = "boot-b"
    assert gate.status()["gate"] == "DEGRADED_CLOCK"


def test_operational_manual_sync_is_measure_only_and_never_steps(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    gate.synchronize(operation_id="clock-operation-0001", samples=samples(clock))
    before = clock.wall_value
    result = gate.synchronize(
        operation_id="clock-operation-0002", samples=samples(clock)
    )
    assert result["mode"] == "measure-only"
    assert clock.wall_value == before
