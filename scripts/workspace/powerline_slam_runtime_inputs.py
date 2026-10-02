#!/usr/bin/env python3
"""Build powerline SLAM estimator inputs from III configuration and III flights.

Subcommands (ROS is not needed):

calibration
    The estimator's runtime calibration set (powerline_slam schema, as
    config/r22/runtime_calibration_U0_F50_C20_R0) for the
    d4s_dc_drone_powerline_eval layout, derived from the III sim parameter set
    (/tf/sim/drone_to_mmwave, /tf/sim/powerline_eval/*) and the variant's
    camera model. Radar profile Doppler calibrations are copied from a
    powerline_slam calibration set, because they depend on the radar profile,
    not the mount. --compare reports differences against such a set.

mission-priors
    The commanded-position prior sidecar (schema 4) for one flight of
    powerline_slam_flights.py, from its mission_phase_evidence.json: one prior
    per leg after the first, valid from leg begin + 5 s, with the leg's flown
    command in the Gazebo world ENU frame (powerline_slam WO002_SIM_DESIGN_ENU)
    and the per-leg pose sigmas of the development pose-tracking policy.

map-prior
    A powerline_slam map prior converted from WO002_SIM_DESIGN_ENU (Gazebo
    world ENU) to the III world frame (north-west-up) with a flight's live
    Gazebo-to-ROS mapping (flight_plan.json).

imu-covariance
    The gyro covariance (drone FLU, rad^2/s^2) from /fmu/out/sensor_combined,
    with the stationarity checks of the powerline_slam builder: --ground from a
    run's ground_imu segment, disarmed on the ground, which measures sensor
    noise only; --flight from the four guarded hover holds of each flight (the
    powerline_slam rule), which under III's flight stack include the vehicle's
    motion. Reads bags, so it needs the sourced ROS workspace (devcontainer).

--to-world FLIGHT_PLAN expresses the mission priors in the III world frame too;
map prior and mission priors of one estimator run must share a map frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any, Sequence
import xml.etree.ElementTree as ET

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SIM_PARAMETER_SET = (
    WORKSPACE_ROOT / "src/III-Drone-Configuration/config/parameter_sets/sim/tracked/default.yaml"
)
VARIANT_SDF = (
    WORKSPACE_ROOT
    / "src/III-Drone-Simulation/Gazebo-simulation-assets/models/d4s_dc_drone_powerline_eval/model.sdf"
)
MAP_FRAME = "WO002_SIM_DESIGN_ENU"
SETTLE_NS = 5_000_000_000
# Development pose-tracking policy of the powerline_slam dev01-dev13 flights
# (05_POSE_TRACKING_POLICY_v1.json): 0.3 m and 0.03 rad for every leg after
# the first, which carries no prior.
DEFAULT_POSITION_SIGMA_M = 0.3
DEFAULT_ROTATION_SIGMA_RAD = 0.03
DOPPLER_FILES = ("doppler_calibration.json", "doppler_calibration_radar_forward.json")
# Guarded hover-hold windows of the powerline_slam gyro covariance builder.
HOLD_SETTLE_NS = 2_000_000_000
HOLD_END_GUARD_NS = 500_000_000
ARMING_STATE_DISARMED = 1
# III world (north-west-up) from Gazebo world ENU; the translation is the
# flight's live mapping offset.
R_WORLD_FROM_DESIGN = ((0.0, 1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0))

# powerline_slam conventions (raw_adapter.R_ENU_FROM_NED,
# frame_conventions.R_DRONE_FROM_PX4_FRD).
R_ENU_FROM_NED = ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
R_FLU_FROM_FRD = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def rotation_ypr(yaw: float, pitch: float, roll: float) -> list[list[float]]:
    """Rz(yaw) Ry(pitch) Rx(roll), the SDF and static_transform_publisher convention."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def quaternion_xyzw(r: Sequence[Sequence[float]]) -> list[float]:
    trace = r[0][0] + r[1][1] + r[2][2]
    if trace > 0.0:
        s = 2.0 * math.sqrt(trace + 1.0)
        q = [(r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s]
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = 2.0 * math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2])
        q = [0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s]
    elif r[1][1] > r[2][2]:
        s = 2.0 * math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2])
        q = [(r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s]
    else:
        s = 2.0 * math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1])
        q = [(r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s]
    if q[3] < 0.0:
        q = [-value for value in q]
    return q


def roll_pitch_yaw(r: Sequence[Sequence[float]]) -> list[float]:
    pitch = math.asin(max(-1.0, min(1.0, -r[2][0])))
    if abs(math.cos(pitch)) < 1e-9:
        # Gimbal lock (sensor pointing straight up or down): fold yaw into roll.
        return [math.atan2(-r[1][2], r[1][1]), pitch, 0.0]
    return [math.atan2(r[2][1], r[2][2]), pitch, math.atan2(r[1][0], r[0][0])]


def sim_parameters(path: Path = SIM_PARAMETER_SET) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())["/**"]["ros__parameters"]


