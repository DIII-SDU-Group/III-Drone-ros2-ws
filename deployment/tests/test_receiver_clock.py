from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.clock import (
    ClockController,
    ClockFlushCoordinator,
    MAX_COMMIT_OFFSET_NS,
)


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
            "target_monotonic_ns": clock.monotonic_value + index,
            "target_wall_ns": clock.wall_value + index,
            "operator_midpoint_utc_ns": operator + index,
            "rtt_ns": rtt_ns + index,
            "offset_ns": clock.wall_value - operator,
        }
        for index in range(count)
    ]


def controller(
    tmp_path: Path,
    clock: FakeClock,
    starts: list,
    *,
    flushes: list | None = None,
    safe=lambda: False,
    stops: list | None = None,
):
    flushes = [] if flushes is None else flushes
    stops = [] if stops is None else stops
    return ClockController(
        tmp_path / "clock-state.json",
        boot_id=lambda: clock.boot,
        monotonic_ns=clock.monotonic_ns,
        wall_ns=clock.wall_ns,
        set_wall_ns=clock.set_wall_ns,
        flush_before_open=lambda state: flushes.append(state["gate"])
        or {
            "schema": "iii.clock-flush-barrier/v1",
            "clock_state_id": state["state_id"],
            "commits": {"runtime-api": "a" * 64},
        },
        gate_opened=lambda: starts.append("runtime") or {"started": True},
        maintenance_safe=safe,
        stop_runtime=lambda: stops.append("runtime"),
    )


def test_boot_starts_degraded_and_five_settled_samples_open_gate(tmp_path: Path):
    clock = FakeClock()
    starts = []
    flushes = []
    gate = controller(tmp_path, clock, starts, flushes=flushes)
    assert gate.status()["gate"] == "DEGRADED_CLOCK"
    result = gate.synchronize(
        operation_id="clock-operation-0001", samples=samples(clock)
    )
    assert result["gate"] == "OPERATIONAL"
    assert abs(result["offset_ns"]) <= 250_000_000
    assert starts == ["runtime"]
    assert flushes == ["FLUSHING_CLOCK"]
    assert gate.status()["gate"] == "OPERATIONAL"


