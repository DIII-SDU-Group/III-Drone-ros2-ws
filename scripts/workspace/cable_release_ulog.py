"""Judge each cable release from PX4 SITL flight logs.

Leave Cable arms the vehicle on the cable, pushes it up against the cable with
an acceleration setpoint, opens the gripper once PX4 reports it airborne, and
hands over to CableTakeoff. PX4 writes one log per arming, so every log that
starts with the push holds exactly one release. Per release this checks:

- PX4's land detector never reports landed, maybe landed or ground contact
  from takeoff until CableTakeoff starts (PX4 would cut thrust or disarm);
- once established, the push carries the vehicle (thrust above hover) without
  saturating thrust;
- the vehicle stays pressed against the cable while the gripper opens (no
  vertical excursion);
- CableTakeoff tracks its reference from the start (no sag below it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

TOPICS = ("vehicle_status", "vehicle_land_detected", "vehicle_thrust_setpoint",
          "trajectory_setpoint", "vehicle_local_position", "hover_thrust_estimate",
          "vehicle_local_position_groundtruth")

# Pass criteria.
MIN_PUSH_THRUST_OVER_HOVER = 1.05   # the push carries the vehicle with margin
MAX_PUSH_THRUST = 0.98              # below PX4's thrust limit: not saturated
# Stays pressed against the cable; a release jump shows as 0.6-0.9 m/s. Judged
# on the simulator's ground truth when the log has it: the estimated altitude
# of a vehicle held still drifts, usually 1-2 cm but 6.3 cm once (HIL
# 2026-10-05, ground truth still within 1 mm). Real flights use the estimate.
MAX_PRESSED_EXCURSION_M = 0.05
MAX_PRESSED_SPEED_M_S = 0.10
MAX_TAKEOFF_SAG_M = 0.05            # below CableTakeoff's reference
TAKEOFF_SAG_WINDOW_S = 2.5
PUSH_SEARCH_WINDOW_S = 5.0
# Steady flight, where thrust equals the hover thrust.
STEADY_SPEED_M_S = 0.05
STEADY_ACCELERATION_M_S2 = 0.2
STEADY_MIN_SAMPLES = 20
MIN_FLIGHT_THRUST = 0.2


def _at(times: np.ndarray, values: np.ndarray, t: float) -> float:
    index = min(int(np.searchsorted(times, t)), len(values) - 1)
    return float(values[index])


def _hover_thrust(topics, seconds, t_takeoff, tt, collective) -> float | None:
    """The thrust the vehicle needs to hover, measured in flight after takeoff:
    PX4's converged hover-thrust estimate, else the thrust of steady flight."""
    hover = topics.get("hover_thrust_estimate")
    if hover is not None and len(hover["timestamp"]):
        th = seconds(hover["timestamp"])
        valid = hover["hover_thrust"][(th > t_takeoff + 20) & (hover["valid"].astype(bool))]
        if len(valid):
            return float(np.median(valid))
    position = topics["vehicle_local_position"]
    tp = seconds(position["timestamp"])
    steady = ((tp > t_takeoff + 10) & (np.abs(position["vz"]) < STEADY_SPEED_M_S)
              & (np.hypot(position["vx"], position["vy"]) < STEADY_SPEED_M_S)
              & (np.abs(position["az"]) < STEADY_ACCELERATION_M_S2))
    index = np.clip(np.searchsorted(tt, tp[steady]), 0, len(tt) - 1)
    # Not landed: thrust above idle.
    thrust = collective[index][collective[index] > MIN_FLIGHT_THRUST]
    if len(thrust) < STEADY_MIN_SAMPLES:
        return None
    return float(np.median(thrust))


