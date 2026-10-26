#!/usr/bin/env python3
"""Record and verify an endurance split-host HIL mission lifecycle.

The observer is deliberately passive: mode activation and recharge/leave intents
remain ordinary mission commands.  Its evidence is stronger than the previous
one-cycle smoke check: a successful observation must span the requested wall
clock duration and contain the requested number of ordered, complete cycles.
"""

from __future__ import annotations

import argparse
import bisect
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

from iii_drone_interfaces.msg import MissionModeStatus, StringStamped
from px4_msgs.msg import VehicleLandDetected, VehicleStatus


def _mission_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


def _mode_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


def _plain(message: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in message.get_fields_and_field_types():
        value = getattr(message, field)
        result[field] = list(value) if isinstance(value, (list, tuple)) else value
    return result


def _mode(message: StringStamped) -> dict[str, Any] | None:
    if not message.data:
        return None
    try:
        return json.loads(message.data)
    except json.JSONDecodeError:
        return {"raw": message.data}


def _mode_source_stamp(message: StringStamped) -> dict[str, int | None]:
    """Retain the ROS publication stamp used to order mode evidence."""
    stamp = getattr(message, "stamp", None)
    if stamp is None:
        return {"sec": None, "nanosec": None, "nanoseconds": None}
    try:
        seconds = int(getattr(stamp, "sec", 0))
        nanoseconds = int(getattr(stamp, "nanosec", 0))
    except (TypeError, ValueError):
        return {"sec": None, "nanosec": None, "nanoseconds": None}
    if seconds < 0 or nanoseconds < 0 or nanoseconds >= 1_000_000_000:
        return {"sec": None, "nanosec": None, "nanoseconds": None}
    return {
        "sec": seconds,
        "nanosec": nanoseconds,
        "nanoseconds": seconds * 1_000_000_000 + nanoseconds,
    }


def _positive_source_stamp(source_stamp_ns: int | None) -> bool:
    """A lifecycle phase needs a real, strictly positive source timestamp."""
    return source_stamp_ns is not None and source_stamp_ns > 0


def _px4_sample(message: Any) -> dict[str, Any]:
    """Capture a PX4 sample with both source and observer receipt timing."""
    return {
        "value": _plain(message),
        "message_timestamp_us": int(getattr(message, "timestamp", 0)),
        "local_receipt_at": datetime.now(timezone.utc).isoformat(),
        "_local_receipt_monotonic": time.monotonic(),
    }


def _sample_evidence(
    sample: dict[str, Any] | None,
    *,
    now_monotonic: float,
    cleanup_started_monotonic: float | None,
    freshness_sec: float,
) -> dict[str, Any]:
    """Describe whether one PX4 sample is fresh and post-cleanup."""
    if sample is None:
        return {
            "available": False,
            "message_timestamp_us": None,
            "local_receipt_at": None,
            "age_sec": None,
            "fresh": False,
            "post_cleanup": False,
            "value": None,
        }
    receipt_monotonic = float(sample["_local_receipt_monotonic"])
    age_sec = max(0.0, now_monotonic - receipt_monotonic)
    return {
        "available": True,
        "message_timestamp_us": sample["message_timestamp_us"],
        "local_receipt_at": sample["local_receipt_at"],
        "age_sec": age_sec,
        "fresh": age_sec <= freshness_sec,
        "post_cleanup": (
            cleanup_started_monotonic is not None
            and receipt_monotonic >= cleanup_started_monotonic
        ),
        "value": sample["value"],
    }


# Tests compare the incremental phase join with a full rebuild per sample.
INCREMENTAL_PHASE_JOIN = True


def insert_source_ordered(observations: list[dict[str, Any]], observation: dict[str, Any]) -> int | None:
    """Insert by source stamp; return the index, or None for a duplicate stamp.

    Samples nearly always arrive in source order, so search from the end: an
    in-order sample is appended without scanning the whole history.
    """
    source_stamp_ns = observation["source_stamp_ns"]
    index = len(observations)
    while index > 0:
        candidate_stamp_ns = observations[index - 1]["source_stamp_ns"]
        if candidate_stamp_ns == source_stamp_ns:
            return None
        if candidate_stamp_ns < source_stamp_ns:
            break
        index -= 1
    observations.insert(index, observation)
    return index


def _fresh_phase_join_state() -> dict[str, Any]:
    return {
        "activity_index": 0,
        "previous_active": None,
        "active_generation": None,
        "last_phase_source_stamp_ns": -1,
    }


def _join_phase_observation(
    activities: list[dict[str, Any]],
    state: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Join one phase observation (in source order) to its activation generation."""
    source_stamp_ns = observation["source_stamp_ns"]
    while (
        state["activity_index"] < len(activities)
        and activities[state["activity_index"]]["source_stamp_ns"] <= source_stamp_ns
    ):
        activity = activities[state["activity_index"]]
        active = activity["value"]["active"]
        if active:
            if state["previous_active"] is False:
                state["active_generation"] = activity
            elif state["previous_active"] is None:
                # The initial Inspection prelude may begin with a
                # fresh active sample. Every later phase is stricter.
                state["active_generation"] = None
        # Deactivation does not erase the activation that produced a
        # terminal result. PX4 may hand control to the successor
        # before the final SUCCESS status is published. A later
        # inactive->active edge replaces this generation; periodic
        # terminal samples cannot manufacture a new one.
        state["previous_active"] = active
        state["activity_index"] += 1
    state["last_phase_source_stamp_ns"] = source_stamp_ns
    candidate = dict(observation)
    active_generation = state["active_generation"]
    if active_generation is not None:
        candidate["activation_observation"] = active_generation
        candidate["activation_source_stamp_ns"] = active_generation["source_stamp_ns"]
    else:
        candidate["activation_observation"] = None
        candidate["activation_source_stamp_ns"] = None
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--duration-sec", type=float, default=1800.0)
    parser.add_argument("--required-cycles", type=int, default=2)
    parser.add_argument(
        "--finish-after-required-cycles",
        action="store_true",
        help="End the mission observation after the required cycles, then run the existing safety cleanup.",
    )
    parser.add_argument("--prelude-timeout-sec", type=float, default=300.0)
    parser.add_argument(
        "--final-safe-grace-sec",
        type=float,
        default=300.0,
        help="Additional time allowed to observe safe landing/disarm after the mission outcome is decided.",
    )
    parser.add_argument(
        "--cleanup-freshness-sec",
        type=float,
        default=5.0,
        help="Maximum local receipt age for each post-cleanup PX4 safety sample.",
    )
    args = parser.parse_args()
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output_path = artifact_dir / "mission_lifecycle_observation.json"

    rclpy.init()
    node = Node("hil_mission_lifecycle_observer")
    observer_started_monotonic = time.monotonic()
    latest_mission: dict[str, Any] = {}
    latest_vehicle: dict[str, Any] = {}
    latest_land_detected: dict[str, Any] = {}
    latest_vehicle_sample: dict[str, Any] | None = None
    latest_land_detected_sample: dict[str, Any] | None = None
    latest_modes: dict[str, dict[str, Any] | None] = {
        "inspection_demo": None,
        "reach_cable": None,
        "cable_charging": None,
        "leave_cable": None,
    }
    # Reporting remains payload-deduplicated, but lifecycle evidence tracks
    # source-ordered inactive->active mode generations independently.
    reported_mode_payloads: dict[str, dict[str, Any] | None] = {
        name: None for name in latest_modes
    }
    phase_observations: dict[str, list[dict[str, Any]]] = {
        name: [] for name in latest_modes
    }
    activity_observations: dict[str, list[dict[str, Any]]] = {
        name: [] for name in latest_modes
    }
    phase_candidates: dict[str, list[dict[str, Any]]] = {
        name: [] for name in latest_modes
    }
    # Join state after the last phase observation of each mode, so an
    # in-order observation extends phase_candidates instead of rebuilding it.
    phase_join_state: dict[str, dict[str, Any]] = {
        name: _fresh_phase_join_state() for name in latest_modes
    }
    events: list[dict[str, Any]] = []
    if args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.required_cycles <= 0:
        parser.error("--required-cycles must be positive")
    if args.prelude_timeout_sec <= 0:
        parser.error("--prelude-timeout-sec must be positive")

    # A cycle begins at an active inspection phase, then must pass every
    # canonical successor before returning to active inspection.  We do not
    # infer a cycle from a stale/transient status snapshot.
    cycle_phase = "awaiting_inspection"
    completed_cycles: list[dict[str, Any]] = []
    current_cycle: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []
    post_deadline_mode_events: list[dict[str, Any]] = []
    mission_started_monotonic: float | None = None
    mission_started_at: str | None = None
    cleanup_started_monotonic: float | None = None
    cleanup_started_at: str | None = None
    cleanup_started_wallclock_ns: int | None = None
    cleanup_deadline: float | None = None
    mission_outcome: str | None = None
    cleanup_outcome: str | None = None
    mission_failure_latched = False
    mission_failure_kind: str | None = None
    deadline: float | None = None
    cycle_floor_source_stamp_ns = 0
    last_accepted_phase_source_stamp_ns: dict[str, int] = {
        name: 0 for name in latest_modes
    }

    if args.final_safe_grace_sec < 0:
        parser.error("--final-safe-grace-sec must be non-negative")
    if args.cleanup_freshness_sec <= 0:
        parser.error("--cleanup-freshness-sec must be positive")

    def latch_mission_failure(kind: str, evidence: dict[str, Any]) -> None:
        nonlocal mission_failure_kind, mission_failure_latched, mission_outcome
        if mission_failure_latched:
            return
        mission_failure_latched = True
        mission_failure_kind = kind
        mission_outcome = kind
        failures.append(evidence)

    def begin_cleanup() -> None:
        nonlocal cleanup_deadline, cleanup_started_at, cleanup_started_monotonic
        nonlocal cleanup_started_wallclock_ns
        if cleanup_started_monotonic is not None:
            return
        cleanup_started_monotonic = time.monotonic()
        # Mode status publishers use RCL_SYSTEM_TIME, so retain this source
        # ordering boundary before accepting cancellation as cleanup evidence.
        cleanup_started_wallclock_ns = time.time_ns()
        cleanup_started_at = datetime.now(timezone.utc).isoformat()
        cleanup_deadline = cleanup_started_monotonic + max(0.0, args.final_safe_grace_sec)

    def cleanup_safety_evidence(now_monotonic: float) -> dict[str, Any]:
        vehicle_evidence = _sample_evidence(
            latest_vehicle_sample,
            now_monotonic=now_monotonic,
            cleanup_started_monotonic=cleanup_started_monotonic,
            freshness_sec=args.cleanup_freshness_sec,
        )
        land_evidence = _sample_evidence(
            latest_land_detected_sample,
            now_monotonic=now_monotonic,
            cleanup_started_monotonic=cleanup_started_monotonic,
            freshness_sec=args.cleanup_freshness_sec,
        )
        vehicle_value = vehicle_evidence.get("value") or {}
        land_value = land_evidence.get("value") or {}
        safe = bool(
            vehicle_evidence["available"]
            and vehicle_evidence["fresh"]
            and vehicle_evidence["post_cleanup"]
            and land_evidence["available"]
            and land_evidence["fresh"]
            and land_evidence["post_cleanup"]
            and land_value.get("landed") is True
            and vehicle_value.get("arming_state") == VehicleStatus.ARMING_STATE_DISARMED
        )
        return {
            "safe_landed_disarmed": safe,
            "freshness_window_sec": args.cleanup_freshness_sec,
            "vehicle_status": vehicle_evidence,
            "land_detected": land_evidence,
        }

    def on_vehicle(message: VehicleStatus) -> None:
        nonlocal latest_vehicle_sample
        sample = _px4_sample(message)
        value = _plain(message)
        latest_vehicle.update(value)
        latest_vehicle_sample = sample
        # A HIL run must not silently continue through a PX4 failsafe.  Latch
        # the mission failure, then let the bounded cleanup observation prove
        # whether the vehicle reached a safe landed/disarmed state.
        if value.get("failsafe") and sample["_local_receipt_monotonic"] >= observer_started_monotonic:
            latch_mission_failure(
                "px4_failsafe",
                {
                    "time": sample["local_receipt_at"],
                    "kind": "px4_failsafe",
                    "vehicle_status": value,
                    "message_timestamp_us": sample["message_timestamp_us"],
                },
            )

    def on_land_detected(message: VehicleLandDetected) -> None:
        nonlocal latest_land_detected_sample
        sample = _px4_sample(message)
        latest_land_detected.update(_plain(message))
        latest_land_detected_sample = sample

    def on_mission(message: MissionModeStatus) -> None:
        latest_mission.update(_plain(message))

    def phase_is_eligible(name: str, value: dict[str, Any]) -> bool:
        if name == "leave_cable":
            return bool(value.get("tree_finished") and value.get("tree_success"))
        return value.get("active") is True

    def cache_mode_activity(name: str, observation: dict[str, Any]) -> int | None:
        """Retain only valid source-stamped activity transitions for edge proof."""
        if not _positive_source_stamp(observation["source_stamp_ns"]):
            return None
        if not isinstance(observation["value"].get("active"), bool):
            return None
        return insert_source_ordered(activity_observations[name], observation)

    def cache_phase_observation(name: str, observation: dict[str, Any]) -> int | None:
        if _positive_source_stamp(observation["source_stamp_ns"]):
            return insert_source_ordered(phase_observations[name], observation)
        return None

    def rebuild_phase_candidates(name: str) -> None:
        """Join phase evidence to its source-ordered inactive->active generation."""
        state = _fresh_phase_join_state()
        phase_candidates[name] = [
            _join_phase_observation(activity_observations[name], state, observation)
            for observation in phase_observations[name]
        ]
        phase_join_state[name] = state

    def update_phase_candidates(
        name: str,
        activity_index: int | None,
        phase_index: int | None,
    ) -> None:
        """Extend the candidates for an in-order observation; rebuild otherwise.

        Rebuilding every candidate on every status sample made each sample cost
        the length of the run so far (HIL soak runs: the observer's CPU, and the
        Pi load, grew through every long run).
        """
        state = phase_join_state[name]
        phases = phase_observations[name]
        late_activity = activity_index is not None and (
            activity_index < state["activity_index"]
            or activity_observations[name][activity_index]["source_stamp_ns"]
            <= state["last_phase_source_stamp_ns"]
        )
        late_phase = phase_index is not None and phase_index != len(phases) - 1
        if late_activity or late_phase or not INCREMENTAL_PHASE_JOIN:
            rebuild_phase_candidates(name)
        elif phase_index is not None:
            phase_candidates[name].append(
                _join_phase_observation(activity_observations[name], state, phases[phase_index])
            )

    def next_phase_candidate(
        name: str,
        *,
        require_new_activation: bool,
    ) -> dict[str, Any] | None:
        candidates = phase_candidates[name]
        first = bisect.bisect_right(
            candidates, cycle_floor_source_stamp_ns, key=lambda candidate: candidate["source_stamp_ns"]
        )
        for candidate in candidates[first:]:
            if require_new_activation:
                activation_source_stamp_ns = candidate["activation_source_stamp_ns"]
                activation_floor_ns = cycle_floor_source_stamp_ns
                if name == "inspection_demo" and cycle_phase == "leave_complete" and current_cycle:
                    # The successor can activate before Leave publishes its
                    # SUCCESS, but must belong to the handoff after Leave
                    # actually started in this cycle.
                    activation_floor_ns = current_cycle["leave_cable_succeeded_activation_source_stamp_ns"]
                if (
                    activation_source_stamp_ns is None
                    or activation_source_stamp_ns <= last_accepted_phase_source_stamp_ns[name]
                    or activation_source_stamp_ns <= activation_floor_ns
                ):
                    continue
                if name == "leave_cable" and activation_source_stamp_ns >= candidate["source_stamp_ns"]:
                    continue
            return candidate
        return None

    def record_phase_evidence(
        cycle: dict[str, Any],
        phase_name: str,
        observation: dict[str, Any],
    ) -> None:
        cycle[f"{phase_name}_at"] = observation["receipt_at"]
        cycle[f"{phase_name}_receipt_at"] = observation["receipt_at"]
        cycle[f"{phase_name}_receipt_monotonic"] = observation["receipt_monotonic"]
        cycle[f"{phase_name}_source_stamp"] = observation["source_stamp"]
        cycle[f"{phase_name}_source_stamp_ns"] = observation["source_stamp_ns"]
        activation = observation["activation_observation"]
        if activation is not None:
            cycle[f"{phase_name}_activation_receipt_at"] = activation["receipt_at"]
            cycle[f"{phase_name}_activation_receipt_monotonic"] = activation["receipt_monotonic"]
            cycle[f"{phase_name}_activation_source_stamp"] = activation["source_stamp"]
            cycle[f"{phase_name}_activation_source_stamp_ns"] = activation["source_stamp_ns"]

    def accept_phase_evidence(
        cycle: dict[str, Any],
        phase_name: str,
        mode_name: str,
        observation: dict[str, Any],
    ) -> None:
        record_phase_evidence(cycle, phase_name, observation)
        last_accepted_phase_source_stamp_ns[mode_name] = observation["source_stamp_ns"]

    def begin_inspection_phase(observation: dict[str, Any]) -> None:
        nonlocal current_cycle, cycle_floor_source_stamp_ns, cycle_phase
        nonlocal mission_started_at, mission_started_monotonic
        if mission_started_monotonic is None:
            mission_started_monotonic = time.monotonic()
            mission_started_at = observation["receipt_at"]
        current_cycle = {}
        accept_phase_evidence(current_cycle, "inspection_started", "inspection_demo", observation)
        cycle_floor_source_stamp_ns = observation["source_stamp_ns"]
        cycle_phase = "inspection"

    def evaluate_phase_evidence() -> None:
        """Advance only from a source-ordered chain of cached exact phases."""
        nonlocal current_cycle, cycle_floor_source_stamp_ns, cycle_phase
        while True:
            if cycle_phase == "awaiting_inspection":
                observation = next_phase_candidate(
                    "inspection_demo",
                    require_new_activation=False,
                )
                if observation is None:
                    return
                begin_inspection_phase(observation)
                continue
            if current_cycle is None:
                return
            if cycle_phase == "inspection":
                observation = next_phase_candidate(
                    "reach_cable",
                    require_new_activation=True,
                )
                if observation is None:
                    return
                accept_phase_evidence(current_cycle, "reach_cable_active", "reach_cable", observation)
                cycle_floor_source_stamp_ns = observation["source_stamp_ns"]
                cycle_phase = "reach_active"
                continue
            if cycle_phase == "reach_active":
                observation = next_phase_candidate(
                    "cable_charging",
                    require_new_activation=True,
                )
                if observation is None:
                    return
                accept_phase_evidence(current_cycle, "cable_charging_active", "cable_charging", observation)
                cycle_floor_source_stamp_ns = observation["source_stamp_ns"]
                cycle_phase = "charging_active"
                continue
            if cycle_phase == "charging_active":
                observation = next_phase_candidate(
                    "leave_cable",
                    require_new_activation=True,
                )
                if observation is None:
                    return
                accept_phase_evidence(current_cycle, "leave_cable_succeeded", "leave_cable", observation)
                cycle_floor_source_stamp_ns = observation["source_stamp_ns"]
                cycle_phase = "leave_complete"
                continue
            if cycle_phase == "leave_complete":
                observation = next_phase_candidate(
                    "inspection_demo",
                    require_new_activation=True,
                )
                if observation is None:
                    return
                accept_phase_evidence(current_cycle, "inspection_resumed", "inspection_demo", observation)
                current_cycle["cycle_index"] = len(completed_cycles) + 1
                completed_cycles.append(current_cycle)
                begin_inspection_phase(observation)
                continue
            return

    def on_mode(name: str):
        def callback(message: StringStamped) -> None:
            value = _mode(message)
            if value is None:
                return
            source_stamp = _mode_source_stamp(message)
            source_stamp_ns = source_stamp["nanoseconds"]
            now = datetime.now(timezone.utc).isoformat()
            receipt_monotonic = time.monotonic()
            observation = {
                "value": value,
                "source_stamp": source_stamp,
                "source_stamp_ns": source_stamp_ns,
                "receipt_at": now,
                "receipt_monotonic": receipt_monotonic,
            }
            latest_modes[name] = value
            if reported_mode_payloads[name] != value:
                reported_mode_payloads[name] = value
                events.append(
                    {
                        "time": now,
                        "receipt_at": now,
                        "receipt_monotonic": receipt_monotonic,
                        "mode": name,
                        "status": value,
                        "source_stamp": source_stamp,
                        "source_stamp_ns": source_stamp_ns,
                    }
                )
            # Before the first fresh Inspection Demo activation, mode topics
            # may replay a terminal result from an earlier run.  It is not
            # evidence about this endurance attempt; only classify failures
            # after this run has entered its first inspection phase.
            if (
                mission_started_monotonic is not None
                and value.get("tree_finished")
                and not value.get("tree_success")
            ):
                # A terminal cancellation is cleanup evidence only if its
                # source timestamp is strictly newer than the wall-clock
                # boundary recorded when cleanup began. Delayed, missing, or
                # invalid source stamps remain causal mission failures.
                if (
                    cleanup_started_wallclock_ns is not None
                    and _positive_source_stamp(source_stamp_ns)
                    and source_stamp_ns > cleanup_started_wallclock_ns
                ):
                    post_deadline_mode_events.append(
                        {
                            "time": now,
                            "receipt_at": now,
                            "mode": name,
                            "status": value,
                            "source_stamp": source_stamp,
                            "source_stamp_ns": source_stamp_ns,
                        }
                    )
                else:
                    latch_mission_failure(
                        "mode_failed",
                        {
                            "time": now,
                            "receipt_at": now,
                            "mode": name,
                            "status": value,
                            "source_stamp": source_stamp,
                            "source_stamp_ns": source_stamp_ns,
                        },
                )
                return

            activity_index = cache_mode_activity(name, observation)
            phase_index = (
                cache_phase_observation(name, observation)
                if phase_is_eligible(name, value) else None
            )
            update_phase_candidates(name, activity_index, phase_index)
            # This is deliberately independent from reporting deduplication:
            # receipt order may be arbitrary while source order, activation
            # edges, and source-stamped phase evidence remain causal.
            evaluate_phase_evidence()
        return callback

    subscriptions = [
        node.create_subscription(MissionModeStatus, "/mission/status", on_mission, _mission_qos()),
        # PX4/uXRCE output is best-effort sensor telemetry.  A reliable
        # subscription is incompatible and silently loses the exact safety
        # samples this observer exists to retain.
        node.create_subscription(VehicleStatus, "/fmu/out/vehicle_status_v1", on_vehicle, qos_profile_sensor_data),
        node.create_subscription(VehicleLandDetected, "/fmu/out/vehicle_land_detected", on_land_detected, qos_profile_sensor_data),
        *[
            node.create_subscription(StringStamped, f"/mission/modes/{name}/status", on_mode(name), _mode_qos())
            for name in latest_modes
        ],
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    prelude_deadline = time.monotonic() + args.prelude_timeout_sec
    duration_elapsed_at: str | None = None
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.5)
            now_monotonic = time.monotonic()
            if mission_failure_latched and cleanup_started_monotonic is None:
                begin_cleanup()
            if mission_started_monotonic is None:
                if cleanup_started_monotonic is None and now_monotonic >= prelude_deadline:
                    mission_outcome = "inspection_prelude_timeout"
                    break
                if cleanup_started_monotonic is not None:
                    safety = cleanup_safety_evidence(now_monotonic)
                    if safety["safe_landed_disarmed"]:
                        cleanup_outcome = "safe_landed_disarmed"
                        break
                    if cleanup_deadline is not None and now_monotonic >= cleanup_deadline:
                        cleanup_outcome = "final_safety_timeout"
                        break
                continue
            if deadline is None:
                deadline = mission_started_monotonic + args.duration_sec
            if mission_failure_latched and cleanup_started_monotonic is None:
                begin_cleanup()
            if (
                args.finish_after_required_cycles
                and not mission_failure_latched
                and mission_outcome is None
                and len(completed_cycles) >= args.required_cycles
                and now_monotonic < deadline
            ):
                mission_outcome = "required_cycles_completed"
                begin_cleanup()
            if not mission_failure_latched and mission_outcome is None and now_monotonic >= deadline:
                mission_outcome = "duration_elapsed"
            if now_monotonic >= deadline:
                duration_elapsed_at = duration_elapsed_at or datetime.now(timezone.utc).isoformat()
                begin_cleanup()
            if cleanup_started_monotonic is not None:
                safety = cleanup_safety_evidence(now_monotonic)
                if safety["safe_landed_disarmed"]:
                    cleanup_outcome = "safe_landed_disarmed"
                    break
                if cleanup_deadline is not None and now_monotonic >= cleanup_deadline:
                    cleanup_outcome = "final_safety_timeout"
                    break
        if mission_outcome is None:
            mission_outcome = "observer_stopped"
        if cleanup_started_monotonic is None:
            cleanup_outcome = "not_started"
        elif cleanup_outcome is None:
            cleanup_outcome = "observer_stopped"
    finally:
        now_monotonic = time.monotonic()
        safety_evidence = cleanup_safety_evidence(now_monotonic)
        if mission_outcome is None:
            mission_outcome = "observer_stopped"
        if cleanup_started_monotonic is None:
            cleanup_outcome = cleanup_outcome or "not_started"
        else:
            cleanup_outcome = cleanup_outcome or (
                "safe_landed_disarmed"
                if safety_evidence["safe_landed_disarmed"]
                else "observer_stopped"
            )
        if mission_outcome == "duration_elapsed" and cleanup_outcome == "safe_landed_disarmed":
            outcome = "duration_elapsed"
        elif mission_failure_kind is not None:
            outcome = mission_failure_kind
        elif cleanup_outcome == "final_safety_timeout":
            outcome = "final_safety_timeout"
        else:
            outcome = mission_outcome
        result = {
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "mission_outcome": mission_outcome,
            "cleanup_outcome": cleanup_outcome,
            "requested_duration_sec": args.duration_sec,
            "prelude_timeout_sec": args.prelude_timeout_sec,
            "final_safe_grace_sec": args.final_safe_grace_sec,
            "finish_after_required_cycles": args.finish_after_required_cycles,
            "cleanup_freshness_sec": args.cleanup_freshness_sec,
            "mission_started_at": mission_started_at,
            "cleanup_started_at": cleanup_started_at,
            "cleanup_started_wallclock_ns": cleanup_started_wallclock_ns,
            "active_duration_sec": (
                None
                if mission_started_monotonic is None
                else max(0.0, min(args.duration_sec, time.monotonic() - mission_started_monotonic))
            ),
            "duration_elapsed_at": duration_elapsed_at,
            "requested_cycles": args.required_cycles,
            "completed_cycle_count": len(completed_cycles),
            "completed_cycles": completed_cycles,
            "in_progress_cycle": current_cycle,
            "cycle_phase_at_finish": cycle_phase,
            "mission_failures": failures,
            "mode_failures": failures,
            "mission_failure_latched": mission_failure_latched,
            "mission_failure_kind": mission_failure_kind,
            "post_deadline_mode_events": post_deadline_mode_events,
            "final_mission": latest_mission,
            "final_vehicle_status": latest_vehicle,
            "final_land_detected": latest_land_detected,
            "final_vehicle_status_evidence": safety_evidence["vehicle_status"],
            "final_land_detected_evidence": safety_evidence["land_detected"],
            "cleanup_safety_evidence": safety_evidence,
            "final_safe_landed_disarmed": safety_evidence["safe_landed_disarmed"],
            "cleanup_safe_landed_disarmed": safety_evidence["safe_landed_disarmed"],
            "final_modes": latest_modes,
            "events": events,
        }
        output_path.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        for subscription in subscriptions:
            node.destroy_subscription(subscription)
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            # rclpy may already have shut down while an external shutdown
            # interrupted spin_once; preserve the observation artifact.
            pass
    print(json.dumps(result, indent=2, default=str), flush=True)
    mission_completed = (
        result["mission_outcome"] == "required_cycles_completed"
        if args.finish_after_required_cycles
        else result["mission_outcome"] == "duration_elapsed"
    )
    return 0 if (
        mission_completed
        and len(completed_cycles) >= args.required_cycles
        and result["cleanup_outcome"] == "safe_landed_disarmed"
        and result["cleanup_safe_landed_disarmed"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