def test_high_rtt_or_too_few_samples_fail_closed(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    with pytest.raises(ContractError, match="five samples"):
        gate.synchronize(
            operation_id="clock-operation-0001", samples=samples(clock, count=4)
        )
    with pytest.raises(ContractError, match="2 seconds"):
        gate.synchronize(
            operation_id="clock-operation-0001",
            samples=samples(clock, rtt_ns=2_000_000_001),
        )


def test_new_boot_reenters_degraded_clock(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    gate.synchronize(operation_id="clock-operation-0001", samples=samples(clock))
    clock.boot = "boot-b"
    assert gate.status()["gate"] == "DEGRADED_CLOCK"


def test_current_boot_clock_state_rejects_boolean_integer_spoofing(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    gate.synchronize(operation_id="clock-operation-0001", samples=samples(clock))
    value = gate.load()
    value["uncertainty_ns"] = True
    value["state_id"] = content_identity(
        {key: item for key, item in value.items() if key != "state_id"}
    )
    gate.state_path.write_bytes(canonical_json(value) + b"\n")
    with pytest.raises(ContractError, match="uncertainty_ns"):
        gate.load()


def test_operational_manual_sync_is_measure_only_and_never_steps(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    gate.synchronize(operation_id="clock-operation-0001", samples=samples(clock))
    before = clock.wall_value
    result = gate.synchronize(
        operation_id="clock-operation-0002", samples=samples(clock)
    )
    assert result["mode"] == "measure-only"
    assert result["resync_required"] is False
    assert clock.wall_value == before


def test_settled_sample_and_threshold_edges_are_enforced(tmp_path: Path):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])
    edge = samples(clock, rtt_ns=2_000_000_000 - 4)
    gate.synchronize(operation_id="clock-operation-edge", samples=edge)

    unsettled = samples(clock)
    unsettled[-1]["target_wall_ns"] += 250_000_001
    unsettled[-1]["offset_ns"] += 250_000_001
    with pytest.raises(ContractError, match="not settled"):
        ClockController.validate_samples(unsettled)
    reversed_samples = list(reversed(samples(clock)))
    with pytest.raises(ContractError, match="settled in order"):
        ClockController.validate_samples(reversed_samples)


def test_flush_failure_never_opens_gate_or_starts_runtime(tmp_path: Path):
    clock = FakeClock()
    starts: list[str] = []
    gate = controller(tmp_path, clock, starts)

    def fail(_state):
        raise ContractError("durability failure")

    gate.flush_before_open = fail
    with pytest.raises(ContractError, match="durability failure"):
        gate.synchronize(operation_id="clock-operation-fail", samples=samples(clock))
    assert gate.load()["gate"] == "FLUSHING_CLOCK"
    assert starts == []


def test_flush_resume_is_bound_to_operation_and_rechecks_wall_mapping(
    tmp_path: Path,
):
    clock = FakeClock()
    gate = controller(tmp_path, clock, [])

    def fail(_state):
        raise ContractError("interrupted")

    gate.flush_before_open = fail
    with pytest.raises(ContractError, match="interrupted"):
        gate.synchronize(operation_id="clock-operation-a", samples=samples(clock))
    assert gate.load()["gate"] == "FLUSHING_CLOCK"

    with pytest.raises(ContractError, match="retained operation"):
        gate.synchronize(operation_id="clock-operation-b", samples=samples(clock))

    flushing = gate.load()

    def move_clock(_state):
        clock.wall_value += MAX_COMMIT_OFFSET_NS + 1
        return {
            "schema": "iii.clock-flush-barrier/v1",
            "clock_state_id": flushing["state_id"],
            "commits": {},
        }

    gate.flush_before_open = move_clock
    with pytest.raises(ContractError, match="during log flush"):
        gate.synchronize(operation_id="clock-operation-a", samples=samples(clock))
    assert gate.load()["gate"] == "FLUSHING_CLOCK"


def test_flush_coordinator_authenticates_exact_durable_commits(tmp_path: Path):
    state = {"boot_id": "boot-a", "state_id": "a" * 64}
    for service in ("runtime-api", "system-daemon"):
        value = {
            "schema": "iii.clock-flush-commit/v1",
            "commit_id": "0" * 64,
            "service": service,
            "boot_id": state["boot_id"],
            "clock_state_id": state["state_id"],
            "records_flushed": 1,
            "dropped_records": 0,
            "committed_monotonic_ns": 10,
        }
        value["commit_id"] = content_identity(
            {key: item for key, item in value.items() if key != "commit_id"}
        )
        (tmp_path / f"{service}.json").write_bytes(canonical_json(value) + b"\n")
    barrier = ClockFlushCoordinator(tmp_path, monotonic=lambda: 0.0)(state)
    assert set(barrier["commits"]) == {"runtime-api", "system-daemon"}

    invalid = {
        "schema": "iii.clock-flush-commit/v1",
        "commit_id": "0" * 64,
        "service": "runtime-api",
        "boot_id": state["boot_id"],
        "clock_state_id": state["state_id"],
        "records_flushed": True,
        "dropped_records": 0,
        "committed_monotonic_ns": 10,
    }
    invalid["commit_id"] = content_identity(
        {key: item for key, item in invalid.items() if key != "commit_id"}
    )
    (tmp_path / "runtime-api.json").write_bytes(canonical_json(invalid) + b"\n")
    now = iter((0.0, 0.0, 0.1, 0.2, 0.3))
    with pytest.raises(ContractError, match="did not commit"):
        ClockFlushCoordinator(
            tmp_path,
            monotonic=lambda: next(now),
            sleep=lambda _duration: None,
            timeout_s=0.25,
        )(state)


def test_standby_discontinuity_stops_graph_and_requires_explicit_restart(
    tmp_path: Path,
):
    clock = FakeClock()
    starts: list[str] = []
    stops: list[str] = []
    gate = controller(tmp_path, clock, starts, safe=lambda: True, stops=stops)
    gate.synchronize(operation_id="clock-operation-boot", samples=samples(clock))
    clock.wall_value += 2_000_000_001
    assert gate.status()["gate"] == "DEGRADED_CLOCK"
    assert stops == ["runtime"]
    result = gate.synchronize(
        operation_id="clock-operation-resync", samples=samples(clock)
    )
    assert result["start_required"] is True
    assert result["runtime_start"] is None
    assert starts == ["runtime"]


def test_active_discontinuity_buffers_fault_until_maintenance_safe(tmp_path: Path):
    clock = FakeClock()
    safe = {"value": False}
    stops: list[str] = []
    gate = controller(
        tmp_path,
        clock,
        [],
        safe=lambda: safe["value"],
        stops=stops,
    )
    gate.synchronize(operation_id="clock-operation-boot", samples=samples(clock))
    clock.wall_value -= 250_000_001
    assert gate.status()["gate"] == "CLOCK_FAULT_ACTIVE"
    assert stops == []
    before = clock.wall_value
    measured = gate.synchronize(
        operation_id="clock-operation-measure", samples=samples(clock)
    )
    assert measured["mode"] == "measure-only"
    assert measured["gate"] == "CLOCK_FAULT_ACTIVE"
    assert clock.wall_value == before
    safe["value"] = True
    assert gate.status()["gate"] == "DEGRADED_CLOCK"
    assert stops == ["runtime"]