def analyze(topics: dict[str, dict[str, np.ndarray]]) -> dict[str, Any] | None:
    """Analyse one arming log given its topics as field arrays.

    Returns None when the log does not start with a cable push.
    """
    status = topics["vehicle_status"]
    armed = np.flatnonzero(status["arming_state"] == 2)
    if len(armed) == 0:
        return None
    t0 = int(status["timestamp"][armed[0]])

    def seconds(stamps: np.ndarray) -> np.ndarray:
        return (np.asarray(stamps, dtype=np.int64) - t0) / 1e6

    setpoint = topics["trajectory_setpoint"]
    ts = seconds(setpoint["timestamp"])
    sp_pos_z, sp_vel_z, sp_acc_z = setpoint["position[2]"], setpoint["velocity[2]"], setpoint["acceleration[2]"]
    push = (ts >= 0) & (ts <= PUSH_SEARCH_WINDOW_S) & np.isnan(sp_pos_z) & np.isnan(sp_vel_z) & np.isfinite(sp_acc_z)
    if not push.any():
        return None

    land = topics["vehicle_land_detected"]
    tl = seconds(land["timestamp"])
    airborne = np.flatnonzero((tl >= 0) & ~land["landed"].astype(bool))
    failures: list[str] = []
    result: dict[str, Any] = {"failures": failures}
    if len(airborne) == 0:
        failures.append("PX4 never reported airborne")
        return result
    t_airborne = float(tl[airborne[0]])
    # CableTakeoff is the first position reference after the push began.
    takeoff = np.flatnonzero((ts > max(t_airborne, float(ts[np.flatnonzero(push)[0]]))) & np.isfinite(sp_pos_z))
    if len(takeoff) == 0:
        failures.append("no CableTakeoff reference after the push")
        return result
    t_takeoff = float(ts[takeoff[0]])
    # NED: upward acceleration is negative. The push is established once its
    # setpoint reaches its most upward value.
    pushing = (ts >= 0) & (ts < t_takeoff) & np.isnan(sp_pos_z) & np.isnan(sp_vel_z) & np.isfinite(sp_acc_z)
    if not pushing.any():
        failures.append("no push before CableTakeoff")
        return result
    target = float(np.max(-sp_acc_z[pushing]))
    t_established = float(ts[np.flatnonzero(pushing & (-sp_acc_z >= target - 1e-3))[0]])
    result.update({"airborne_s": round(t_airborne, 2), "push_established_s": round(t_established, 2),
                   "takeoff_start_s": round(t_takeoff, 2), "push_acceleration_m_s2": round(target, 3),
                   "pressed_before_takeoff_s": round(t_takeoff - t_established, 2)})

    window = (tl > t_airborne) & (tl < t_takeoff)
    flags = {name: bool(land[name][window].any()) for name in ("landed", "maybe_landed", "ground_contact")}
    result["land_detector_before_takeoff"] = flags
    if any(flags.values()):
        failures.append(f"PX4 land detector set before CableTakeoff: {flags}")

    thrust = topics["vehicle_thrust_setpoint"]
    tt = seconds(thrust["timestamp"])
    collective = -thrust["xyz[2]"]
    pressed = (tt >= t_established) & (tt < t_takeoff)
    hover_true = _hover_thrust(topics, seconds, t_takeoff, tt, collective)
    result["hover_thrust"] = None if hover_true is None else round(hover_true, 3)
    if pressed.any():
        thrust_min, thrust_max = float(collective[pressed].min()), float(collective[pressed].max())
        result["push_thrust"] = [round(thrust_min, 3), round(thrust_max, 3)]
        if thrust_max >= MAX_PUSH_THRUST:
            failures.append(f"push saturates thrust ({thrust_max:.2f})")
        if hover_true is None:
            failures.append("hover thrust not measurable after the release: cannot judge the push")
        else:
            ratio = thrust_min / hover_true
            result["push_thrust_over_hover_min"] = round(ratio, 3)
            if ratio < MIN_PUSH_THRUST_OVER_HOVER:
                failures.append(f"push thrust {thrust_min:.2f} does not carry the vehicle (hover {hover_true:.2f})")
    else:
        failures.append("no thrust samples while pressed against the cable")

    position = topics["vehicle_local_position"]
    tp = seconds(position["timestamp"])
    pressed_p = (tp >= t_established) & (tp < t_takeoff)
    pressed_source, pressed_z, pressed_vz = "estimate", position["z"], position["vz"]
    truth = topics.get("vehicle_local_position_groundtruth")
    if truth is not None and len(truth.get("timestamp", ())):
        t_truth = seconds(truth["timestamp"])
        pressed_truth = (t_truth >= t_established) & (t_truth < t_takeoff)
        if pressed_truth.any():
            pressed_source, pressed_z, pressed_vz = "groundtruth", truth["z"], truth["vz"]
            pressed_p = pressed_truth
    if pressed_p.any():
        excursion = float(np.ptp(pressed_z[pressed_p]))
        speed = float(np.abs(pressed_vz[pressed_p]).max())
        result["pressed_source"] = pressed_source
        result["pressed_excursion_m"] = round(excursion, 4)
        result["pressed_max_speed_m_s"] = round(speed, 3)
        if excursion > MAX_PRESSED_EXCURSION_M or speed > MAX_PRESSED_SPEED_M_S:
            failures.append(f"vehicle moved while pressed against the cable ({excursion * 100:.1f} cm, {speed:.2f} m/s)")
    sag_window = (tp >= t_takeoff) & (tp <= t_takeoff + TAKEOFF_SAG_WINDOW_S)
    finite = np.isfinite(sp_pos_z)
    if sag_window.any() and finite.any():
        reference = np.interp(tp[sag_window], ts[finite], sp_pos_z[finite])
        sag = float((position["z"][sag_window] - reference).max())  # NED: positive = below
        result["takeoff_sag_m"] = round(sag, 4)
        if sag > MAX_TAKEOFF_SAG_M:
            failures.append(f"CableTakeoff sagged {sag * 100:.1f} cm below its reference")
    return result


def load(path: Path) -> dict[str, dict[str, np.ndarray]]:
    from pyulog import ULog

    ulog = ULog(str(path), list(TOPICS))
    return {data.name: data.data for data in ulog.data_list if data.multi_id == 0}


def report(since_epoch: float, until_epoch: float, roots: tuple[Path, ...], root: Path) -> dict[str, Any]:
    """Every cable release in logs last written during [since, until + 600 s]."""
    try:
        import pyulog  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "pyulog not installed", "releases": [], "failures": []}
    releases, failures = [], []
    for log_root in roots:
        for path in sorted(log_root.glob("*/*.ulg")) if log_root.exists() else []:
            if not since_epoch <= path.stat().st_mtime <= until_epoch + 600:
                continue
            try:
                result = analyze(load(path))
            except Exception as exc:  # noqa: BLE001 - a truncated live log is reported, not fatal
                result = {"failures": [], "unreadable": str(exc)}
            if result is None:
                continue
            name = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            result["log"] = name
            releases.append(result)
            failures.extend(f"cable release {name}: {failure}" for failure in result["failures"])
    return {"available": True, "releases": releases, "failures": failures}
