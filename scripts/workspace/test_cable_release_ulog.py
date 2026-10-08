"""Tests for judging cable releases from PX4 flight-log topics."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cable_release_ulog as release  # noqa: E402

NAN = float("nan")
HOVER = 0.70


def _log(*, push_thrust=0.84, saturate=False, pressed_rise=0.0, land_blip=False, sag=0.0,
         velocity_push=False) -> dict:
    """A 60 s arming on the cable at 50 Hz: push from 0.1 s, airborne at 1 s,
    established at 3 s, CableTakeoff at 6 s descending 0.3 m/s."""
    t = np.arange(0.0, 60.0, 0.02)
    us = (1_000_000 + t * 1e6).astype(np.int64)
    before_takeoff = t < 6.0
    push = (t >= 0.1) & before_takeoff
    acc = np.where(push, -np.clip(0.2 + np.maximum(t - 1.0, 0.0), 0.2, 2.2), NAN)
    vel = np.where(before_takeoff, NAN, 0.3)
    if velocity_push:
        acc = np.full_like(t, NAN)
        vel = np.where(before_takeoff, -2.0, 0.3)
    pos = np.where(before_takeoff, NAN, -4.0 + 0.3 * (t - 6.0))
    thrust = np.where(t < 1.0, 0.0, np.where(t < 3.0, HOVER, 1.0 if saturate else push_thrust))
    thrust = np.where(before_takeoff, thrust, HOVER)
    z = np.where(before_takeoff, -4.0, -4.0 + 0.3 * (t - 6.0) + sag)
    z = np.where((t > 4.0) & (t < 5.0), z - pressed_rise, z)
    landed = t < 1.0
    ground_contact = landed | (land_blip & (t > 4.5) & (t < 4.6))
    return {
        "vehicle_status": {"timestamp": us, "arming_state": np.full_like(us, 2)},
        "vehicle_land_detected": {"timestamp": us, "landed": landed, "maybe_landed": landed,
                                  "ground_contact": ground_contact},
        "vehicle_thrust_setpoint": {"timestamp": us, "xyz[2]": -thrust},
        "trajectory_setpoint": {"timestamp": us, "position[2]": pos, "velocity[2]": vel,
                                "acceleration[2]": acc},
        "vehicle_local_position": {"timestamp": us, "z": z, "vz": np.gradient(z, t),
                                   "vx": np.zeros_like(t), "vy": np.zeros_like(t), "az": np.zeros_like(t)},
        "hover_thrust_estimate": {"timestamp": us, "hover_thrust": np.full_like(t, HOVER),
                                  "valid": t > 30.0},
    }


def test_a_bounded_push_that_holds_the_cable_passes() -> None:
    result = release.analyze(_log())
    assert result["failures"] == []
    assert result["airborne_s"] == 1.0
    assert result["push_established_s"] == 3.0
    assert result["takeoff_start_s"] == 6.0
    assert result["push_acceleration_m_s2"] == 2.2
    assert result["push_thrust_over_hover_min"] == 1.2
    assert result["land_detector_before_takeoff"] == {"landed": False, "maybe_landed": False,
                                                      "ground_contact": False}


def test_each_release_criterion_fails_on_its_own() -> None:
    assert any("saturates" in f for f in release.analyze(_log(saturate=True))["failures"])
    assert any("does not carry" in f for f in release.analyze(_log(push_thrust=0.72))["failures"])
    assert any("moved while pressed" in f for f in release.analyze(_log(pressed_rise=0.08))["failures"])
    assert any("land detector" in f for f in release.analyze(_log(land_blip=True))["failures"])
    assert any("sagged" in f for f in release.analyze(_log(sag=0.10))["failures"])


def test_logs_without_an_acceleration_push_are_not_cable_releases() -> None:
    # Ground takeoffs, and releases flown before the push became an
    # acceleration setpoint, are not judged.
    assert release.analyze(_log(velocity_push=True)) is None


def test_hover_thrust_falls_back_to_steady_flight_when_the_estimate_is_not_logged() -> None:
    log = _log()
    del log["hover_thrust_estimate"]
    # Hold still after the descent so steady flight exists.
    t = (log["vehicle_local_position"]["timestamp"] - 1_000_000) / 1e6
    log["vehicle_local_position"]["vz"] = np.where((t >= 6.0) & (t <= 20.0), 0.3, 0.0)
    result = release.analyze(log)
    assert result["hover_thrust"] == HOVER
    assert result["failures"] == []


def _with_truth(log: dict, truth_rise: float) -> dict:
    """The simulator's ground truth: still while pressed except truth_rise."""
    local = log["vehicle_local_position"]
    t = (local["timestamp"] - local["timestamp"][0]) / 1e6
    z = np.where(t < 6.0, -4.0, local["z"])
    z = np.where((t > 4.0) & (t < 5.0), z - truth_rise, z)
    log["vehicle_local_position_groundtruth"] = {"timestamp": local["timestamp"], "z": z,
                                                 "vz": np.gradient(z, t)}
    return log


def test_estimate_drift_while_pressed_is_judged_on_ground_truth() -> None:
    # HIL 2026-10-05: the estimated altitude moved 6.3 cm while the simulator's
    # ground truth stayed within 1 mm.
    result = release.analyze(_with_truth(_log(pressed_rise=0.063), truth_rise=0.0))
    assert result["pressed_source"] == "groundtruth"
    assert not any("moved while pressed" in f for f in result["failures"])


def test_real_motion_while_pressed_still_fails_on_ground_truth() -> None:
    result = release.analyze(_with_truth(_log(), truth_rise=0.08))
    assert any("moved while pressed" in f for f in result["failures"])

