#!/usr/bin/env python3
"""Record deterministic simulation flights for offline powerline perception work.

The executable catalog and artifact contract are intentionally usable without ROS
(`--list`, `--dry-run`, and `--verify-run`). Live execution is layered on the same
specification and uses the canonical III runtime through ``DroneAgentTools``.
"""

from __future__ import annotations

import argparse
import copy
import csv
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = 3
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GEOMETRY_PATH = (
    WORKSPACE_ROOT / "tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json"
)
DEFAULT_OUTPUT_ROOT = WORKSPACE_ROOT / "datasets/perception_pipeline"

# This is the dataset interface, not a discovery result. Do not broaden it with
# ``-a``: bags must contain exactly these names and no implicit camera-info or
# control-input topics.
RECORD_TOPICS: tuple[str, ...] = (
    "/sensor/cable_camera/image_raw",
    "/sensor/mmwave/points",
    "/sensor/mmwave/points_full",
    "/simulation/ground_truth/drone/odometry",
    "/simulation/ground_truth/mmwave/conductor_labels",
    "/simulation/ground_truth/cable_camera/conductor_instance_mask",
    "/simulation/ground_truth/conductor_id_map",
    "/simulation/ground_truth/drone/state",
    "/simulation/ground_truth/conductors/geometry",
    "/simulation/ground_truth/mmwave/scan",
    "/simulation/ground_truth/cable_camera/frame",
    "/fmu/out/vehicle_attitude",
    "/fmu/out/vehicle_angular_velocity",
    "/fmu/out/vehicle_acceleration",
    "/fmu/out/vehicle_imu",
    "/fmu/out/sensor_combined",
    "/fmu/out/vehicle_odometry",
    "/fmu/out/vehicle_local_position",
    "/fmu/out/timesync_status",
    "/fmu/out/estimator_status_flags",
    "/fmu/out/estimator_status",
    "/fmu/out/estimator_event_flags",
    "/fmu/out/estimator_sensor_bias",
    "/fmu/out/vehicle_status_v1",
    "/fmu/out/vehicle_global_position",
    "/fmu/out/vehicle_gps_position",
    "/tf",
    "/tf_static",
    "/perception/hough_transformer/cable_yaw_angle",
    "/perception/hough_transformer/status",
    "/perception/pl_dir_computer/powerline_direction_pose",
    "/perception/pl_dir_computer/powerline_direction_quat",
    "/perception/pl_dir_computer/status",
    "/perception/pl_mapper/powerline",
    "/perception/pl_mapper/points_est",
    "/perception/pl_mapper/transformed_points",
    "/perception/pl_mapper/projected_points",
)

# Detection-driven output: a zero count is meaningful while the conductor is
# deliberately outside the image, but the topic must still appear in metadata.
CONDITIONALLY_EMPTY_TOPICS: frozenset[str] = frozenset({
    "/perception/hough_transformer/cable_yaw_angle",
})

SENSOR_GROUND_TRUTH: dict[str, Any] = {
    "cable_camera": {
        "topic": "/sensor/cable_camera/image_raw",
        "frame_orientation": "upward-looking in the simulated drone body",
        "horizontal_fov_rad": 1.396,
        "resolution_px": [640, 480],
        "update_rate_hz": 10.0,
        "noise": {"type": "gaussian", "stddev": 0.007},
        "source": "d4s_dc_drone simulation SDF",
    },
    "mmwave": {
        "legacy_topic": "/sensor/mmwave/points",
        "full_topic": "/sensor/mmwave/points_full",
        "full_topic_type": "sensor_msgs/msg/PointCloud2",
        "full_topic_fields": {
            "x": "metres in the mmwave sensor frame",
            "y": "metres in the mmwave sensor frame",
            "z": "metres in the mmwave sensor frame",
            "velocity": "radial velocity in metres/second",
            "snr": "signal-to-noise ratio in dB",
            "noise": "noise level in dB",
        },
        "frame_orientation": "upward-looking in the simulated drone body",
        "update_rate_hz": 30.0,
        "maximum_range_m": 18.0,
        "view_cone_slope": 0.7,
        "noise": {"standard_deviation_m": 0.02, "dropout_probability": 0.02},
        "source": "d4s_dc_drone simulation SDF/plugin configuration",
        "visibility_rule": (
            "Sensor +X is rotated upward. A conductor return is possible only when it is above the sensor and "
            "up_distance > 0.7 * lateral_distance, within 18 m; no conductor below the drone is visible."
        ),
    },
}


@dataclasses.dataclass(frozen=True)
class MotionProfile:
    name: str
    average_velocity_mps: float
    maximum_velocity_mps: float
    acceleration_mps2: float
    jerk_mps3: float
    average_yaw_rate_rps: float
    maximum_yaw_rate_rps: float


MOTION_PROFILES: dict[str, MotionProfile] = {
    "slow": MotionProfile("slow", 0.20, 0.35, 0.25, 0.50, 0.15, 0.25),
    "nominal": MotionProfile("nominal", 0.50, 1.00, 0.50, 1.00, 0.30, 0.50),
    "fast": MotionProfile("fast", 1.00, 1.50, 0.50, 1.00, 0.50, 0.75),
}


@dataclasses.dataclass(frozen=True)
class Waypoint:
    fixture: str
    label: str
    hold_sec: float = 2.0
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    yaw_offset_rad: float = 0.0
    expected_visibility: str = "any"


@dataclasses.dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    description: str
    motion_profile: str
    waypoints: tuple[Waypoint, ...]
    tags: tuple[str, ...]
    pre_roll_sec: float = 3.0
    post_roll_sec: float = 3.0
    expected_inside_corridor_majority: bool = True
    expected_observability_sequence: tuple[str, ...] = ()
    expected_motion: str = "translation"

    @property
    def folder_name(self) -> str:
        return f"{self.scenario_id}_{self.name}"