def extrinsics(mount: Sequence[float], child_frame: str) -> dict[str, Any]:
    """[x, y, z, yaw, pitch, roll] III mount -> powerline_slam radar extrinsics."""
    rotation = rotation_ypr(mount[3], mount[4], mount[5])
    return {
        "parent_frame": "drone",
        "child_frame": child_frame,
        "translation_m": [float(value) for value in mount[:3]],
        "quaternion_xyzw": quaternion_xyzw(rotation),
        "source_pose_rpy_rad": roll_pitch_yaw(rotation),
        "sensor_frame_convention": "ROS FLU/Gazebo sensor frame; no PX4 FRD flip applies to this SDF extrinsic",
    }


def camera_intrinsics(sdf_path: Path = VARIANT_SDF) -> dict[str, Any]:
    """Ideal pinhole of the variant's cable camera, as the sensor plugin publishes it."""
    camera = ET.parse(sdf_path).getroot().find(".//sensor[@name='cable_camera']/camera")
    width = int(camera.findtext("image/width"))
    height = int(camera.findtext("image/height"))
    focal = 0.5 * width / math.tan(0.5 * float(camera.findtext("horizontal_fov")))
    cx, cy = 0.5 * (width - 1), 0.5 * (height - 1)
    return {
        "source": "source-derived-model-calibration; plugin CameraInfo formula; "
                  "III d4s_dc_drone_powerline_eval model.sdf; CameraInfo bag check required",
        "image_width": width,
        "image_height": height,
        "camera_matrix": {"rows": 3, "cols": 3, "data": [focal, 0.0, cx, 0.0, focal, cy, 0.0, 0.0, 1.0]},
        "distortion_model": "plumb_bob",
        "distortion_coefficients": {"rows": 1, "cols": 5, "data": [0.0] * 5},
        "projection_matrix": {"rows": 3, "cols": 4, "data": [focal, 0.0, cx, 0.0, 0.0, focal, cy, 0.0, 0.0, 0.0, 1.0, 0.0]},
    }


