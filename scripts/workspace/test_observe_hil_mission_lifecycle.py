"""Focused callback-replay tests for the HIL mission lifecycle observer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("observe_hil_mission_lifecycle.py")
SPEC = importlib.util.spec_from_file_location("observe_hil_mission_lifecycle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


class _FakeNode:
    current: "_FakeNode | None" = None

    def __init__(self, _name: str) -> None:
        self.subscriptions: list[tuple[str, object]] = []
        _FakeNode.current = self

    def create_subscription(self, _message_type: object, topic: str, callback: object, _qos: object) -> object:
        self.subscriptions.append((topic, callback))
        return SimpleNamespace()

    def destroy_subscription(self, _subscription: object) -> None:
        return None

    def destroy_node(self) -> None:
        _FakeNode.current = None


class _FieldsMessage:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)

    def get_fields_and_field_types(self) -> dict[str, str]:
        return {name: "test" for name in self.__dict__}


def _mode_message(
    status: dict[str, object],
    stamp: tuple[int | None, int | None],
) -> SimpleNamespace:
    return SimpleNamespace(
        data=json.dumps(status, separators=(",", ":")),
        stamp=SimpleNamespace(sec=stamp[0], nanosec=stamp[1]),
    )


def _replay(
    monkeypatch,
    tmp_path: Path,
    sequence: list[tuple[str, dict[str, object], tuple[int | None, int | None]]],
    *,
    required_cycles: int = 1,
    finish_after_required_cycles: bool = False,
    safety_after_modes: bool = False,
    cleanup_mode_replay: tuple[str, dict[str, object], tuple[int | None, int | None]] | None = None,
    cleanup_failsafe: bool = False,
    duration_sec: float = 1,
) -> dict[str, object]:
    state = {"running": True, "mode_index": 0, "final_safety_sent": False}
    monkeypatch.setattr(observer, "Node", _FakeNode)
    monkeypatch.setattr(observer.rclpy, "init", lambda: None)
    monkeypatch.setattr(observer.rclpy, "shutdown", lambda: None)
    monkeypatch.setattr(observer.rclpy, "ok", lambda: state["running"])

    def spin_once(node: _FakeNode, **_kwargs: object) -> None:
        callbacks = dict(node.subscriptions)
        if state["mode_index"] < len(sequence):
            name, status, stamp = sequence[state["mode_index"]]
            callbacks[f"/mission/modes/{name}/status"](_mode_message(status, stamp))
            state["mode_index"] += 1
            if state["mode_index"] == len(sequence) and not safety_after_modes:
                state["running"] = False
        elif safety_after_modes and not state["final_safety_sent"]:
            if cleanup_mode_replay is not None:
                name, status, stamp = cleanup_mode_replay
                callbacks[f"/mission/modes/{name}/status"](_mode_message(status, stamp))
            if cleanup_failsafe:
                callbacks["/fmu/out/vehicle_status_v1"](
                    _FieldsMessage(
                        timestamp=125_000,
                        arming_state=observer.VehicleStatus.ARMING_STATE_DISARMED,
                        failsafe=True,
                    )
                )
            callbacks["/fmu/out/vehicle_status_v1"](
                _FieldsMessage(
                    timestamp=126_000,
                    arming_state=observer.VehicleStatus.ARMING_STATE_DISARMED,
                    failsafe=False,
                )
            )
            callbacks["/fmu/out/vehicle_land_detected"](
                _FieldsMessage(timestamp=127_000, landed=True)
            )
            state["final_safety_sent"] = True
            state["running"] = False

    monkeypatch.setattr(observer.rclpy, "spin_once", spin_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--artifact-dir",
            str(tmp_path),
            "--duration-sec",
            str(duration_sec),
            "--required-cycles",
            str(required_cycles),
            "--prelude-timeout-sec",
            "1",
            "--final-safe-grace-sec",
            "0.1",
        ],
    )
    if finish_after_required_cycles:
        sys.argv.append("--finish-after-required-cycles")
    observer.main()
    return json.loads((tmp_path / "mission_lifecycle_observation.json").read_text(encoding="utf-8"))


ACTIVE = {"active": True}
INACTIVE = {"active": False}
LEAVE_SUCCESS = {"tree_finished": True, "tree_success": True}


def _one_cycle_with_edges() -> list[tuple[str, dict[str, object], tuple[int, int]]]:
    return [
        ("inspection_demo", ACTIVE, (10, 0)),
        ("reach_cable", INACTIVE, (15, 0)),
        ("reach_cable", ACTIVE, (20, 0)),
        ("cable_charging", INACTIVE, (25, 0)),
        ("cable_charging", ACTIVE, (30, 0)),
        ("leave_cable", INACTIVE, (34, 0)),
        ("leave_cable", ACTIVE, (35, 0)),
        ("leave_cable", LEAVE_SUCCESS, (40, 0)),
        ("inspection_demo", INACTIVE, (41, 0)),
        ("inspection_demo", ACTIVE, (42, 0)),
    ]


def test_activation_edges_recorded_and_default_endurance_retained(monkeypatch, tmp_path):
    result = _replay(monkeypatch, tmp_path, _one_cycle_with_edges())

    assert result["completed_cycle_count"] == 1
    cycle = result["completed_cycles"][0]
    assert result["finish_after_required_cycles"] is False
    assert result["mission_outcome"] != "required_cycles_completed"
    assert result["cleanup_outcome"] == "not_started"
    for phase in (
        "inspection_started",
        "reach_cable_active",
        "cable_charging_active",
        "leave_cable_succeeded",
        "inspection_resumed",
    ):
        assert cycle[f"{phase}_receipt_at"]
        assert cycle[f"{phase}_receipt_monotonic"] > 0
        assert cycle[f"{phase}_source_stamp"]["nanoseconds"] > 0
    for phase in (
        "reach_cable_active",
        "cable_charging_active",
        "leave_cable_succeeded",
        "inspection_resumed",
    ):
        assert cycle[f"{phase}_activation_source_stamp"]["nanoseconds"] > 0


def test_leave_success_after_deactivation_retains_its_activation(monkeypatch, tmp_path):
    # A successful PX4 handoff can deactivate Leave before its terminal
    # publication. Receipt order of that deactivation must not change proof.
    for index, late_receipt in enumerate((False, True)):
        sequence = _one_cycle_with_edges()
        sequence[7] = ("leave_cable", {**LEAVE_SUCCESS, "active": False}, (40, 0))
        deactivation = ("leave_cable", INACTIVE, (36, 0))
        sequence.insert(len(sequence) if late_receipt else 7, deactivation)
        result = _replay(monkeypatch, tmp_path / str(index), sequence)
        assert result["completed_cycle_count"] == 1
        assert result["completed_cycles"][0]["leave_cable_succeeded_activation_source_stamp_ns"] == 35_000_000_000


def test_old_activation_cannot_satisfy_a_later_cycle_phase(monkeypatch, tmp_path):
    for index, (mode, old_inactive, old_active) in enumerate((
        ("reach_cable", 4, 5),
        ("cable_charging", 15, 16),
        ("leave_cable", 24, 25),
        ("inspection_demo", 31, 32),
    )):
        sequence = []
        for name, value, stamp in _one_cycle_with_edges():
            if name == mode and value is INACTIVE:
                stamp = (old_inactive, 0)
            elif name == mode and value is ACTIVE and stamp != (10, 0):
                sequence.append((name, value, (old_active, 0)))
                # Republish active after the phase floor, with no new edge.
            sequence.append((name, value, stamp))
        result = _replay(monkeypatch, tmp_path / str(index), sequence)
        assert result["completed_cycle_count"] == 0


def test_mode_failure_remains_latched(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("leave_cable", {"tree_finished": True, "tree_success": False}, (40, 0)),
        ],
    )

    assert result["mission_failure_latched"] is True
    assert result["mission_failure_kind"] == "mode_failed"
    assert result["completed_cycle_count"] == 0


def test_new_inspection_activation_can_arrive_before_leave_success_callback(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("reach_cable", INACTIVE, (15, 0)),
            ("reach_cable", ACTIVE, (20, 0)),
            ("cable_charging", INACTIVE, (25, 0)),
            ("cable_charging", ACTIVE, (30, 0)),
            ("leave_cable", INACTIVE, (34, 0)),
            ("leave_cable", ACTIVE, (35, 0)),
            ("inspection_demo", INACTIVE, (36, 0)),
            ("inspection_demo", ACTIVE, (37, 0)),
            ("inspection_demo", ACTIVE, (41, 0)),
            ("leave_cable", LEAVE_SUCCESS, (40, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 1
    cycle = result["completed_cycles"][0]
    assert cycle["inspection_resumed_source_stamp"]["nanoseconds"] == 41_000_000_000
    assert cycle["inspection_resumed_activation_source_stamp"]["nanoseconds"] == 37_000_000_000
    assert (
        cycle["inspection_resumed_receipt_monotonic"]
        < cycle["leave_cable_succeeded_receipt_monotonic"]
    )


def test_identical_active_payloads_complete_two_cycles_only_with_real_edges(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            *_one_cycle_with_edges(),
            ("reach_cable", INACTIVE, (55, 0)),
            ("reach_cable", ACTIVE, (60, 0)),
            ("cable_charging", INACTIVE, (65, 0)),
            ("cable_charging", ACTIVE, (70, 0)),
            ("leave_cable", INACTIVE, (74, 0)),
            ("leave_cable", ACTIVE, (75, 0)),
            ("leave_cable", LEAVE_SUCCESS, (80, 0)),
            ("inspection_demo", INACTIVE, (81, 0)),
            ("inspection_demo", ACTIVE, (82, 0)),
        ],
        required_cycles=2,
        finish_after_required_cycles=True,
        safety_after_modes=True,
    )

    assert result["mission_outcome"] == "required_cycles_completed"
    assert result["completed_cycle_count"] == 2
    assert result["cleanup_outcome"] == "safe_landed_disarmed"
    assert [
        cycle["inspection_resumed_source_stamp"]["nanoseconds"]
        for cycle in result["completed_cycles"]
    ] == [42_000_000_000, 82_000_000_000]


def test_periodic_active_and_terminal_replays_cannot_count_a_second_cycle(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            *_one_cycle_with_edges(),
            ("inspection_demo", ACTIVE, (50, 0)),
            ("reach_cable", ACTIVE, (60, 0)),
            ("cable_charging", ACTIVE, (70, 0)),
            ("leave_cable", LEAVE_SUCCESS, (80, 0)),
            ("inspection_demo", ACTIVE, (90, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 1
    assert result["cycle_phase_at_finish"] == "inspection"


def test_leave_success_without_a_prior_active_generation_fails_closed(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("reach_cable", INACTIVE, (15, 0)),
            ("reach_cable", ACTIVE, (20, 0)),
            ("cable_charging", INACTIVE, (25, 0)),
            ("cable_charging", ACTIVE, (30, 0)),
            ("leave_cable", INACTIVE, (34, 0)),
            # A combined active/success sample is not proof that Leave was
            # active before its terminal success.
            ("leave_cable", {"active": True, **LEAVE_SUCCESS}, (40, 0)),
            ("inspection_demo", INACTIVE, (41, 0)),
            ("inspection_demo", ACTIVE, (42, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 0
    assert result["cycle_phase_at_finish"] == "charging_active"


def test_resumption_requires_a_new_inspection_activation(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("reach_cable", INACTIVE, (15, 0)),
            ("reach_cable", ACTIVE, (20, 0)),
            ("cable_charging", INACTIVE, (25, 0)),
            ("cable_charging", ACTIVE, (30, 0)),
            ("leave_cable", INACTIVE, (34, 0)),
            ("leave_cable", ACTIVE, (35, 0)),
            ("leave_cable", LEAVE_SUCCESS, (40, 0)),
            ("inspection_demo", ACTIVE, (42, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 0
    assert result["cycle_phase_at_finish"] == "leave_complete"


def test_source_regressing_charging_needs_a_new_activation(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("reach_cable", INACTIVE, (29, 0)),
            ("reach_cable", ACTIVE, (30, 0)),
            ("cable_charging", INACTIVE, (19, 0)),
            ("cable_charging", ACTIVE, (20, 0)),
            ("leave_cable", INACTIVE, (35, 0)),
            ("leave_cable", ACTIVE, (36, 0)),
            ("leave_cable", LEAVE_SUCCESS, (40, 0)),
            ("cable_charging", INACTIVE, (30, 1)),
            ("cable_charging", ACTIVE, (31, 0)),
            ("inspection_demo", INACTIVE, (41, 0)),
            ("inspection_demo", ACTIVE, (42, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 1
    cycle = result["completed_cycles"][0]
    assert cycle["reach_cable_active_source_stamp"]["nanoseconds"] == 30_000_000_000
    assert cycle["cable_charging_active_source_stamp"]["nanoseconds"] == 31_000_000_000
    assert cycle["cable_charging_active_activation_source_stamp"]["nanoseconds"] == 31_000_000_000


def test_source_order_allows_charging_callback_before_reach_predecessor(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (5, 0)),
            ("cable_charging", INACTIVE, (19, 0)),
            ("cable_charging", ACTIVE, (20, 0)),
            ("reach_cable", ACTIVE, (10, 0)),
            # The inactive predecessor arrives after its active callback but
            # has an earlier source stamp, so the edge is source-ordered.
            ("reach_cable", INACTIVE, (9, 0)),
            ("leave_cable", INACTIVE, (25, 0)),
            ("leave_cable", ACTIVE, (26, 0)),
            ("leave_cable", LEAVE_SUCCESS, (30, 0)),
            ("inspection_demo", INACTIVE, (35, 0)),
            ("inspection_demo", ACTIVE, (40, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 1
    cycle = result["completed_cycles"][0]
    assert cycle["reach_cable_active_source_stamp"]["nanoseconds"] == 10_000_000_000
    assert cycle["cable_charging_active_source_stamp"]["nanoseconds"] == 20_000_000_000
    assert (
        cycle["cable_charging_active_receipt_monotonic"]
        < cycle["reach_cable_active_receipt_monotonic"]
    )


def test_stale_equal_and_invalid_source_stamps_do_not_advance(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        [
            ("inspection_demo", ACTIVE, (10, 0)),
            ("reach_cable", INACTIVE, (19, 0)),
            ("reach_cable", ACTIVE, (20, 0)),
            ("cable_charging", INACTIVE, (19, 0)),
            ("cable_charging", ACTIVE, (20, 0)),
            ("cable_charging", ACTIVE, (19, 0)),
            ("cable_charging", ACTIVE, (0, 0)),
            ("cable_charging", ACTIVE, (-1, 0)),
            ("cable_charging", INACTIVE, (20, 1)),
            ("cable_charging", ACTIVE, (21, 0)),
            ("leave_cable", INACTIVE, (21, 1)),
            ("leave_cable", ACTIVE, (22, 0)),
            ("leave_cable", LEAVE_SUCCESS, (21, 0)),
            ("leave_cable", LEAVE_SUCCESS, (20, 0)),
            ("leave_cable", LEAVE_SUCCESS, (0, 0)),
            ("leave_cable", LEAVE_SUCCESS, (22, 1)),
            ("inspection_demo", ACTIVE, (22, 0)),
            ("inspection_demo", ACTIVE, (21, 0)),
            ("inspection_demo", ACTIVE, (0, 0)),
            ("inspection_demo", INACTIVE, (22, 1)),
            ("inspection_demo", ACTIVE, (23, 0)),
        ],
    )

    assert result["completed_cycle_count"] == 1
    cycle = result["completed_cycles"][0]
    assert [
        cycle[f"{phase}_source_stamp"]["nanoseconds"]
        for phase in (
            "inspection_started",
            "reach_cable_active",
            "cable_charging_active",
            "leave_cable_succeeded",
            "inspection_resumed",
        )
    ] == [10_000_000_000, 20_000_000_000, 21_000_000_000, 22_000_000_001, 23_000_000_000]


def test_delayed_precleanup_mode_failure_latches_during_cleanup(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        _one_cycle_with_edges(),
        finish_after_required_cycles=True,
        safety_after_modes=True,
        cleanup_mode_replay=(
            "leave_cable",
            {"tree_finished": True, "tree_success": False},
            (1, 0),
        ),
    )

    assert result["mission_failure_latched"] is True
    assert result["mission_failure_kind"] == "mode_failed"
    assert result["cleanup_outcome"] == "safe_landed_disarmed"
    assert result["mission_failures"][0]["source_stamp_ns"] <= result["cleanup_started_wallclock_ns"]


def test_only_after_boundary_mode_failure_is_postcleanup_evidence(monkeypatch, tmp_path):
    future_stamp_ns = time.time_ns() + 10_000_000_000
    future_stamp = divmod(future_stamp_ns, 1_000_000_000)
    result = _replay(
        monkeypatch,
        tmp_path,
        _one_cycle_with_edges(),
        finish_after_required_cycles=True,
        safety_after_modes=True,
        cleanup_mode_replay=(
            "leave_cable",
            {"tree_finished": True, "tree_success": False},
            future_stamp,
        ),
    )

    assert result["mission_failure_latched"] is False
    assert result["mission_outcome"] == "required_cycles_completed"
    assert result["post_deadline_mode_events"][0]["source_stamp_ns"] > result["cleanup_started_wallclock_ns"]


def test_missing_or_invalid_cleanup_failure_stamp_remains_fatal(monkeypatch, tmp_path):
    for index, stamp in enumerate(((0, 0), (None, None))):
        result = _replay(
            monkeypatch,
            tmp_path / str(index),
            _one_cycle_with_edges(),
            finish_after_required_cycles=True,
            safety_after_modes=True,
            cleanup_mode_replay=(
                "leave_cable",
                {"tree_finished": True, "tree_success": False},
                stamp,
            ),
        )

        assert result["mission_failure_latched"] is True
        assert result["mission_failure_kind"] == "mode_failed"


def test_cleanup_failsafe_always_latches(monkeypatch, tmp_path):
    result = _replay(
        monkeypatch,
        tmp_path,
        _one_cycle_with_edges(),
        finish_after_required_cycles=True,
        safety_after_modes=True,
        cleanup_failsafe=True,
    )

    assert result["mission_failure_latched"] is True
    assert result["mission_failure_kind"] == "px4_failsafe"
    assert result["cleanup_outcome"] == "safe_landed_disarmed"


def _periodic_cycles(cycles: int, samples_per_phase: int) -> list[tuple[str, dict[str, object], tuple[int, int]]]:
    """Canonical cycles with periodic status replays, as the modes publish them."""
    sequence: list[tuple[str, dict[str, object], tuple[int, int]]] = []
    stamp = 1_000
    def emit(name: str, status: dict[str, object]) -> None:
        nonlocal stamp
        stamp += 1
        sequence.append((name, status, (stamp, 0)))
    for _ in range(cycles):
        for name in ("inspection_demo", "reach_cable", "cable_charging", "leave_cable"):
            emit(name, INACTIVE)
            for _ in range(samples_per_phase):
                emit(name, ACTIVE)
            if name == "leave_cable":
                emit(name, {"active": False, "tree_finished": True, "tree_success": True})
            emit(name, INACTIVE)
    return sequence


# HIL soak runs: the observer re-joined every phase sample on every status
# sample, so its CPU (and the Pi load) grew through every long run.
def test_long_periodic_replay_costs_linear_time(monkeypatch, tmp_path):
    def replay_seconds(samples_per_phase: int, directory: Path) -> float:
        directory.mkdir()
        sequence = _periodic_cycles(2, samples_per_phase)
        started = time.perf_counter()
        _replay(monkeypatch, directory, sequence, duration_sec=3600)
        return time.perf_counter() - started

    short = replay_seconds(500, tmp_path / "short")
    long = replay_seconds(2000, tmp_path / "long")
    # Four times the samples: linear cost is ~4x, the old quadratic cost ~16x.
    assert long < 8 * max(short, 0.05)


def _without_wall_clock(value: object) -> object:
    """Drop receipt times and durations; keep source-stamped evidence."""
    if isinstance(value, dict):
        return {
            key: _without_wall_clock(item)
            for key, item in value.items()
            if not (
                key.endswith("_at")
                or "monotonic" in key
                or key in {"time", "artifact_dir", "age_sec", "active_duration_sec"}
            )
        }
    if isinstance(value, list):
        return [_without_wall_clock(item) for item in value]
    return value


def test_incremental_phase_join_matches_full_rebuild(monkeypatch, tmp_path):
    import random

    for seed in range(8):
        rng = random.Random(seed)
        sequence = _periodic_cycles(3, rng.randint(2, 6))
        # Deliver some samples late (source order kept by stamps).
        for _ in range(len(sequence) // 6):
            index = rng.randrange(1, len(sequence))
            sequence[index - 1], sequence[index] = sequence[index], sequence[index - 1]
        results = []
        for incremental in (True, False):
            monkeypatch.setattr(observer, "INCREMENTAL_PHASE_JOIN", incremental)
            directory = tmp_path / f"seed{seed}_{incremental}"
            directory.mkdir()
            result = _replay(monkeypatch, directory, sequence, required_cycles=3, duration_sec=3600)
            results.append(json.dumps(_without_wall_clock(result), sort_keys=True, default=str))
        assert results[0] == results[1], f"seed {seed}"