def wp(
    fixture: str,
    label: str,
    hold_sec: float = 2.0,
    *,
    dx: float = 0.0,
    dy: float = 0.0,
    dz: float = 0.0,
    yaw: float = 0.0,
    visibility: str = "any",
) -> Waypoint:
    return Waypoint(fixture, label, hold_sec, dx, dy, dz, yaw, visibility)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "B01", "static_four_line_baseline", "Stationary baseline below the full four-conductor stack.", "nominal",
        (wp("mid_corridor_taken_off_conductors_visible", "four-line hover", 18, visibility="full"),),
        ("static", "full-observability"), expected_motion="hover",
    ),
    Scenario(
        "B02", "static_rotated_view", "Stationary four-line hover with a 90 degree yaw change.", "nominal",
        (wp("mid_corridor_taken_off_conductors_visible", "baseline yaw", 8, visibility="full"),
         wp("mid_corridor_taken_off_conductors_visible", "cross-span yaw", 12, yaw=math.pi / 2, visibility="full")),
        ("static", "yaw", "full-observability"), expected_motion="yaw",
    ),
    Scenario(
        "B03", "static_partial_top_pair", "Stationary high-corridor view of only the top conductor pair.", "nominal",
        (wp("high_corridor_in_flight_top_two_conductors_visible", "top-pair hover", 18, visibility="partial"),),
        ("static", "partial-observability"), expected_motion="hover",
    ),
    Scenario(
        "B04", "static_lateral_edge", "Stationary view at the north lateral visibility/corridor edge.", "nominal",
        (wp("midspan_lateral_outside_north", "lateral edge hover", 18, visibility="partial"),),
        ("static", "partial-observability", "corridor-edge"), expected_inside_corridor_majority=False, expected_motion="hover",
    ),
    Scenario(
        "B05", "static_above_stack", "Safe outside climb followed by a stationary negative example above the conductor stack.", "nominal",
        (wp("staging_west_clear", "outside high staging", 3, dz=7.0, visibility="none"),
         wp("midspan_inside_powerline_corridor", "above-stack hover", 18, dz=7.0, visibility="none")),
        ("static", "negative", "above-stack", "safe-outside-climb"),
        expected_inside_corridor_majority=False,
    ),
    Scenario(
        "B06", "vertical_slow", "Slow climb and descent under the conductors.", "slow",
        (wp("midspan_under_lower_conductor", "low", 4, dz=-0.5, visibility="full"),
         wp("midspan_under_lower_conductor", "high", 5, dz=2.2, visibility="partial"),
         wp("midspan_under_lower_conductor", "return low", 4, dz=-0.5, visibility="full")),
        ("vertical", "slow", "inside-corridor"), expected_observability_sequence=("full", "partial", "full"),
    ),
    Scenario(
        "B07", "vertical_fast", "Fast vertical excursion and return under the conductors.", "fast",
        (wp("midspan_under_lower_conductor", "low", 3, dz=-0.5, visibility="full"),
         wp("midspan_under_lower_conductor", "high", 3, dz=2.2, visibility="partial"),
         wp("midspan_under_lower_conductor", "return low", 3, dz=-0.5, visibility="full")),
        ("vertical", "fast", "inside-corridor"), expected_observability_sequence=("full", "partial", "full"),
    ),
    Scenario(
        "B08", "along_span_slow", "Slow east-to-west traversal below the conductor span.", "slow",
        (wp("span_entry_east_under", "east entry", 4, visibility="full"),
         wp("span_exit_west_under", "west exit", 5, visibility="full")),
        ("horizontal", "along-span", "slow", "inside-corridor"),
    ),
    Scenario(
        "B09", "along_span_fast_reverse", "Fast west-to-east traversal below the conductor span.", "fast",
        (wp("span_exit_west_under", "west start", 3, visibility="full"),
         wp("span_entry_east_under", "east finish", 4, visibility="full")),
        ("horizontal", "along-span", "fast", "inside-corridor"),
    ),
    Scenario(
        "B10", "lateral_entry_slow", "Slow lateral transition from no/partial view into the full corridor view.", "slow",
        (wp("midspan_lateral_outside_north", "outside", 5, visibility="partial"),
         wp("mid_corridor_taken_off_conductors_visible", "inside", 8, visibility="full")),
        ("horizontal", "lateral", "entry", "partial-observability"), expected_inside_corridor_majority=False,
        expected_observability_sequence=("partial", "full"),
    ),
    Scenario(
        "B11", "lateral_exit_nominal", "Nominal lateral transition from full view to no/partial view.", "nominal",
        (wp("mid_corridor_taken_off_conductors_visible", "inside", 8, visibility="full"),
         wp("midspan_lateral_outside_north", "outside", 5, visibility="partial")),
        ("horizontal", "lateral", "exit", "partial-observability"), expected_inside_corridor_majority=False,
        expected_observability_sequence=("full", "partial"),
    ),
    Scenario(
        "B12", "lateral_crossing_fast", "Fast lateral crossing through the full field of view.", "fast",
        (wp("midspan_lateral_outside_north", "north outside", 3, visibility="partial"),
         wp("mid_corridor_taken_off_conductors_visible", "center", 3, visibility="full"),
         wp("midspan_lateral_outside_north", "south outside", 3, dx=5.0, dy=-8.0, visibility="none")),
        ("horizontal", "lateral", "fast", "entry-exit"), expected_inside_corridor_majority=False,
        expected_observability_sequence=("partial", "full", "none"),
    ),
    Scenario(
        "B13", "yaw_sweep_slow", "Slow in-place yaw sweep while all conductors remain observable.", "slow",
        tuple(wp("mid_corridor_taken_off_conductors_visible", f"yaw {angle:+.0f} deg", 4, yaw=math.radians(angle), visibility="full")
              for angle in (-90, -45, 0, 45, 90, 0)),
        ("yaw", "slow", "full-observability", "inside-corridor"), expected_motion="yaw",
    ),
    Scenario(
        "B14", "yaw_oscillation_fast", "Fast repeated yaw oscillations under the full conductor stack.", "fast",
        tuple(wp("mid_corridor_taken_off_conductors_visible", f"yaw {angle:+.0f} deg", 2, yaw=math.radians(angle), visibility="full")
              for angle in (-70, 70, -70, 70, 0)),
        ("yaw", "fast", "full-observability", "inside-corridor"), expected_motion="yaw",
    ),
    Scenario(
        "B15", "combined_diagonal_climb_yaw", "Combined along-span translation, climb, and yaw change.", "nominal",
        (wp("span_entry_east_under", "low east", 4, dz=-0.5, yaw=-0.5, visibility="full"),
         wp("midspan_under_lower_conductor", "high center", 5, dz=2.2, yaw=0.8, visibility="partial"),
         wp("span_exit_west_under", "low west", 4, dz=-0.5, yaw=0.0, visibility="full")),
        ("combined", "horizontal", "vertical", "yaw", "inside-corridor"),
        expected_observability_sequence=("full", "partial", "full"),
    ),
    Scenario(
        "B16", "full_partial_full", "One continuous bag with full, then partial, then full conductor observability.", "slow",
        (wp("mid_corridor_taken_off_conductors_visible", "early full view", 8, visibility="full"),
         wp("high_corridor_in_flight_top_two_conductors_visible", "middle partial top-pair view", 10, visibility="partial"),
         wp("mid_corridor_taken_off_conductors_visible", "late full view", 8, visibility="full")),
        ("observability-transition", "full-partial-full", "inside-corridor"),
        expected_observability_sequence=("full", "partial", "full"),
    ),
    Scenario(
        "B17", "partial_along_span", "High-corridor along-span scan retaining only partial observability.", "slow",
        (wp("high_corridor_in_flight_top_two_conductors_visible", "partial center", 5, visibility="partial"),
         wp("high_corridor_in_flight_top_two_conductors_visible", "partial west", 5, dx=1.4, dy=5.8, visibility="partial"),
         wp("high_corridor_in_flight_top_two_conductors_visible", "partial east", 5, dx=-1.4, dy=-5.8, visibility="partial")),
        ("horizontal", "partial-observability", "inside-corridor"),
    ),
    Scenario(
        "B18", "takeoff_landing_transition", "Ground pre-roll, centered takeoff, stable full view, descent, and landing.", "nominal",
        (wp("init_on_ground", "ground start", 3, dz=0.9, visibility="full"),
         wp("mid_corridor_taken_off_conductors_visible", "airborne full view", 12, visibility="full"),
         wp("init_on_ground", "landing approach", 3, dz=0.9, visibility="full")),
        ("takeoff", "landing", "vertical", "inside-corridor", "observability-transition"),
        expected_observability_sequence=(),
    ),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jsonable(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def map_geometry_data_to_live_ros(
    geometry_data: dict[str, Any],
    live_mapping: dict[str, Any],
) -> dict[str, Any]:
    """Rotate/translate Gazebo conductor truth into the current ROS world."""
    mapped = copy.deepcopy(geometry_data)
    offset = live_mapping["offset"]

    def point(value: dict[str, Any]) -> dict[str, float]:
        return {
            "x": float(value["y"]) + float(offset["x"]),
            "y": -float(value["x"]) + float(offset["y"]),
            "z": float(value["z"]) + float(offset["z"]),
        }

    def vector(value: dict[str, Any]) -> dict[str, float]:
        return {"x": float(value["y"]), "y": -float(value["x"]), "z": float(value.get("z", 0.0))}

    def bbox_from_points(values: Sequence[dict[str, Any]]) -> dict[str, dict[str, float]]:
        return {
            "min": {axis: min(float(value[axis]) for value in values) for axis in ("x", "y", "z")},
            "max": {axis: max(float(value[axis]) for value in values) for axis in ("x", "y", "z")},
        }

    powerlines = mapped["ground_truth"]["powerlines"]
    all_samples: list[dict[str, float]] = []
    for conductor in powerlines.get("conductors", []):
        conductor["coordinate_space"] = "live_ros_world"
        conductor["samples"] = [point(value) for value in conductor.get("samples", [])]
        conductor["start"] = point(conductor["start"])
        conductor["end"] = point(conductor["end"])
        conductor["bounding_box"] = bbox_from_points(conductor["samples"])
        all_samples.extend(conductor["samples"])
    aggregate = powerlines["aggregate"]
    aggregate["start_average"] = point(aggregate["start_average"])
    aggregate["end_average"] = point(aggregate["end_average"])
    aggregate["span_axis_unit_xy"] = vector(aggregate["span_axis_unit_xy"])
    aggregate["bounding_box"] = bbox_from_points(all_samples)
    powerlines["coordinate_space"] = "live_ros_world"

    pylons = mapped["ground_truth"].get("pylons", {})
    pylons["coordinate_space"] = "live_ros_world"
    for pylon in pylons.get("items", []):
        pylon["position"] = {**pylon["position"], **point(pylon["position"])}
        pylon["base_center"] = point(pylon["base_center"])
        corners = [
            {"x": x, "y": y, "z": z}
            for x in (pylon["bounding_box"]["min"]["x"], pylon["bounding_box"]["max"]["x"])
            for y in (pylon["bounding_box"]["min"]["y"], pylon["bounding_box"]["max"]["y"])
            for z in (pylon["bounding_box"]["min"]["z"], pylon["bounding_box"]["max"]["z"])
        ]
        pylon["bounding_box"] = bbox_from_points([point(value) for value in corners])
    mapped["frame_id"] = "world"
    mapped["live_ros_mapping"] = copy.deepcopy(live_mapping)
    return mapped


def validate_catalog() -> list[str]:
    errors: list[str] = []
    if len(RECORD_TOPICS) != 37:
        errors.append(f"expected 37 topics, found {len(RECORD_TOPICS)}")
    if len(set(RECORD_TOPICS)) != len(RECORD_TOPICS):
        errors.append("topic list contains duplicates")
    if len(SCENARIOS) != 18:
        errors.append(f"expected 18 scenarios, found {len(SCENARIOS)}")
    ids = [scenario.scenario_id for scenario in SCENARIOS]
    folders = [scenario.folder_name for scenario in SCENARIOS]
    if len(set(ids)) != len(ids) or ids != [f"B{index:02d}" for index in range(1, 19)]:
        errors.append("scenario IDs must be unique and sequential B01..B18")
    if len(set(folders)) != len(folders):
        errors.append("scenario output folder names must be unique")
    unknown_profiles = sorted({scenario.motion_profile for scenario in SCENARIOS} - set(MOTION_PROFILES))
    if unknown_profiles:
        errors.append(f"unknown motion profiles: {unknown_profiles}")
    if any(not scenario.waypoints for scenario in SCENARIOS):
        errors.append("every scenario must have at least one waypoint")
    b16 = next((scenario for scenario in SCENARIOS if scenario.scenario_id == "B16"), None)
    if b16 is None or b16.expected_observability_sequence != ("full", "partial", "full"):
        errors.append("B16 must require full -> partial -> full observability")
    if b16 and tuple(point.expected_visibility for point in b16.waypoints) != ("full", "partial", "full"):
        errors.append("B16 waypoints must encode full -> partial -> full observability")
    if sum(s.expected_inside_corridor_majority for s in SCENARIOS) <= len(SCENARIOS) // 2:
        errors.append("most scenarios must require majority-inside-corridor coverage")
    return errors


def select_scenarios(filters: Sequence[str]) -> tuple[Scenario, ...]:
    if not filters:
        return SCENARIOS
    wanted = {item.upper() for item in filters}
    selected = tuple(
        scenario for scenario in SCENARIOS
        if scenario.scenario_id.upper() in wanted or scenario.name.upper() in wanted or scenario.folder_name.upper() in wanted
    )
    missing = wanted - {
        alias.upper()
        for scenario in selected
        for alias in (scenario.scenario_id, scenario.name, scenario.folder_name)
    }
    if missing:
        raise ValueError(f"unknown scenario filter(s): {', '.join(sorted(missing))}")
    return selected


def ground_truth_document(
    geometry_path: Path,
    scenario: Scenario,
    *,
    live_geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry = live_geometry or source_geometry
    truth = geometry.get("ground_truth", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "scenario_id": scenario.scenario_id,
        "frame_conventions": {
            "world": "ROS ENU world frame",
            "vehicle": "drone base_link body frame",
            "yaw": "radians about world +Z",
            "positions": "metres",
        },
        "vehicle_ground_truth": {
            "topic": "/simulation/ground_truth/drone/state",
            "compatibility_topic": "/simulation/ground_truth/drone/odometry",
            "type": "iii_drone_interfaces/msg/SimulatorDroneState",
            "source": "Gazebo model base_link entity components; independent of PX4/navigation",
            "parent_frame": "world",
            "source_link": "base_link (reported in every message)",
            "pose": "position and quaternion orientation in the Gazebo world frame",
            "quaternion_order": "ROS geometry_msgs fields x, y, z, w",
            "twist": "linear and angular velocity expressed in the world frame",
            "publish_rate_hz": 100.0,
        },
        "measurement_provenance": {
            "immutable_ids": [
                str(item.get("id")) for item in truth.get("powerlines", {}).get("conductors", [])
            ],
            "id_map_topic": "/simulation/ground_truth/conductor_id_map",
            "static_geometry_topic": "/simulation/ground_truth/conductors/geometry",
            "mmwave_labels": {
                "topic": "/simulation/ground_truth/mmwave/conductor_labels",
                "type": "sensor_msgs/msg/PointCloud2",
                "field": "conductor_id (uint16)",
                "alignment": "same timestamp, width, and point order as /sensor/mmwave/points_full",
            },
            "mmwave_scan_truth": {
                "topic": "/simulation/ground_truth/mmwave/scan",
                "type": "iii_drone_interfaces/msg/RadarScanGroundTruth",
                "classes": {
                    "1": "VALID_PHYSICAL_CONDUCTOR",
                    "2": "PHANTOM",
                    "3": "CLUTTER_NO_PHYSICAL_SOURCE",
                },
                "ideal_generating_point": "exact selected simulator point before measurement noise",
                "nearest_physical_point": "nearest canonical centerline point, separately represented",
            },
            "camera_instance_mask": {
                "topic": "/simulation/ground_truth/cable_camera/conductor_instance_mask",
                "type": "sensor_msgs/msg/Image (mono16)",
                "alignment": "same timestamp and 640x480 geometry as each cable camera RGB frame",
                "background_label": 0,
                "frame_truth_topic": "/simulation/ground_truth/cable_camera/frame",
                "occlusion_semantics": (
                    "Z-buffered between physical conductors; does not test occlusion by non-conductor scene meshes."
                ),
            },
        },
        "geometry_source": {
            "path": str(geometry_path),
            "sha256": sha256_file(geometry_path),
            "world": geometry.get("world"),
            "name": geometry.get("name"),
            "source_coordinate_space": "gazebo_world",
            "mapped_to_live_ros_world": live_geometry is not None,
            "live_mapping": geometry.get("live_ros_mapping"),
        },
        "conductors": truth.get("powerlines", {}),
        "pylons": truth.get("pylons", {}),
        "ground_plane": truth.get("ground_plane", {}),
        "sensors": SENSOR_GROUND_TRUTH,
        "planned_waypoints": jsonable(scenario.waypoints),
        "truth_scope": (
            "Conductor and pylon geometry is mapped from immutable Gazebo truth into the live ROS world "
            "when the scenario executes. The bag's simulator odometry topic is authoritative vehicle 6-DOF "
            "ground truth; trajectory.json and plots remain operational samples from world->drone TF."
            if live_geometry is not None else
            "Preflight Gazebo geometry; live execution replaces this document with ROS-world mapped truth."
        ),
    }


def scenario_manifest_document(scenario: Scenario, repetition: int, geometry_path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "planned",
        "scenario": jsonable(scenario),
        "repetition": repetition,
        "motion_profile": jsonable(MOTION_PROFILES[scenario.motion_profile]),
        "record_topics": list(RECORD_TOPICS),
        "topic_count": len(RECORD_TOPICS),
        "ground_truth": "ground_truth.json",
        "trajectory": "trajectory.json",
        "verification": "verification.json",
        "bag_directory": "bag",
        "plots": {
            "topdown": "plots/trajectory_topdown.png",
            "side": "plots/trajectory_side.png",
            "observability": "plots/observability_timeline.png",
        },
        "geometry_sha256": sha256_file(geometry_path),
        "created_at": utc_now(),
    }


def initialize_run(
    output_root: Path,
    run_id: str,
    scenarios: Sequence[Scenario],
    repetitions: int,
    geometry_path: Path,
    *,
    dry_run: bool,
) -> tuple[Path, dict[str, Any]]:
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for repetition in range(1, repetitions + 1):
        for scenario in scenarios:
            suffix = "" if repetitions == 1 else f"_r{repetition:02d}"
            folder = f"{scenario.folder_name}{suffix}"
            scenario_dir = run_dir / folder
            scenario_dir.mkdir(parents=True, exist_ok=True)
            write_json(scenario_dir / "ground_truth.json", ground_truth_document(geometry_path, scenario))
            scenario_manifest = scenario_manifest_document(scenario, repetition, geometry_path)
            write_json(scenario_dir / "scenario_manifest.json", scenario_manifest)
            entries.append({
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "folder": folder,
                "repetition": repetition,
                "status": "planned",
                "expected_inside_corridor_majority": scenario.expected_inside_corridor_majority,
                "expected_observability_sequence": list(scenario.expected_observability_sequence),
            })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "planned" if dry_run else "initialized",
        "dry_run": dry_run,
        "created_at": utc_now(),
        "workspace_root": str(WORKSPACE_ROOT),
        "geometry_path": str(geometry_path),
        "geometry_sha256": sha256_file(geometry_path),
        "topic_contract": {"count": len(RECORD_TOPICS), "exact_topics": list(RECORD_TOPICS)},
        "motion_profiles": {name: jsonable(profile) for name, profile in MOTION_PROFILES.items()},
        "scenario_count": len(entries),
        "scenarios": entries,
        "artifact_schema": {
            "run": ["manifest.json", "manifest.csv", "contact_sheets/"],
            "scenario": [
                "scenario_manifest.json", "ground_truth.json", "trajectory.json", "verification.json",
                "bag/metadata.yaml", "plots/trajectory_topdown.png", "plots/trajectory_side.png",
                "plots/observability_timeline.png",
            ],
        },
    }
    write_run_manifest(run_dir, manifest)
    return run_dir, manifest


def write_run_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    write_json(run_dir / "manifest.json", manifest)
    rows = manifest.get("scenarios", [])
    fieldnames = (
        "scenario_id", "name", "folder", "repetition", "status",
        "expected_inside_corridor_majority", "expected_observability_sequence",
    )
    with (run_dir / "manifest.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["expected_observability_sequence"] = ">".join(row.get("expected_observability_sequence", []))
            writer.writerow(csv_row)


MOTION_PARAMETER_NAMES: dict[str, str] = {
    "average_velocity_mps": "/control/trajectory_interpolator/interpolation_avg_velocity_m_s",
    "maximum_velocity_mps": "/control/trajectory_interpolator/interpolation_max_velocity_m_s",
    "acceleration_mps2": "/control/trajectory_interpolator/interpolation_max_acceleration_m_s2",
    "jerk_mps3": "/control/trajectory_interpolator/interpolation_max_jerk_m_s3",
    "average_yaw_rate_rps": "/control/trajectory_interpolator/interpolation_avg_yaw_rate_rad_s",
    "maximum_yaw_rate_rps": "/control/trajectory_interpolator/interpolation_max_yaw_rate_rad_s",
}


def read_bag_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to inspect rosbag metadata") from exc
    if not metadata_path.is_file():
        raise ValueError(f"rosbag metadata is missing: {metadata_path}")
    raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    info = raw.get("rosbag2_bagfile_information", raw)
    topics: dict[str, dict[str, Any]] = {}
    for entry in info.get("topics_with_message_count", []):
        metadata = entry.get("topic_metadata", {})
        name = str(metadata.get("name", ""))
        if name:
            topics[name] = {
                "type": metadata.get("type"),
                "serialization_format": metadata.get("serialization_format"),
                "offered_qos_profiles": metadata.get("offered_qos_profiles"),
                "message_count": int(entry.get("message_count", 0)),
            }
    return {
        "path": str(metadata_path),
        "duration_ns": int((info.get("duration") or {}).get("nanoseconds", 0)),
        "message_count": int(info.get("message_count", 0)),
        "topics": topics,
    }


def verify_exact_bag_topics(metadata_path: Path, expected_topics: Sequence[str] = RECORD_TOPICS) -> dict[str, Any]:
    metadata = read_bag_metadata(metadata_path)
    expected = set(expected_topics)
    actual = set(metadata["topics"])
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    zero_messages = sorted(name for name in expected & actual if metadata["topics"][name]["message_count"] <= 0)
    disallowed_zero_messages = sorted(set(zero_messages) - CONDITIONALLY_EMPTY_TOPICS)
    checks = {
        "metadata_present": True,
        "exact_topic_set": not missing and not unexpected,
        "continuous_topics_have_messages": not disallowed_zero_messages,
        "bag_has_messages": metadata["message_count"] > 0,
        "duration_positive": metadata["duration_ns"] > 0,
    }
    return {
        "success": all(checks.values()),
        "checks": checks,
        "expected_topic_count": len(expected),
        "actual_topic_count": len(actual),
        "missing_topics": missing,
        "unexpected_topics": unexpected,
        "zero_message_topics": zero_messages,
        "allowed_zero_message_topics": sorted(set(zero_messages) & CONDITIONALLY_EMPTY_TOPICS),
        "disallowed_zero_message_topics": disallowed_zero_messages,
        "metadata": metadata,
    }


def ordered_states_present(states: Sequence[str], required: Sequence[str]) -> bool:
    """Return whether required states occur in order, allowing repeats/noise."""
    cursor = 0
    for state in states:
        if cursor < len(required) and state == required[cursor]:
            cursor += 1
    return cursor == len(required)


def visibility_category(visible_count: int) -> str:
    if visible_count <= 0:
        return "none"
    if visible_count < 4:
        return "partial"
    return "full"


def path_length(samples: Sequence[dict[str, Any]]) -> float:
    points = [sample for sample in samples if all(key in sample for key in ("x", "y", "z"))]
    return sum(
        math.dist(
            (float(first["x"]), float(first["y"]), float(first["z"])),
            (float(second["x"]), float(second["y"]), float(second["z"])),
        )
        for first, second in zip(points, points[1:])
    )


def upward_conductor_visibility(geometry: Any, pose: dict[str, float]) -> list[str]:
    """Approximate the simulator's upward +X cone in world coordinates.

    Flight roll/pitch are small in these scripted maneuvers, so world +Z is the
    sensor's upward axis. Dense canonical conductor samples make the point test a
    conservative and deterministic proxy for the plugin's segment projection.
    """
    visible: list[str] = []
    for conductor in geometry.conductors:
        points = conductor.get("samples") or [conductor.get("start"), conductor.get("end")]
        for point in (item for item in points if item):
            up = float(point["z"]) - float(pose["z"])
            lateral = math.hypot(float(point["x"]) - float(pose["x"]), float(point["y"]) - float(pose["y"]))
            distance = math.hypot(up, lateral)
            if up > 0.0 and up > 0.7 * lateral and distance <= 18.0:
                visible.append(str(conductor.get("id")))
                break
    return visible


def coherence_report(scenario: Scenario, samples: Sequence[dict[str, Any]], targets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    valid = [sample for sample in samples if all(key in sample for key in ("x", "y", "z", "yaw"))]
    finite = all(
        math.isfinite(float(sample[key]))
        for sample in valid
        for key in ("x", "y", "z", "yaw")
    )
    duration = float(valid[-1].get("elapsed_sec", 0.0)) if valid else 0.0
    travelled = path_length(valid)
    clearances = [float(sample["nearest_conductor_distance_m"]) for sample in valid if "nearest_conductor_distance_m" in sample]
    inside = [bool(sample.get("inside_powerline_corridor")) for sample in valid]
    inside_fraction = sum(inside) / len(inside) if inside else 0.0
    first = valid[0] if valid else None
    hover_drift = max(
        (
            math.dist(
                (float(first["x"]), float(first["y"]), float(first["z"])),
                (float(sample["x"]), float(sample["y"]), float(sample["z"])),
            )
            for sample in valid
        ),
        default=math.inf,
    ) if first else math.inf
    yaw_values = [float(sample["yaw"]) for sample in valid]
    yaw_excursion = max(yaw_values, default=0.0) - min(yaw_values, default=0.0)
    target_errors: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        target_samples = [sample for sample in valid if sample.get("target_index") == index and sample.get("phase") in {"hold", "post_roll"}]
        ground_phase = scenario.scenario_id == "B18" and index == 0
        error = min(
            (
                math.hypot(float(sample["x"]) - float(target["x"]), float(sample["y"]) - float(target["y"]))
                if ground_phase else
                math.dist(
                    (float(sample["x"]), float(sample["y"]), float(sample["z"])),
                    (float(target["x"]), float(target["y"]), float(target["z"])),
                )
                for sample in target_samples
            ),
            default=math.inf,
        )
        target_errors.append({
            "target_index": index,
            "label": target["label"],
            "minimum_position_error_m": error,
            "distance_dimensions": "xy" if ground_phase else "xyz",
        })
    waypoint_states = [str(target.get("expected_visibility", "any")) for target in targets]
    sampled_states = [
        visibility_category(int(sample["expected_visible_count"]))
        for sample in valid
        if sample.get("expected_visible_count") is not None
    ]
    sampled_state_transitions = [
        state for index, state in enumerate(sampled_states)
        if index == 0 or state != sampled_states[index - 1]
    ]
    required_sequence = list(scenario.expected_observability_sequence)
    checks = {
        "nonempty_samples": len(valid) >= 10,
        "finite_pose_coordinates": finite,
        "duration_sufficient": duration >= max(5.0, scenario.pre_roll_sec + scenario.post_roll_sec),
        # TF/geometry QA sampling shares CPU with Gazebo, four perception
        # nodes, and rosbag compression. One Hz is enough to establish path
        # coherence; sensor rates are checked independently in bag metadata.
        "sample_density_sufficient": len(valid) / max(duration, 1.0) >= 1.0,
        "conductor_clearance_at_least_0_5m": bool(clearances) and min(clearances) >= 0.5,
        "target_positions_reached": bool(target_errors) and all(item["minimum_position_error_m"] <= 0.75 for item in target_errors),
        "corridor_policy": (inside_fraction >= 0.60) if scenario.expected_inside_corridor_majority else True,
        "motion_intent": (
            hover_drift <= 0.75 if scenario.expected_motion == "hover"
            else yaw_excursion >= 0.45 if scenario.expected_motion == "yaw"
            else travelled >= 0.5
        ),
        "required_observability_order": (
            ordered_states_present(sampled_states, required_sequence) if required_sequence else True
        ),
    }
    return {
        "success": all(checks.values()),
        "checks": checks,
        "metrics": {
            "valid_sample_count": len(valid),
            "duration_sec": duration,
            "sample_rate_hz": len(valid) / max(duration, 1.0),
            "path_length_m": travelled,
            "hover_drift_m": hover_drift,
            "yaw_excursion_rad": yaw_excursion,
            "minimum_conductor_clearance_m": min(clearances, default=None),
            "inside_corridor_fraction": inside_fraction,
            "target_errors": target_errors,
            "waypoint_observability_states": waypoint_states,
            "sampled_observability_transitions": sampled_state_transitions,
            "required_observability_sequence": required_sequence,
        },
    }


def write_observability_plot(path: Path, scenario: Scenario, samples: Sequence[dict[str, Any]]) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid = [sample for sample in samples if "elapsed_sec" in sample]
    times = [float(sample["elapsed_sec"]) for sample in valid]
    expected_counts = [float(sample.get("expected_visible_count", math.nan)) for sample in valid]
    target_levels = {"none": 0.0, "partial": 2.0, "full": 4.0, "any": math.nan}
    target_counts = [
        target_levels.get(
            scenario.waypoints[int(sample.get("target_index", 0))].expected_visibility
            if scenario.waypoints and isinstance(sample.get("target_index", 0), int)
            and 0 <= int(sample.get("target_index", 0)) < len(scenario.waypoints)
            else "any",
            math.nan,
        )
        for sample in valid
    ]
    mapper_counts = [
        math.nan if sample.get("mapper_line_count") is None else float(sample["mapper_line_count"])
        for sample in valid
    ]
    inside = [1.0 if sample.get("inside_powerline_corridor") else 0.0 for sample in valid]
    clearance = [float(sample.get("nearest_conductor_distance_m", math.nan)) for sample in valid]
    fig, axes = plt.subplots(2, 1, figsize=(9, 6), dpi=140, sharex=True)
    axes[0].step(times, target_counts, where="post", label="fixture-calibrated target", color="tab:green", linewidth=2.0)
    axes[0].plot(times, expected_counts, label="upward-cone ground truth", color="tab:blue", linestyle=":", alpha=0.8)
    if any(math.isfinite(value) for value in mapper_counts):
        axes[0].plot(times, mapper_counts, label="PL mapper lines", color="tab:orange", alpha=0.8)
    axes[0].set_ylabel("conductor count")
    axes[0].set_ylim(-0.2, 5.2)
    axes[0].legend(loc="best", fontsize="small")
    axes[0].grid(True, linewidth=0.3)
    axes[1].step(times, inside, where="post", label="inside corridor", color="tab:green")
    axes[1].plot(times, clearance, label="nearest conductor [m]", color="tab:red")
    axes[1].axhline(0.5, color="black", linestyle="--", linewidth=0.8, label="0.5 m minimum")
    axes[1].set_xlabel("elapsed time [s]")
    axes[1].set_ylabel("state / distance")
    axes[1].legend(loc="best", fontsize="small")
    axes[1].grid(True, linewidth=0.3)
    fig.suptitle(f"{scenario.scenario_id} {scenario.name}: observability and corridor timeline")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path


def create_contact_sheet(run_dir: Path, plot_name: str, output_name: str) -> Path | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required for run contact sheets") from exc
    images: list[tuple[str, Path]] = []
    for scenario_dir in sorted(path for path in run_dir.iterdir() if path.is_dir() and path.name.startswith("B")):
        candidate = scenario_dir / "plots" / plot_name
        if candidate.is_file():
            images.append((scenario_dir.name, candidate))
    if not images:
        return None
    thumb_w, thumb_h, label_h = 560, 400, 32
    columns = 3
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (columns * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image_path) in enumerate(images):
        image = Image.open(image_path).convert("RGB")
        image.thumbnail((thumb_w, thumb_h))
        x = (index % columns) * thumb_w + (thumb_w - image.width) // 2
        y0 = (index // columns) * (thumb_h + label_h)
        sheet.paste(image, (x, y0))
        draw.text(((index % columns) * thumb_w + 8, y0 + thumb_h + 7), label, fill="black")
    output = run_dir / "contact_sheets" / output_name
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return output


def create_all_contact_sheets(run_dir: Path) -> dict[str, str]:
    specs = {
        "topdown": ("trajectory_topdown.png", "all_topdown.png"),
        "side": ("trajectory_side.png", "all_side.png"),
        "observability": ("observability_timeline.png", "all_observability.png"),
    }
    outputs: dict[str, str] = {}
    for key, (source, output) in specs.items():
        path = create_contact_sheet(run_dir, source, output)
        if path is not None:
            outputs[key] = str(path)
    return outputs


def verify_existing_run(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return {"success": False, "error": f"manifest missing: {manifest_path}"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_topics = tuple(manifest.get("topic_contract", {}).get("exact_topics") or RECORD_TOPICS)
    scenario_results: list[dict[str, Any]] = []
    for entry in manifest.get("scenarios", []):
        scenario_dir = run_dir / entry["folder"]
        verification_path = scenario_dir / "verification.json"
        checks = {
            "verification_present": verification_path.is_file(),
            "ground_truth_present": (scenario_dir / "ground_truth.json").is_file(),
            "trajectory_present": (scenario_dir / "trajectory.json").is_file(),
            "topdown_plot_present": (scenario_dir / "plots/trajectory_topdown.png").is_file(),
            "side_plot_present": (scenario_dir / "plots/trajectory_side.png").is_file(),
            "observability_plot_present": (scenario_dir / "plots/observability_timeline.png").is_file(),
        }
        verification: dict[str, Any] = {}
        if verification_path.is_file():
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            checks["scenario_passed"] = verification.get("status") == "passed"
        else:
            checks["scenario_passed"] = False
        try:
            bag = verify_exact_bag_topics(scenario_dir / "bag/metadata.yaml", expected_topics)
            checks["bag_exact_topics"] = bag["success"]
        except Exception as exc:
            bag = {"success": False, "error": str(exc)}
            checks["bag_exact_topics"] = False
        scenario_results.append({
            "scenario_id": entry.get("scenario_id"), "folder": entry["folder"],
            "success": all(checks.values()), "checks": checks, "bag": bag,
        })
    contact_sheets = create_all_contact_sheets(run_dir)
    checks = {
        "has_scenarios": bool(scenario_results),
        "all_scenarios_pass": bool(scenario_results) and all(item["success"] for item in scenario_results),
        "all_contact_sheets_present": set(contact_sheets) == {"topdown", "side", "observability"},
    }
    result = {
        "success": all(checks.values()), "checks": checks, "scenario_count": len(scenario_results),
        "scenarios": scenario_results, "contact_sheets": contact_sheets, "verified_at": utc_now(),
    }
    write_json(run_dir / "aggregate_verification.json", result)
    return result


def _nested_parameter_value(document: Any, parameter_name: str) -> Any:
    current = document
    for component in parameter_name.strip("/").split("/"):
        if not isinstance(current, dict) or component not in current:
            current = None
            break
        current = current[component]
    if isinstance(current, dict) and "value" in current:
        return current["value"]
    if current is not None and not isinstance(current, (dict, list)):
        return current
    if isinstance(document, dict):
        if parameter_name in document:
            return document[parameter_name]
        for value in document.values():
            found = _nested_parameter_value(value, parameter_name)
            if found is not None:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _nested_parameter_value(value, parameter_name)
            if found is not None:
                return found
    return None


class DatasetRunner:
    """Live canonical runner kept separate from import-safe catalog helpers."""

    def __init__(
        self,
        run_dir: Path,
        geometry_path: Path,
        *,
        keep_running: bool = False,
        headless: bool = False,
    ):
        try:
            import yaml
            from iii_drone_interfaces.msg import Powerline
            from iii_drone_mcp.agent_tools import DroneAgentTools
            from iii_drone_mcp.simulation_observation import (
                corridor_membership,
                load_geometry,
                nearest_conductor,
                visibility_state,
            )
        except ImportError as exc:
            raise RuntimeError(
                "live execution requires the ROS environment and PYTHONPATH=/home/iii/ws/tools/III-Drone-MCP"
            ) from exc
        self.yaml = yaml
        self.corridor_membership = corridor_membership
        self.nearest_conductor = nearest_conductor
        self.visibility_state = visibility_state
        self.Powerline = Powerline
        self.geometry = load_geometry(WORKSPACE_ROOT, geometry_path)
        self.run_dir = run_dir
        self.geometry_path = geometry_path
        self.keep_running = keep_running
        self.headless = headless
        self.tools = DroneAgentTools(
            artifact_dir=run_dir / "runtime",
            px4_system_address=os.environ.get(
                "III_DATASET_PX4_SYSTEM_ADDRESS",
                "udpin://0.0.0.0:14540",
            ),
        )
        self._recording_id: str | None = None
        self._original_motion: dict[str, Any] = {}
        self._geometry_mapped_to_live_ros = False

    @staticmethod
    def require(result: Any, context: str) -> dict[str, Any]:
        if not result.success:
            raise RuntimeError(f"{context}: {result.message}; data={result.data}")
        return dict(result.data or {})

    def ensure_ready(self) -> None:
        simulation = self.tools.simulation("status")
        simulation_stdout = str((simulation.data or {}).get("stdout", ""))
        simulation_flags = self.tools._simulation_status_flags(simulation_stdout)
        if not simulation.success or not simulation_flags["backend_processes_ready"]:
            self.require(
                self.tools.simulation("start", headless=self.headless, ready_timeout_sec=180),
                "start simulation",
            )
        system = self.tools.system("status")
        system_stdout = str((system.data or {}).get("stdout", "")).lower()
        if (
            not system.success
            or "booted: false" in system_stdout
            or "inactive" in system_stdout
        ):
            self.require(self.tools.system("boot", timeout_sec=180), "boot canonical system")
            self.require(self.tools.system("start", timeout_sec=180), "start canonical system")
        status = self.require(self.tools.px4("status", timeout_sec=20), "read PX4 status")
        if not bool(status.get("in_air")):
            if not bool(status.get("armed")):
                self.require(
                    self.tools.px4("arm", timeout_sec=75, postcondition_timeout_sec=30, health_stable_sec=2.0),
                    "PX4 arm",
                )
            self.require(
                self.tools.px4("takeoff", timeout_sec=90, postcondition_timeout_sec=60, min_altitude_m=1.5),
                "PX4 takeoff",
            )
        # An operator may hand control back while PX4 remains in ALTITUDE or
        # another manual flight mode. External-mode activation is deterministic
        # from HOLD, whereas an immediate ALTITUDE -> CustomOperation request can
        # be rejected even with healthy position/attitude estimates.
        self.px4_with_retries("hold", "stabilize PX4 in HOLD", timeout_sec=20)
        time.sleep(2.0)
        self.activate_custom_operation_with_recovery()
        self.start_pl_mapper_with_retries("start PL mapper")
        self.tools._take_cached_message("/perception/pl_mapper/powerline", self.Powerline, 0.0)
        current = self.require(self.tools.configuration("get_yaml", timeout_sec=10), "snapshot configuration")
        parsed = self.yaml.safe_load(str(current.get("yaml", ""))) or {}
        for parameter_name in MOTION_PARAMETER_NAMES.values():
            value = _nested_parameter_value(parsed, parameter_name)
            if value is None:
                raise RuntimeError(f"motion parameter missing from configuration snapshot: {parameter_name}")
            self._original_motion[parameter_name] = value
        write_json(self.run_dir / "runtime/original_motion_configuration.json", self._original_motion)

    def activate_custom_operation_with_recovery(self) -> None:
        """Activate the PX4 external mode, re-registering after a simulator restart.

        The III node can remain healthy and publish its old mode ID after PX4 SITL
        has been recreated. A warm node restart forces registration with the new
        PX4 process; retry only once so genuine flight-state failures remain loud.
        """
        isolated_mode_id = os.environ.get("III_DATASET_CUSTOM_OPERATION_MODE_ID")
        if isolated_mode_id is not None:
            target_system = int(os.environ.get("III_DATASET_PX4_TARGET_SYSTEM", "1"))
            self.require(
                self.tools.px4(
                    "set_nav_state",
                    nav_state=int(isolated_mode_id),
                    target_system=target_system,
                    target_component=1,
                    repeat_count=5,
                    postcondition_timeout_sec=30,
                    stable_sec=1.0,
                ),
                f"activate pre-registered CustomOperation mode {isolated_mode_id}",
            )
            return
        errors: list[str] = []
        for attempt in range(3):
            try:
                result = self.tools.activate_custom_operation(postcondition_timeout_sec=30)
                if result.success:
                    return
                errors.append(f"attempt {attempt + 1}: {result.message}; data={result.data}")
            except Exception as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
            if attempt == 0:
                restart = self.tools.system(
                    "restart", entity_id="custom_operation", include_dependencies=False,
                    cold=False, timeout_sec=60,
                )
                self.require(
                    restart,
                    "re-register Custom Operation after activation failure: " + errors[-1],
                )
                # The managed wrapper reports before DDS discovery and PX4 mode
                # registration have necessarily settled under recording load.
                time.sleep(5.0)
            elif attempt < 2:
                self.require(self.tools.system("start", timeout_sec=60), "recover canonical system")
                time.sleep(3.0)
        raise RuntimeError("failed to activate Custom Operation: " + " | ".join(errors))

    def start_pl_mapper_with_retries(self, context: str) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self.require(
                    self.tools.pl_mapper("start", reset=True, timeout_sec=20),
                    context,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2.0)
        assert last_error is not None
        raise last_error

    def px4_with_retries(self, command: str, context: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self.require(self.tools.px4(command, **kwargs), context)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2.0)
        assert last_error is not None
        raise last_error

    def apply_motion_profile(self, profile: MotionProfile) -> None:
        values = {parameter_name: getattr(profile, field_name) for field_name, parameter_name in MOTION_PARAMETER_NAMES.items()}
        self._set_motion_values(values)

    def _set_motion_values(self, values: dict[str, Any]) -> None:
        avg_velocity = MOTION_PARAMETER_NAMES["average_velocity_mps"]
        avg_yaw = MOTION_PARAMETER_NAMES["average_yaw_rate_rps"]
        # Lower averages first so both increasing and decreasing max-limit
        # transitions satisfy live configuration constraints.
        ordered = [
            (avg_velocity, min(0.1, float(values[avg_velocity]))),
            (avg_yaw, min(0.05, float(values[avg_yaw]))),
            (MOTION_PARAMETER_NAMES["maximum_velocity_mps"], values[MOTION_PARAMETER_NAMES["maximum_velocity_mps"]]),
            (MOTION_PARAMETER_NAMES["maximum_yaw_rate_rps"], values[MOTION_PARAMETER_NAMES["maximum_yaw_rate_rps"]]),
            (MOTION_PARAMETER_NAMES["acceleration_mps2"], values[MOTION_PARAMETER_NAMES["acceleration_mps2"]]),
            (MOTION_PARAMETER_NAMES["jerk_mps3"], values[MOTION_PARAMETER_NAMES["jerk_mps3"]]),
            (avg_velocity, values[avg_velocity]),
            (avg_yaw, values[avg_yaw]),
        ]
        for parameter_name, value in ordered:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    self.require(
                        self.tools.configuration("set", parameter_name=parameter_name, value=value, timeout_sec=20),
                        f"set {parameter_name}",
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(2.0)
            if last_error is not None:
                raise last_error
            # The service can reply before every managed client has applied the
            # value. Avoid a subsequent persistence check observing mixed state.
            time.sleep(1.0)

    def restore_motion_configuration(self) -> None:
        errors: list[str] = []
        try:
            if self._original_motion:
                self._set_motion_values(self._original_motion)
        except Exception as exc:
            errors.append(str(exc))
        write_json(
            self.run_dir / "runtime/restored_motion_configuration.json",
            {"success": not errors, "values": self._original_motion, "errors": errors, "restored_at": utc_now()},
        )
        if errors:
            raise RuntimeError("failed to restore motion configuration: " + "; ".join(errors))

    def resolve_waypoint(self, point: Waypoint) -> dict[str, Any]:
        last_error: Exception | None = None
        target: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                target = self.require(
                    self.tools.resolve_fixture_target(
                        position_id=point.fixture,
                        geometry_path=str(self.geometry_path),
                    ),
                    f"resolve fixture {point.fixture}",
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2.0)
        if target is None:
            assert last_error is not None
            raise last_error
        return {
            "frame_id": str(target.get("frame_id", "world")),
            "x": float(target["x"]) + point.dx,
            "y": float(target["y"]) + point.dy,
            # Custom Operation requires ground estimate + 0.5 m. The inferred
            # ROS/Gazebo vertical offset can drift by several decimetres during
            # a long suite, so 1.2 m is the robust low-flight floor while still
            # remaining safely below and fully viewing the conductor stack.
            "z": max(1.2, float(target["z"]) + point.dz),
            "yaw": float(target["yaw"]) + point.yaw_offset_rad,
            "fixture": point.fixture,
            "label": point.label,
            "hold_sec": point.hold_sec,
            "expected_visibility": point.expected_visibility,
            "fixture_resolution": target,
        }

    def sample(self, samples: list[dict[str, Any]], *, phase: str, target_index: int | None) -> None:
        pose = self.tools._lookup_world_drone_pose(timeout_sec=2.0)
        upward_visible_ids = upward_conductor_visibility(self.geometry, pose)
        corridor = self.corridor_membership(self.geometry, pose)
        nearest = self.nearest_conductor(self.geometry, pose)
        powerline, powerline_metadata = self.tools._take_cached_message(
            "/perception/pl_mapper/powerline", self.Powerline, 0.0, stale_after_sec=1.0
        )
        samples.append({
            "t": time.time(),
            "elapsed_sec": 0.0 if not samples else time.time() - samples[0]["t"],
            "phase": phase,
            "target_index": target_index,
            **pose,
            "expected_visible_conductor_ids": upward_visible_ids,
            "expected_visible_count": len(upward_visible_ids),
            "inside_powerline_corridor": corridor["inside_powerline_corridor"],
            "inside_span": corridor["inside_span"],
            "inside_lateral": corridor["inside_lateral"],
            "span_coordinate_m": corridor["span_coordinate_m"],
            "lateral_coordinate_m": corridor["lateral_coordinate_m"],
            "nearest_conductor_id": nearest["id"],
            "nearest_conductor_distance_m": nearest["distance_m"],
            "mapper_line_count": None if powerline is None else len(powerline.lines),
            "mapper_sample_age_sec": powerline_metadata.get("age_sec") if isinstance(powerline_metadata, dict) else None,
        })

    def sample_for(self, samples: list[dict[str, Any]], duration_sec: float, *, phase: str, target_index: int | None) -> None:
        deadline = time.monotonic() + duration_sec
        while time.monotonic() < deadline:
            try:
                self.sample(samples, phase=phase, target_index=target_index)
            except Exception as exc:
                samples.append({"t": time.time(), "phase": phase, "target_index": target_index, "sample_error": str(exc)})
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))

    def fly_and_sample(
        self, samples: list[dict[str, Any]], target: dict[str, Any], *, target_index: int, reposition: bool = False
    ) -> None:
        operation_name = "cable_aware_fly_to_position" if reposition else "fly_to_position"
        result = self.tools.start_operation(
            operation_name,
            frame_id=target["frame_id"], x=target["x"], y=target["y"], z=target["z"], yaw=target["yaw"],
            cancel_existing=True, clear_queue=reposition, send_timeout_sec=20,
            # Gazebo image/point-cloud serialization can briefly starve the
            # lifecycle get-state reply while recording. The clear-queue
            # service is already present in that condition, so give the state
            # probe enough time to recover instead of rejecting a valid bag.
            maneuver_ready_timeout_sec=60,
        )
        data = self.require(result, f"fly to {target['label']}")
        goal_id = str(data["goal_id"])
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            self.sample(samples, phase="reposition" if reposition else "maneuver", target_index=target_index)
            status = self.tools.operation_goal_status(goal_id)
            state = str((status.data or {}).get("state", status.message))
            if state in {"succeeded", "failed", "cancelled", "rejected"}:
                if state != "succeeded":
                    raise RuntimeError(f"flight goal {goal_id} ended as {state}: {status.data}")
                break
            time.sleep(0.20)
        else:
            self.tools.cancel_operation_goal(goal_id)
            raise RuntimeError(f"flight goal timed out: {target['label']}")
        self.sample_for(samples, target["hold_sec"], phase="hold", target_index=target_index)

    def reposition(self, target: dict[str, Any]) -> None:
        throwaway: list[dict[str, Any]] = []
        reposition_target = {**target, "hold_sec": 1.0}
        try:
            self.fly_and_sample(throwaway, reposition_target, target_index=0, reposition=True)
        except RuntimeError:
            # The cable-aware action may reject a goal that is already in the
            # low, known-clear under-conductor corridor (notably after a prior
            # recovery HOLD). A direct low-altitude reposition is safe here;
            # never use this fallback for high/above-stack staging targets.
            if float(target["z"]) > 2.0:
                raise
            self.fly_and_sample(throwaway, reposition_target, target_index=0, reposition=False)

    def px4_while_sampling(
        self, command: str, samples: list[dict[str, Any]], *, phase: str, target_index: int, timeout_sec: float = 90.0
    ) -> None:
        kwargs: dict[str, Any] = {"timeout_sec": timeout_sec, "postcondition_timeout_sec": timeout_sec}
        if command == "arm":
            kwargs["health_stable_sec"] = 1.0
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.tools.px4, command, **kwargs)
            while not future.done():
                self.sample_for(samples, 0.25, phase=phase, target_index=target_index)
            self.require(future.result(), f"PX4 {command}")

    def start_bag(self, scenario_dir: Path, recording_id: str) -> None:
        bag_dir = scenario_dir / "bag"
        if bag_dir.exists():
            raise RuntimeError(f"bag output already exists: {bag_dir}")
        result = self.tools.rosbag_record(
            "start", recording_id=recording_id, output_dir=str(bag_dir), all_topics=False,
            topics=list(RECORD_TOPICS), include_hidden_topics=False, startup_grace_sec=1.5,
        )
        self.require(result, "start exact-topic rosbag")
        self._recording_id = recording_id

    def stop_bag(self) -> None:
        if self._recording_id is None:
            return
        recording_id = self._recording_id
        self._recording_id = None
        self.require(self.tools.rosbag_record("stop", recording_id=recording_id, timeout_sec=20), "stop rosbag")

    def safe_recover(self) -> None:
        try:
            if self._recording_id is not None:
                self.stop_bag()
        except Exception:
            pass
        try:
            self.tools.cancel_all_operation_goals(reason="dataset scenario failure")
        except Exception:
            pass
        try:
            self.tools.px4("hold", timeout_sec=15)
        except Exception:
            pass

    def run_scenario(self, scenario: Scenario, scenario_dir: Path, repetition: int) -> dict[str, Any]:
        started_at = utc_now()
        targets = [self.resolve_waypoint(point) for point in scenario.waypoints]
        if not self._geometry_mapped_to_live_ros:
            mapping = targets[0]["fixture_resolution"]["live_mapping"]
            mapped_data = map_geometry_data_to_live_ros(self.geometry.data, mapping)
            self.geometry = dataclasses.replace(self.geometry, data=mapped_data)
            self._geometry_mapped_to_live_ros = True
        write_json(
            scenario_dir / "ground_truth.json",
            ground_truth_document(self.geometry_path, scenario, live_geometry=self.geometry.data),
        )
        write_json(scenario_dir / "resolved_waypoints.json", targets)
        self.apply_motion_profile(MOTION_PROFILES[scenario.motion_profile])
        self.activate_custom_operation_with_recovery()
        self.reposition(targets[0])
        if scenario.scenario_id == "B18":
            self.require(self.tools.px4("land", timeout_sec=90, postcondition_timeout_sec=90), "preflight landing")
        # Reset after transit: upward sensors must not seed a bag with conductors
        # seen at an earlier/lower repositioning pose.
        self.start_pl_mapper_with_retries("reset PL mapper after reposition")
        samples: list[dict[str, Any]] = []
        recording_id = f"{scenario.scenario_id.lower()}_r{repetition:02d}_{int(time.time())}"
        try:
            self.start_bag(scenario_dir, recording_id)
            self.sample_for(samples, scenario.pre_roll_sec, phase="pre_roll", target_index=0)
            self.sample_for(samples, targets[0]["hold_sec"], phase="hold", target_index=0)
            if scenario.scenario_id == "B18":
                self.px4_while_sampling("arm", samples, phase="arming", target_index=0, timeout_sec=60)
                self.px4_while_sampling("takeoff", samples, phase="takeoff", target_index=0, timeout_sec=90)
                # PX4 reports in_air before the maneuver controller has always
                # consumed the corresponding flight-capable state transition.
                # Stabilize in HOLD and allow that ROS state to settle before
                # submitting the first post-takeoff custom-operation goal.
                self.px4_with_retries("hold", "stabilize after recorded takeoff", timeout_sec=20)
                self.activate_custom_operation_with_recovery()
                self.sample_for(samples, 3.0, phase="takeoff_settle", target_index=0)
            for index, target in enumerate(targets[1:], start=1):
                self.fly_and_sample(samples, target, target_index=index)
            if scenario.scenario_id == "B18":
                self.px4_while_sampling("land", samples, phase="landing", target_index=len(targets) - 1, timeout_sec=90)
            self.sample_for(samples, scenario.post_roll_sec, phase="post_roll", target_index=len(targets) - 1)
            self.stop_bag()
        except Exception:
            self.safe_recover()
            raise
        write_json(scenario_dir / "trajectory.json", {"schema_version": SCHEMA_VERSION, "samples": samples})
        bag_verification = verify_exact_bag_topics(scenario_dir / "bag/metadata.yaml")
        plot_dir = scenario_dir / "plots"
        plot_dir.mkdir(parents=True, exist_ok=True)
        previous_artifact_dir = self.tools.artifact_dir
        self.tools.artifact_dir = plot_dir
        try:
            plots = self.tools._write_simulation_plots(self.geometry, samples, prefix="trajectory")
        finally:
            self.tools.artifact_dir = previous_artifact_dir
        plots["observability"] = str(
            write_observability_plot(plot_dir / "observability_timeline.png", scenario, samples)
        )
        plot_checks = {
            key: Path(path).is_file() and Path(path).stat().st_size > 10_000
            for key, path in plots.items()
        }
        coherence = coherence_report(scenario, samples, targets)
        result = {
            "status": "passed" if bag_verification["success"] and coherence["success"] and all(plot_checks.values()) else "failed",
            "started_at": started_at,
            "completed_at": utc_now(),
            "recording_id": recording_id,
            "resolved_waypoints": targets,
            "sample_count": len(samples),
            "bag_verification": bag_verification,
            "coherence": coherence,
            "plots": plots,
            "plot_checks": plot_checks,
        }
        write_json(scenario_dir / "verification.json", result)
        manifest = json.loads((scenario_dir / "scenario_manifest.json").read_text(encoding="utf-8"))
        manifest.update({"status": result["status"], "started_at": started_at, "completed_at": result["completed_at"]})
        write_json(scenario_dir / "scenario_manifest.json", manifest)
        if result["status"] != "passed":
            raise RuntimeError(f"scenario verification failed: {result}")
        return result

    def close(self) -> None:
        self.safe_recover()
        try:
            self.restore_motion_configuration()
        finally:
            try:
                self.tools.node.destroy_node()
            except Exception:
                pass


def execute_run(
    run_dir: Path,
    manifest: dict[str, Any],
    scenarios: Sequence[Scenario],
    repetitions: int,
    geometry_path: Path,
    *,
    resume: bool,
    keep_running: bool,
    headless: bool,
) -> int:
    runner = DatasetRunner(run_dir, geometry_path, keep_running=keep_running, headless=headless)
    failures: list[str] = []
    try:
        runner.ensure_ready()
        for repetition in range(1, repetitions + 1):
            for scenario in scenarios:
                entry = next(
                    (
                        item for item in manifest["scenarios"]
                        if item["scenario_id"] == scenario.scenario_id and int(item["repetition"]) == repetition
                    ),
                    None,
                )
                if entry is None:
                    raise RuntimeError(f"run manifest has no entry for {scenario.scenario_id} repetition {repetition}")
                scenario_dir = run_dir / entry["folder"]
                verification_path = scenario_dir / "verification.json"
                if resume and verification_path.is_file():
                    previous = json.loads(verification_path.read_text(encoding="utf-8"))
                    if previous.get("status") == "passed":
                        entry["status"] = "passed"
                        entry["resumed"] = True
                        write_run_manifest(run_dir, manifest)
                        continue
                if resume and scenario_dir.exists() and (
                    (scenario_dir / "bag").exists() or (scenario_dir / "failure.json").exists()
                ):
                    archive = run_dir / "rejected_attempts" / f"{entry['folder']}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(scenario_dir), str(archive))
                    scenario_dir.mkdir(parents=True, exist_ok=True)
                    write_json(scenario_dir / "ground_truth.json", ground_truth_document(geometry_path, scenario))
                    write_json(
                        scenario_dir / "scenario_manifest.json",
                        scenario_manifest_document(scenario, repetition, geometry_path),
                    )
                    entry["rejected_attempt_archive"] = str(archive.relative_to(run_dir))
                    entry.pop("error", None)
                    entry["status"] = "initialized"
                    write_run_manifest(run_dir, manifest)
                try:
                    result = runner.run_scenario(scenario, scenario_dir, int(entry["repetition"]))
                    entry["status"] = result["status"]
                    entry.pop("error", None)
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["error"] = str(exc)
                    failures.append(f"{entry['folder']}: {exc}")
                    write_json(scenario_dir / "failure.json", {"failed_at": utc_now(), "error": str(exc)})
                    runner.safe_recover()
                write_run_manifest(run_dir, manifest)
        manifest["status"] = "recorded" if all(entry.get("status") == "passed" for entry in manifest["scenarios"]) else "failed"
        manifest["failures"] = [
            f"{entry['folder']}: {entry.get('error', 'not completed')}"
            for entry in manifest["scenarios"] if entry.get("status") != "passed"
        ]
        manifest["completed_at"] = utc_now()
        manifest["contact_sheets"] = create_all_contact_sheets(run_dir)
        write_run_manifest(run_dir, manifest)
    finally:
        runner.close()
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


def default_run_id() -> str:
    return "perception_dataset_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def print_catalog(scenarios: Iterable[Scenario]) -> None:
    print(f"Exact recording contract: {len(RECORD_TOPICS)} topics")
    for topic in RECORD_TOPICS:
        print(f"  {topic}")
    print(f"\nFlight catalog: {len(tuple(scenarios))} scenarios")
    for scenario in scenarios:
        transition = " -> ".join(scenario.expected_observability_sequence) or "n/a"
        print(
            f"  {scenario.scenario_id} {scenario.name:<32} "
            f"profile={scenario.motion_profile:<7} waypoints={len(scenario.waypoints):>2} visibility={transition}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print exact topics and the flight catalog, then exit")
    parser.add_argument("--dry-run", action="store_true", help="validate and materialize manifests without starting ROS")
    parser.add_argument("--scenario", action="append", default=[], help="scenario ID/name to run; repeat for multiple")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY_PATH)
    parser.add_argument("--resume", action="store_true", help="skip scenario folders already verified as passing")
    parser.add_argument("--keep-running", action="store_true", help="leave simulation and canonical system running")
    parser.add_argument("--headless", action="store_true", help="run the Gazebo/PX4 backend without Gazebo GUI or QGroundControl")
    parser.add_argument("--verify-run", type=Path, help="validate an existing run without flying")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = validate_catalog()
    if errors:
        for error in errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    try:
        scenarios = select_scenarios(args.scenario)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.repetitions < 1:
        print("--repetitions must be positive", file=sys.stderr)
        return 2
    if args.list:
        print_catalog(scenarios)
        return 0
    if args.verify_run:
        result = verify_existing_run(args.verify_run.resolve())
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1
    if not args.geometry.is_file():
        print(f"geometry file not found: {args.geometry}", file=sys.stderr)
        return 2
    run_id = args.run_id or default_run_id()
    run_dir = args.output_root.resolve() / run_id
    if args.resume and (run_dir / "manifest.json").is_file():
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        print(f"Resuming {len(scenarios)} selected scenario(s) in {run_dir}")
    else:
        run_dir, manifest = initialize_run(
            args.output_root.resolve(), run_id, scenarios, args.repetitions, args.geometry.resolve(), dry_run=args.dry_run
        )
        print(f"Initialized {manifest['scenario_count']} scenario artifact folders in {run_dir}")
    if args.dry_run:
        print("Dry run complete; no ROS commands were issued.")
        return 0
    return execute_run(
        run_dir, manifest, scenarios, args.repetitions, args.geometry.resolve(),
        resume=args.resume, keep_running=args.keep_running, headless=args.headless,
    )


if __name__ == "__main__":
    raise SystemExit(main())