def build_calibration(output: Path, doppler_source: Path) -> dict[str, Any]:
    params = sim_parameters()
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    output.mkdir(parents=True)
    radar_up = extrinsics(params["/tf/sim/drone_to_mmwave"], params["/tf/mmwave_frame_id"])
    radar_forward = extrinsics(
        params["/tf/sim/powerline_eval/drone_to_mmwave_forward"], params["/tf/mmwave_forward_frame_id"])
    camera_mount = params["/tf/sim/powerline_eval/drone_to_cable_camera"]
    camera_rotation = rotation_ypr(camera_mount[3], camera_mount[4], camera_mount[5])
    camera = {
        "calibration_kind": "source-derived-model-calibration",
        "extrinsics": {
            "child_sensor_frame": params["/tf/cable_camera_frame_id"],
            "parent_frame": "drone",
            "rotation_matrix_parent_from_sensor_row_major": [value for row in camera_rotation for value in row],
            "sensor_frame_convention": "Gazebo camera sensor FLU; loader composes its fixed optical-to-Gazebo basis exactly once",
            "source_parent_frame": "base_link",
            "source_parent_to_drone": "identity: runtime plugin link_name base_link is canonical drone",
            "source_pose_rpy_rad": roll_pitch_yaw(camera_rotation),
            "translation_m": dict(zip("xyz", (float(value) for value in camera_mount[:3]))),
        },
        "schema_version": 1,
    }

    def radar_document(body: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": 1, "calibration_kind": "source-derived-model-calibration", "radar_extrinsics": body}

    (output / "radar_up_extrinsics.yaml").write_text(yaml.safe_dump(radar_document(radar_up), sort_keys=False))
    # v13-compatible alias of Radar-U, as in the powerline_slam calibration sets.
    shutil.copyfile(output / "radar_up_extrinsics.yaml", output / "radar_extrinsics.yaml")
    (output / "radar_forward_extrinsics.yaml").write_text(
        yaml.safe_dump(radar_document(radar_forward), sort_keys=False))
    (output / "camera_extrinsics.json").write_text(json.dumps(camera, indent=2, sort_keys=True) + "\n")
    (output / "camera_intrinsics.yaml").write_text(yaml.safe_dump(camera_intrinsics(), sort_keys=False))
    for name in DOPPLER_FILES:
        shutil.copyfile(doppler_source / name, output / name)
    manifest = {
        "schema_version": 1,
        "variant": "iii_d4s_dc_drone_powerline_eval",
        "derived_from": {
            "sim_parameter_set": {"path": str(SIM_PARAMETER_SET.relative_to(WORKSPACE_ROOT)),
                                  "sha256": sha256_file(SIM_PARAMETER_SET)},
            "variant_model": {"path": str(VARIANT_SDF.relative_to(WORKSPACE_ROOT)), "sha256": sha256_file(VARIANT_SDF)},
            "doppler_calibrations": {name: {"source": str(doppler_source / name),
                                            "sha256": sha256_file(doppler_source / name)} for name in DOPPLER_FILES},
        },
        "radar_extrinsics.yaml": "v13-compatible alias of radar_up (frame mmwave)",
        "files_sha256": {path.name: sha256_file(path) for path in sorted(output.iterdir())},
    }
    (output / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def compare_calibration(generated: Path, reference: Path) -> dict[str, Any]:
    """Numeric differences per file; frame names are expected to differ (mmwave vs radar_up)."""
    def flat(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            items: dict[str, Any] = {}
            for key, child in value.items():
                items.update(flat(child, f"{prefix}.{key}" if prefix else str(key)))
            return items
        if isinstance(value, list):
            items = {}
            for index, child in enumerate(value):
                items.update(flat(child, f"{prefix}[{index}]"))
            return items
        return {prefix: value}

    def load(path: Path) -> Any:
        return json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())

    report: dict[str, Any] = {}
    for path in sorted(generated.iterdir()):
        if path.name == "SOURCE_MANIFEST.json" or not (reference / path.name).is_file():
            continue
        ours, theirs = flat(load(path)), flat(load(reference / path.name))
        differences = {}
        for key in sorted(set(ours) | set(theirs)):
            a, b = ours.get(key), theirs.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(float(a) - float(b)) > 1e-9:
                    differences[key] = [a, b]
            elif a != b:
                differences[key] = [a, b]
        report[path.name] = differences or "identical"
    return report


def live_offset(flight_plan_path: Path) -> list[float]:
    offset = json.loads(flight_plan_path.read_text())["live_mapping"]["offset"]
    return [float(offset[axis]) for axis in "xyz"]


def design_to_world(position: Sequence[float], offset: Sequence[float]) -> list[float]:
    """Gazebo ENU -> III world: x = y_gz + dx, y = -x_gz + dy, z = z_gz + dz."""
    return [sum(R_WORLD_FROM_DESIGN[i][k] * position[k] for k in range(3)) + offset[i] for i in range(3)]


def rotate_covariance(block: Sequence[Sequence[float]], rotations: Sequence[Sequence[Sequence[float]]],
                      columns: Sequence[Sequence[Sequence[float]]] | None = None) -> list[list[float]]:
    """Return L C R^T for block-diagonal L (rotations) and R (columns, default L), 3x3 blocks."""
    columns = rotations if columns is None else columns
    rows, cols = 3 * len(rotations), 3 * len(columns)
    left = [[0.0] * rows for _ in range(rows)]
    right = [[0.0] * cols for _ in range(cols)]
    for blocks, matrix in ((rotations, left), (columns, right)):
        for b, rotation in enumerate(blocks):
            for i in range(3):
                for j in range(3):
                    matrix[3 * b + i][3 * b + j] = rotation[i][j]
    product = [[sum(left[i][k] * block[k][j] for k in range(rows)) for j in range(cols)] for i in range(rows)]
    return [[sum(product[i][k] * right[j][k] for k in range(cols)) for j in range(cols)] for i in range(rows)]


def build_mission_priors(evidence_path: Path, position_sigma_m: float, rotation_sigma_rad: float,
                         flight_plan_path: Path | None = None) -> dict[str, Any]:
    evidence = json.loads(evidence_path.read_text())
    if evidence.get("status") != "VALID" or evidence.get("source_time_unit") != "nanoseconds":
        raise SystemExit("mission phase evidence is not VALID or not in nanoseconds")
    phases = evidence["planned_phases"]
    if any(phase.get("status", "COMPLETE") != "COMPLETE" for phase in phases):
        raise SystemExit("mission phase evidence has an incomplete phase")
    offset = live_offset(flight_plan_path) if flight_plan_path else None
    for left, right in zip(phases, phases[1:]):
        if left["source_time_interval_ns"]["end"] != right["source_time_interval_ns"]["begin"]:
            raise SystemExit("mission phases are not contiguous")
    covariance = [[0.0] * 6 for _ in range(6)]
    for index in range(3):
        covariance[index][index] = position_sigma_m ** 2
        covariance[index + 3][index + 3] = rotation_sigma_rad ** 2
    priors, excluded = [], []
    for index, phase in enumerate(phases):
        begin = int(phase["source_time_interval_ns"]["begin"])
        # Disjoint ownership: a leg ends 1 ns before the next leg begins.
        end = int(phase["source_time_interval_ns"]["end"]) - (1 if index + 1 < len(phases) else 0)
        settled = begin + SETTLE_NS
        if index == 0 or settled > end:
            excluded.append([begin, end])
            continue
        excluded.append([begin, settled - 1])
        command = phase["planned_command"]
        yaw = float(command["yaw_rad"])
        c, s = math.cos(yaw), math.sin(yaw)
        ned_from_frd = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
        position = [float(value) for value in command["target_world_m"]]
        rotation = matmul(matmul(R_ENU_FROM_NED, ned_from_frd), R_FLU_FROM_FRD)
        if offset is not None:
            position = design_to_world(position, offset)
            rotation = matmul(R_WORLD_FROM_DESIGN, rotation)
        priors.append({
            "map_frame_id": MAP_FRAME if offset is None else "world",
            "valid_from_source_time_ns": settled,
            "valid_until_source_time_ns": end,
            "commanded_position_map_m": position,
            "rotation_map_from_drone": rotation,
            "covariance_map_pose": covariance,
        })
    return {
        "schema_version": 4,
        "source_time_settled_delay_ns": SETTLE_NS,
        "excluded_no_anchor_intervals_source_time_ns": excluded,
        "priors": priors,
    }


def convert_map_prior(source_path: Path, flight_plan_path: Path) -> dict[str, Any]:
    prior = json.loads(source_path.read_text())
    if prior.get("map_frame_id") != MAP_FRAME:
        raise SystemExit(f"map prior is not in {MAP_FRAME}")
    offset = live_offset(flight_plan_path)
    rotation = R_WORLD_FROM_DESIGN
    converted = json.loads(json.dumps(prior))
    converted["map_frame_id"] = "world"
    for landmark in converted["landmarks"]:
        landmark["position_map_m"] = design_to_world(landmark["position_map_m"], offset)
        landmark["covariance_global_m2"] = rotate_covariance(landmark["covariance_global_m2"], [rotation])
    policy = converted["joint_covariance_policy"]
    count = len(policy["landmark_ids"])
    policy["map_frame_id"] = "world"
    policy["landmark_joint_covariance_global_m2"] = rotate_covariance(
        policy["landmark_joint_covariance_global_m2"], [rotation] * count)
    # Registration error [origin_map_m, rotation_vector_map] is map-frame expressed too.
    policy["registration_landmark_cross_covariance"] = rotate_covariance(
        policy["registration_landmark_cross_covariance"], [rotation] * 2, [rotation] * count)
    converted["conversion"] = {
        "from_frame": MAP_FRAME,
        "to_frame": "III world: north-west-up at the flight controller's local origin",
        "source": {"path": str(source_path), "sha256": sha256_file(source_path)},
        "live_mapping": {"path": str(flight_plan_path), "sha256": sha256_file(flight_plan_path),
                         "offset_m": offset},
    }
    return converted


def stationary_windows(evidence: dict[str, Any]) -> list[tuple[str, int, int]]:
    windows = []
    for phase in evidence["planned_phases"]:
        if not phase["name"].endswith("_hold"):
            continue
        if float(phase["planned_command"]["duration_s"]) < 5.0:
            raise SystemExit(f"hold {phase['name']} is shorter than 5 s")
        interval = phase["source_time_interval_ns"]
        begin = int(interval["begin"]) + HOLD_SETTLE_NS
        end = int(interval["end"]) - HOLD_END_GUARD_NS
        if begin >= end:
            raise SystemExit(f"hold {phase['name']} has no guarded stationary window")
        windows.append((phase["name"], begin, end))
    if len(windows) != 4:
        raise SystemExit("four hold windows required per flight")
    return windows


def stationary_window(gyro: Any, accel: Any, label: str) -> dict[str, Any]:
    """Mean-free gyro samples (drone FLU) of one window, rejected unless the IMU is stationary."""
    import numpy as np

    gyro, accel = np.asarray(gyro, dtype=float), np.asarray(accel, dtype=float)
    gyro_mean = gyro.mean(axis=0)
    accel_norm_mean = float(np.linalg.norm(accel, axis=1).mean())
    accel_axis_std_norm = float(np.linalg.norm(accel.std(axis=0, ddof=1)))
    if (np.linalg.norm(gyro_mean) > 0.01 or abs(accel_norm_mean - 9.81) > 0.5
            or accel_axis_std_norm > 0.1):
        raise SystemExit(f"{label}: the IMU does not corroborate a stationary window")
    return {"centered": gyro - gyro_mean, "sample_count": len(gyro),
            "gyro_mean_norm_radps": float(np.linalg.norm(gyro_mean)),
            "accel_norm_mean_mps2": accel_norm_mean, "accel_axis_std_norm_mps2": accel_axis_std_norm}


def pooled_gyro_covariance(windows: Sequence[dict[str, Any]], source: str,
                           evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np

    data = np.concatenate([window["centered"] for window in windows])
    covariance = data.T @ data / (len(data) - len(windows))
    covariance = (covariance + covariance.T) / 2
    if len(data) < 4000 or float(np.linalg.eigvalsh(covariance)[0]) <= 0.0:
        raise SystemExit("gyro covariance has too few samples or is not positive definite")
    return {"angular_velocity_covariance_drone_rad2ps2": covariance.tolist(),
            "emitted_rate_unit": "rad/s", "covariance_unit": "rad^2/s^2", "frame": "drone_FLU",
            "source": source, "stationary_windows": list(evidence)}


def window_evidence(statistics: dict[str, Any], **identity: Any) -> dict[str, Any]:
    return {**identity, **{key: value for key, value in statistics.items() if key != "centered"}}


def read_sensor_combined(bag_dir: Path, label: str) -> tuple[list[tuple[int, Any, Any]], list[int]]:
    """SensorCombined samples (source ns, gyro FLU, accel FLU) and VehicleStatus arming states of a bag."""
    import numpy as np
    from rclpy.serialization import deserialize_message
    import rosbag2_py
    from px4_msgs.msg import SensorCombined, VehicleStatus

    frd_to_flu = np.asarray(R_FLU_FROM_FRD)
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id=""),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/fmu/out/sensor_combined", "/fmu/out/vehicle_status_v1"]))
    samples, arming_states, last = [], [], -1
    while reader.has_next():
        topic, raw, _ = reader.read_next()
        if topic == "/fmu/out/vehicle_status_v1":
            arming_states.append(int(deserialize_message(raw, VehicleStatus).arming_state))
            continue
        message = deserialize_message(raw, SensorCombined)
        # UXRCE_DDS_SYNCT=0: PX4 timestamps share the /clock domain of the evidence.
        source_ns = int(message.timestamp) * 1000
        if source_ns <= last:
            raise SystemExit(f"{label}: SensorCombined source time reset or duplicate")
        last = source_ns
        if int(message.gyro_clipping) != 0:
            raise SystemExit(f"{label}: clipped gyro sample")
        samples.append((source_ns, frd_to_flu @ np.asarray(message.gyro_rad, dtype=float),
                        frd_to_flu @ np.asarray(message.accelerometer_m_s2, dtype=float)))
    return samples, arming_states


def build_imu_covariance(flight_dirs: Sequence[Path]) -> dict[str, Any]:
    """Pooled over the guarded hover holds of the flights (the powerline_slam rule)."""
    windows, evidence = [], []
    for flight in flight_dirs:
        holds = stationary_windows(json.loads((flight / "mission_phase_evidence.json").read_text()))
        samples, _ = read_sensor_combined(flight / "bag", flight.name)
        for name, begin, end in holds:
            rows = [row for row in samples if begin <= row[0] <= end]
            if len(rows) < 200:
                raise SystemExit(f"{flight.name}/{name}: fewer than 200 IMU samples")
            window = stationary_window([row[1] for row in rows], [row[2] for row in rows], f"{flight.name}/{name}")
            windows.append(window)
            evidence.append(window_evidence(window, flight=str(flight), phase=name, source_time_window_ns=[begin, end]))
    return pooled_gyro_covariance(windows, "hover_holds", evidence)


def ground_gyro_covariance(samples: Sequence[tuple[int, Any, Any]], arming_states: Sequence[int]) -> dict[str, Any]:
    """From a segment disarmed on the ground: sensor noise without vehicle motion."""
    if not arming_states or any(int(state) != ARMING_STATE_DISARMED for state in arming_states):
        raise SystemExit("the ground segment is not disarmed throughout")
    if not samples:
        raise SystemExit("the ground segment has no IMU samples")
    begin = samples[0][0] + HOLD_SETTLE_NS
    end = samples[-1][0] - HOLD_END_GUARD_NS
    rows = [row for row in samples if begin <= row[0] <= end]
    if len(rows) < 2:
        raise SystemExit("the ground segment is too short")
    window = stationary_window([row[1] for row in rows], [row[2] for row in rows], "ground")
    evidence = [window_evidence(window, window="disarmed_ground", source_time_window_ns=[begin, end])]
    return pooled_gyro_covariance([window], "disarmed_ground", evidence)


def build_ground_imu_covariance(ground_dir: Path) -> dict[str, Any]:
    samples, arming_states = read_sensor_combined(ground_dir / "bag", ground_dir.name)
    document = ground_gyro_covariance(samples, arming_states)
    document["stationary_windows"][0]["ground_imu"] = str(ground_dir)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    calibration = commands.add_parser("calibration")
    calibration.add_argument("--output", type=Path, required=True)
    calibration.add_argument("--doppler-source", type=Path, required=True,
                             help="powerline_slam calibration set providing the radar-profile Doppler files")
    calibration.add_argument("--compare", type=Path, help="powerline_slam calibration set to compare against")
    mission = commands.add_parser("mission-priors")
    mission.add_argument("--evidence", type=Path, required=True)
    mission.add_argument("--output", type=Path, required=True)
    mission.add_argument("--position-sigma-m", type=float, default=DEFAULT_POSITION_SIGMA_M)
    mission.add_argument("--rotation-sigma-rad", type=float, default=DEFAULT_ROTATION_SIGMA_RAD)
    mission.add_argument("--to-world", type=Path, metavar="FLIGHT_PLAN",
                         help="express the priors in the III world frame with this flight's live mapping")
    map_prior = commands.add_parser("map-prior")
    map_prior.add_argument("--source", type=Path, required=True)
    map_prior.add_argument("--flight-plan", type=Path, required=True)
    map_prior.add_argument("--output", type=Path, required=True)
    imu = commands.add_parser("imu-covariance")
    source = imu.add_mutually_exclusive_group(required=True)
    source.add_argument("--ground", type=Path, metavar="GROUND_IMU_DIR",
                        help="a run's ground_imu directory: the IMU disarmed on the ground")
    source.add_argument("--flight", type=Path, action="append",
                        help="flight directory (bag/ and mission_phase_evidence.json); repeatable; "
                             "pools its hover holds, which include the vehicle's motion")
    imu.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "calibration":
        manifest = build_calibration(args.output, args.doppler_source)
        print(json.dumps(manifest["files_sha256"], indent=2))
        if args.compare:
            print(json.dumps(compare_calibration(args.output, args.compare), indent=2))
        return 0
    if args.output.exists():
        raise SystemExit(f"output already exists: {args.output}")
    if args.command == "mission-priors":
        document = build_mission_priors(args.evidence, args.position_sigma_m, args.rotation_sigma_rad, args.to_world)
    elif args.command == "map-prior":
        document = convert_map_prior(args.source, args.flight_plan)
    else:
        document = build_ground_imu_covariance(args.ground) if args.ground else build_imu_covariance(args.flight)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
