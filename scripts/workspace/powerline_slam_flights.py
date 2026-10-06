#!/usr/bin/env python3
"""Fly and record powerline SLAM corridor flights in the live III simulation.

The flights mirror the waypoint legs of the powerline_slam r21 dual-radar
stimulus (``powerline_slam_corridor_flights.json``) on the
``d4s_dc_drone_powerline_eval`` sensor layout: Radar-U on ``/sensor/mmwave``,
Radar-F on ``/sensor/mmwave_forward`` and the cable camera 20 deg from upward.
Live execution uses the canonical III runtime through ``DroneAgentTools``, as
``perception_dataset_flights.py`` does; ``--list`` and ``--dry-run`` work
without ROS.

Each flight directory holds the exact-topic bag, ``mission_phase_evidence.json``
(every leg's planned command and its source-time interval on the simulation
clock, from which the estimator's commanded-position priors are built), the
live flight plan and pose samples. A flight's bag stops only once the camera
truth, which is rendered behind the simulation, covers its last leg. Two
recorders take the flight, one for the camera-resolution images and one for
every other stream, and their bags are merged into the flight's bag.

Before the first takeoff the run records ``ground_imu/``: the IMU disarmed on
the ground, which measures sensor noise without vehicle motion (hover holds
measure the vehicle's motion as well).

Each flight of a run flies in its own process (``--append`` to the run
directory): in long single-process runs the III tools' node has stopped
receiving service responses during the second flight.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import functools
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import perception_dataset_flights as dataset  # noqa: E402

CATALOG_PATH = Path(__file__).resolve().with_name("powerline_slam_corridor_flights.json")
DEFAULT_OUTPUT_ROOT = WORKSPACE_ROOT / "datasets/powerline_slam"
SIM_MODEL = "gz_d4s_dc_drone_powerline_eval"
STAGING_FIXTURE = "mid_corridor_taken_off_conductors_visible"
PRE_ROLL_SEC = 3.0
POST_ROLL_SEC = 3.0
# Camera truth is rendered after each frame and can run seconds behind the
# simulation; a flight's bag stops once it covers the last leg, or after this
# wall-clock limit.
TRUTH_DRAIN_TIMEOUT_SEC = 60.0
# A fly command that finds the maneuver controller not ready is issued up to
# this many times. In long runs the III tools' node has stopped receiving any
# service response for minutes (lifecycle and configuration queries alike)
# while PX4, the controller and the supervision daemon were fine, so a retry
# first replaces that node.
LEG_COMMAND_ATTEMPTS = 3
LEG_COMMAND_RETRY_SEC = 5.0
GROUND_IMU_SEC = 60.0

# Exact recording contract. Runtime inputs of the powerline SLAM estimator
# first, then III runtime context, then evaluator-only simulator truth.
RUNTIME_TOPICS: tuple[str, ...] = (
    "/clock",
    "/sensor/cable_camera/image_raw",
    "/sensor/cable_camera/camera_info",
    "/sensor/mmwave/points_full",
    "/sensor/mmwave_forward/points_full",
    "/fmu/out/sensor_combined",
    "/fmu/out/vehicle_odometry",
    "/fmu/out/vehicle_local_position",
    "/fmu/out/vehicle_status_v1",
    "/fmu/out/timesync_status",
)
CONTEXT_TOPICS: tuple[str, ...] = (
    "/sensor/mmwave/points",
    "/sensor/mmwave_forward/points",
    "/perception/pl_mapper/powerline",
    "/tf",
    "/tf_static",
)
TRUTH_TOPICS: tuple[str, ...] = (
    "/simulation/ground_truth/drone/state",
    "/simulation/ground_truth/drone/odometry",
    "/simulation/ground_truth/conductors/geometry",
    "/simulation/ground_truth/conductor_id_map",
    "/simulation/ground_truth/mmwave/scan_v2",
    "/simulation/ground_truth/mmwave_forward/scan_v2",
    "/simulation/ground_truth/mmwave/conductor_labels",
    "/simulation/ground_truth/mmwave_forward/conductor_labels",
    "/simulation/ground_truth/cable_camera/conductor_instance_mask",
    "/simulation/ground_truth/cable_camera/frame",
    "/simulation/ground_truth/cable_camera/pylon_instance_mask",
    "/simulation/ground_truth/cable_camera/pylon_frame",
    "/simulation/ground_truth/cable_camera/pylon_exact_frame",
)
# Gazebo's IMU samples: their stamps pair PX4's uXRCE-DDS timestamps with the
# simulation clock at the source.
CLOCK_EVIDENCE_TOPICS: tuple[str, ...] = ("/simulation/gazebo/imu",)
RECORD_TOPICS: tuple[str, ...] = RUNTIME_TOPICS + CONTEXT_TOPICS + TRUTH_TOPICS + CLOCK_EVIDENCE_TOPICS
GROUND_IMU_TOPICS: tuple[str, ...] = (
    "/clock",
    "/fmu/out/sensor_combined",
    "/fmu/out/vehicle_status_v1",
    "/simulation/gazebo/imu",
    "/simulation/ground_truth/drone/state",
)
# Per-frame camera truth, published after the frame's masks.
CAMERA_TRUTH_DRAIN_TOPICS: dict[str, str] = {
    "/simulation/ground_truth/cable_camera/frame": "iii_drone_interfaces/msg/CameraFrameGroundTruth",
    "/simulation/ground_truth/cable_camera/pylon_frame": "iii_drone_interfaces/msg/PylonCameraFrameGroundTruth",
    "/simulation/ground_truth/cable_camera/pylon_exact_frame":
        "iii_drone_interfaces/msg/PylonExactMaskFrameGroundTruth",
}
# PX4_PARAM_UXRCE_DDS_SYNCT=0 disables uXRCE-DDS time synchronization, so this
# topic is recorded (to prove it) but must stay empty.
EMPTY_BY_CONTRACT: frozenset[str] = frozenset({"/fmu/out/timesync_status"})
# III's ROS graph is UDP-only (setup/ros_setup.bash) and the host caps socket
# receive buffers (net.core.rmem_max, 212 KB here). The camera-resolution
# images carry 99 % of a flight's bytes (21 MB/s, 0.6-0.9 MB each); a recorder
# that takes them overflows its socket during their bursts and loses samples of
# every other stream (100-300 per flight). They therefore have a recorder of
# their own, and the two bags are merged in receipt order.
IMAGE_TOPICS: tuple[str, ...] = (
    "/sensor/cable_camera/image_raw",
    "/simulation/ground_truth/cable_camera/conductor_instance_mask",
    "/simulation/ground_truth/cable_camera/pylon_instance_mask",
)
STREAM_TOPICS: tuple[str, ...] = tuple(topic for topic in RECORD_TOPICS if topic not in IMAGE_TOPICS)
RECORDER_LOSS = re.compile(r"Number of messages lost on the transport layer: (\d+)")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema") != "iii.powerline-slam-corridor-flights/v1":
        raise ValueError(f"unexpected corridor flight catalog schema in {path}")
    for direction, flight in catalog["flights"].items():
        names = [leg["name"] for leg in flight["legs"]]
        if len(names) != len(set(names)) or not names:
            raise ValueError(f"flight {direction} needs unique, non-empty leg names")
        for leg in flight["legs"]:
            if len(leg["target_world_m"]) != 3 or float(leg["duration_s"]) <= 0.0:
                raise ValueError(f"flight {direction} leg {leg['name']} is malformed")
    return catalog


def wrap_angle(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def gazebo_to_live_world(point: Sequence[float], mapping: dict[str, Any]) -> dict[str, float]:
    """Gazebo ENU -> III world (north-west-up), as map_geometry_data_to_live_ros."""
    offset = mapping["offset"]
    return {
        "x": float(point[1]) + float(offset["x"]),
        "y": -float(point[0]) + float(offset["y"]),
        "z": float(point[2]) + float(offset["z"]),
    }


def live_world_to_gazebo(point: dict[str, float], mapping: dict[str, Any]) -> list[float]:
    """III world (north-west-up) -> Gazebo ENU; inverse of gazebo_to_live_world."""
    offset = mapping["offset"]
    return [
        -(float(point["y"]) - float(offset["y"])),
        float(point["x"]) - float(offset["x"]),
        float(point["z"]) - float(offset["z"]),
    ]


def plan_flight(catalog: dict[str, Any], direction: str, mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Live III fly-to targets for every catalog leg, with their planned commands.

    The planned command is what is actually flown, in Gazebo ENU: a catalog
    target below the minimum live height is raised to it, so its height then
    differs from the catalog command, which is kept for traceability.
    """
    minimum_height = float(catalog["minimum_live_height_m"])
    plan = []
    for leg in catalog["flights"][direction]["legs"]:
        live = gazebo_to_live_world(leg["target_world_m"], mapping)
        live["z"] = max(minimum_height, live["z"])
        catalog_command = {
            "frame": "world",
            "target_world_m": list(leg["target_world_m"]),
            "yaw_rad": float(leg["yaw_rad"]),
            "duration_s": float(leg["duration_s"]),
        }
        plan.append({
            "name": leg["name"],
            "section": leg["section"],
            "role": leg.get("role"),
            "duration_s": float(leg["duration_s"]),
            "catalog_command": catalog_command,
            "planned_command": {
                **catalog_command,
                "target_world_m": [round(value, 6) for value in live_world_to_gazebo(live, mapping)],
            },
            "live_target": {
                "frame_id": "world",
                **live,
                # PX4 NED heading -> III north-west-up yaw.
                "yaw": wrap_angle(-float(leg["yaw_rad"])),
                "label": leg["name"],
                "hold_sec": 0.0,
            },
        })
    return plan


def mission_phase_evidence(
    direction: str,
    plan: Sequence[dict[str, Any]],
    begins_ns: Sequence[int],
    end_ns: int,
    catalog_sha256: str,
) -> dict[str, Any]:
    """Planned legs with disjoint, contiguous source-time ownership.

    A leg owns the simulation-clock interval from issuing its command to issuing
    the next leg's command; the last leg ends when recording of the flight ends.
    planned_command is the flown command in Gazebo ENU (world) and
    catalog_command the catalog leg it came from.
    """
    if len(begins_ns) != len(plan):
        raise ValueError("every planned leg needs a source-time begin")
    boundaries = [*begins_ns, end_ns]
    if any(right <= left for left, right in zip(boundaries, boundaries[1:])):
        raise ValueError("leg source-time intervals must be strictly increasing")
    phases = [
        {
            "name": leg["name"],
            "status": "COMPLETE",
            "planned_command": dict(leg["planned_command"]),
            "catalog_command": dict(leg["catalog_command"]),
            "source_time_interval_ns": {"begin": int(begin), "end": int(end)},
            "live_command": dict(leg["live_target"]),
        }
        for leg, begin, end in zip(plan, boundaries, boundaries[1:])
    ]
    return {
        "schema": "iii.powerline-slam-mission-phase-evidence/v1",
        "status": "VALID",
        "source_time_unit": "nanoseconds",
        "source_time_clock": "/clock (Gazebo world simulation time)",
        "direction": direction,
        "catalog_sha256": catalog_sha256,
        "planned_phases": phases,
    }


def bag_message_counts(metadata_path: Path) -> dict[str, int]:
    import yaml

    info = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))["rosbag2_bagfile_information"]
    return {item["topic_metadata"]["name"]: int(item["message_count"]) for item in info["topics_with_message_count"]}


def merge_bags(parts: Sequence[Path], output: Path) -> dict[str, int]:
    """One MCAP bag of the parts' messages in receipt order; the per-topic counts must add up."""
    import rosbag2_py
    import yaml

    if output.exists():
        raise RuntimeError(f"bag output already exists: {output}")
    config = output.parent / f"{output.name}_merge.yaml"
    config.write_text(yaml.safe_dump({"output_bags": [{"uri": str(output), "storage_id": "mcap", "all_topics": True}]}),
                      encoding="utf-8")
    try:
        rosbag2_py.bag_rewrite([rosbag2_py.StorageOptions(uri=str(part)) for part in parts], str(config))
    finally:
        config.unlink(missing_ok=True)
    expected: dict[str, int] = {}
    for part in parts:
        for topic, count in bag_message_counts(part / "metadata.yaml").items():
            expected[topic] = expected.get(topic, 0) + count
    merged = bag_message_counts(output / "metadata.yaml")
    if merged != expected:
        raise RuntimeError(f"merged bag {output} does not hold exactly its parts' messages")
    return merged


def reported_transport_losses(stderr_path: str | None) -> int | None:
    """A recorder's own count of messages lost on the transport layer (None without its log)."""
    if not stderr_path:
        return None
    try:
        text = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(int(count) for count in RECORDER_LOSS.findall(text))


def verify_bag(metadata_path: Path, expected_topics: Sequence[str] = RECORD_TOPICS) -> dict[str, Any]:
    """Exact topic set; every topic has messages except the empty-by-contract ones, which must be empty."""
    report = dataset.verify_exact_bag_topics(metadata_path, expected_topics)
    topics = report["metadata"]["topics"]
    empty_by_contract = EMPTY_BY_CONTRACT & set(expected_topics)
    disallowed = sorted(set(report["zero_message_topics"]) - empty_by_contract)
    nonempty = sorted(topic for topic in empty_by_contract if topics.get(topic, {}).get("message_count", 0) > 0)
    report["allowed_zero_message_topics"] = sorted(set(report["zero_message_topics"]) & empty_by_contract)
    report["disallowed_zero_message_topics"] = disallowed
    report["checks"]["continuous_topics_have_messages"] = not disallowed
    report["checks"]["timesync_topic_is_empty"] = not nonempty
    report["success"] = all(report["checks"].values())
    return report


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class SimulationClock:
    """Latest /clock value and latest header stamps of watched topics, on its own node and executor thread."""

    def __init__(self, watched_topics: dict[str, str] | None = None) -> None:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
        from rosgraph_msgs.msg import Clock
        from rosidl_runtime_py.utilities import get_message

        self._node = rclpy.create_node("powerline_slam_flight_clock")
        self._lock = threading.Lock()
        self._now_ns: int | None = None
        self._stamps: dict[str, int | None] = {topic: None for topic in watched_topics or {}}
        self._node.create_subscription(Clock, "/clock", self._on_clock, qos_profile_sensor_data)
        for topic, type_name in (watched_topics or {}).items():
            self._node.create_subscription(
                get_message(type_name), topic, functools.partial(self._on_stamped, topic),
                QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE),
            )
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)
        self._thread.start()

    def _on_clock(self, message: Any) -> None:
        with self._lock:
            self._now_ns = int(message.clock.sec) * 1_000_000_000 + int(message.clock.nanosec)

    def _on_stamped(self, topic: str, message: Any) -> None:
        stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
        with self._lock:
            previous = self._stamps[topic]
            self._stamps[topic] = stamp if previous is None else max(previous, stamp)

    def wait_for_stamps(self, minimum_ns: int, timeout_sec: float) -> dict[str, int | None]:
        """Wait until every watched topic carried a stamp at or after minimum_ns; return the latest stamps."""
        deadline = time.monotonic() + timeout_sec
        while True:
            with self._lock:
                latest = dict(self._stamps)
            if all(stamp is not None and stamp >= minimum_ns for stamp in latest.values()):
                return latest
            if time.monotonic() > deadline:
                return latest
            time.sleep(0.1)

    def now_ns(self, timeout_sec: float = 10.0) -> int:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            with self._lock:
                if self._now_ns is not None:
                    return self._now_ns
            time.sleep(0.02)
        raise RuntimeError("no /clock sample received")

    def sleep_until_ns(self, target_ns: int, timeout_sec: float = 600.0) -> None:
        deadline = time.monotonic() + timeout_sec
        while self.now_ns() < target_ns:
            if time.monotonic() > deadline:
                raise RuntimeError("simulation clock did not reach the requested time")
            time.sleep(0.02)

    def close(self) -> None:
        self._executor.shutdown()
        self._node.destroy_node()


class CorridorRunner(dataset.DatasetRunner):
    """Canonical III runner flying catalog legs and recording the SLAM topics."""

    def __init__(self, run_dir: Path, geometry_path: Path, catalog: dict[str, Any], *, headless: bool,
                 keep_running: bool) -> None:
        super().__init__(run_dir, geometry_path, keep_running=keep_running, headless=headless)
        self.catalog = catalog
        self.catalog_sha256 = sha256_file(CATALOG_PATH)
        self.clock: SimulationClock | None = None
        self.ground_imu: dict[str, Any] | None = None
        self._leg_attempts: dict[str, int] = {}
        self.tools_rebuilds: list[dict[str, str]] = []
        self._recordings: dict[str, str] = {}
        self.recorder_losses: dict[str, int | None] = {}

    def rebuild_tools(self, reason: str) -> None:
        """Replace the III tools' ROS node and clients; the old node is destroyed, rclpy keeps running."""
        old = self.tools
        from iii_drone_mcp.agent_tools import DroneAgentTools

        self.tools = DroneAgentTools(artifact_dir=self.run_dir / "runtime",
                                     px4_system_address=old._px4_system_address)
        try:
            old.node.destroy_node()
        except Exception:  # noqa: BLE001 - the old node is abandoned either way
            pass
        self.tools_rebuilds.append({"at": utc_now(), "reason": reason})

    def ensure_ready(self) -> None:
        status = self.tools.simulation("status")
        stdout = str((status.data or {}).get("stdout", ""))
        running = next(
            (line.split(":", 1)[1].strip() for line in stdout.splitlines()
             if line.startswith("px4_sim_model_running:")),
            "none",
        )
        if running != SIM_MODEL:
            command = "restart" if running != "none" else "start"
            self.require(
                self.tools.simulation(command, headless=self.headless, sim_model=SIM_MODEL,
                                      ready_timeout_sec=240),
                f"{command} simulation with {SIM_MODEL}",
            )
        self.clock = SimulationClock(CAMERA_TRUTH_DRAIN_TOPICS)
        # Before the base class arms and takes off.
        self.ground_imu = self.record_ground_imu(self.run_dir / "ground_imu")
        super().ensure_ready()
        profile = dataset.MotionProfile(**self.catalog["motion_profile"])
        self.apply_motion_profile(profile)

    def ensure_system_started(self) -> None:
        system = self.tools.system("status")
        stdout = str((system.data or {}).get("stdout", "")).lower()
        if not system.success or "booted: false" in stdout or "inactive" in stdout:
            self.require(self.tools.system("boot", timeout_sec=180), "boot canonical system")
            self.require(self.tools.system("start", timeout_sec=180), "start canonical system")

    def record_ground_imu(self, ground_dir: Path) -> dict[str, Any]:
        """GROUND_IMU_SEC of IMU and Gazebo IMU, disarmed on the ground; recorded, never fatal."""
        assert self.clock is not None
        existing = ground_dir / "verification.json"
        if existing.is_file():
            # Recorded by an earlier flight process of this run.
            return json.loads(existing.read_text(encoding="utf-8"))
        try:
            self.ensure_system_started()
            status = self.require(self.tools.px4("status", timeout_sec=20), "read PX4 status")
            if bool(status.get("in_air")) or bool(status.get("armed")):
                result: dict[str, Any] = {"status": "skipped", "reason": "the vehicle is armed or in the air"}
            else:
                bag_dir = ground_dir / "bag"
                recording_id = f"ground_imu_{int(time.time())}"
                self.recorder_losses = {}
                self.start_recording("ground", bag_dir, recording_id, GROUND_IMU_TOPICS)
                begin_ns = self.clock.now_ns()
                self.clock.sleep_until_ns(begin_ns + int(GROUND_IMU_SEC * 1e9))
                end_ns = self.clock.now_ns()
                self.stop_bag()
                bag = verify_bag(bag_dir / "metadata.yaml", GROUND_IMU_TOPICS)
                result = {
                    "status": "passed" if bag["success"] else "failed",
                    "recording_id": recording_id,
                    "source_time_span_ns": [begin_ns, end_ns],
                    "bag_verification": bag,
                    "recorder_transport_losses": dict(self.recorder_losses),
                }
        except Exception as exc:  # noqa: BLE001 - the flights do not depend on it
            self.safe_recover()
            result = {"status": "failed", "error": str(exc)}
        write_json(ground_dir / "verification.json", {"recorded_at": utc_now(), **result})
        return result

    def start_recording(self, part: str, bag_dir: Path, recording_id: str, topics: Sequence[str]) -> None:
        if bag_dir.exists():
            raise RuntimeError(f"bag output already exists: {bag_dir}")
        self.require(self.tools.rosbag_record(
            "start", recording_id=recording_id, output_dir=str(bag_dir), all_topics=False,
            topics=list(topics), include_hidden_topics=False, startup_grace_sec=1.5,
        ), f"start {part} rosbag")
        self._recordings[part] = recording_id
        self._recording_id = recording_id

    def start_bag(self, flight_dir: Path, recording_id: str) -> None:
        """The flight's streams and its camera-resolution images, each with a recorder of its own."""
        if (flight_dir / "bag").exists():
            raise RuntimeError(f"bag output already exists: {flight_dir / 'bag'}")
        self.recorder_losses = {}
        self.start_recording("streams", flight_dir / "bag_streams", f"{recording_id}_streams", STREAM_TOPICS)
        self.start_recording("images", flight_dir / "bag_images", f"{recording_id}_images", IMAGE_TOPICS)

    def stop_bag(self) -> None:
        """Stop every recording; each recorder's reported transport losses go to recorder_losses."""
        recordings, self._recordings = self._recordings, {}
        self._recording_id = None
        failures = []
        for part, recording_id in recordings.items():
            result = self.tools.rosbag_record("stop", recording_id=recording_id, timeout_sec=20)
            if not result.success:
                failures.append(f"{part}: {result.message}")
                continue
            self.recorder_losses[part] = reported_transport_losses((result.data or {}).get("stderr_path"))
        if failures:
            raise RuntimeError(f"stop rosbag: {failures}")

    def merge_flight_bag(self, flight_dir: Path) -> None:
        """Merge the two recordings into the flight's bag; the parts go once it holds all their messages."""
        parts = [flight_dir / "bag_streams", flight_dir / "bag_images"]
        merge_bags(parts, flight_dir / "bag")
        for part in parts:
            shutil.rmtree(part)

    def on_leg_command(self, kind: str, leg: dict[str, Any], begin_ns: int) -> None:
        """Hook around a leg's fly-to command: "intent" before it is sent, "accepted" once it was accepted."""

    def fly_leg(self, samples: list[dict[str, Any]], leg: dict[str, Any], index: int) -> int:
        """Issue the leg's fly-to command; own the leg for at least duration_s."""
        assert self.clock is not None
        for attempt in range(1, LEG_COMMAND_ATTEMPTS + 1):
            # The leg begins with the command that is accepted.
            begin_ns = self.clock.now_ns()
            self.on_leg_command("intent", leg, begin_ns)
            result = self.tools.start_operation(
                "fly_to_position", **{key: leg["live_target"][key] for key in ("frame_id", "x", "y", "z", "yaw")},
                cancel_existing=True, clear_queue=False, send_timeout_sec=20, maneuver_ready_timeout_sec=60,
            )
            not_ready = (result.data or {}).get("error") == "maneuver_controller_not_ready"
            if result.success or not not_ready or attempt == LEG_COMMAND_ATTEMPTS:
                break
            time.sleep(LEG_COMMAND_RETRY_SEC)
            self.rebuild_tools(f"fly {leg['name']}: maneuver controller not ready")
        self._leg_attempts[leg["name"]] = attempt
        goal_id = str(self.require(result, f"fly {leg['name']}")["goal_id"])
        self.on_leg_command("accepted", leg, begin_ns)
        deadline = time.monotonic() + 180.0
        while True:
            self.sample(samples, phase=leg["name"], target_index=index)
            state = str((self.tools.operation_goal_status(goal_id).data or {}).get("state", ""))
            if state in {"succeeded", "failed", "cancelled", "rejected"}:
                if state != "succeeded":
                    raise RuntimeError(f"leg {leg['name']} goal {goal_id} ended as {state}")
                break
            if time.monotonic() > deadline:
                self.tools.cancel_operation_goal(goal_id)
                raise RuntimeError(f"leg {leg['name']} timed out")
            time.sleep(0.2)
        hold_until_ns = begin_ns + int(leg["duration_s"] * 1e9)
        while self.clock.now_ns() < hold_until_ns:
            self.sample(samples, phase=leg["name"], target_index=index)
            time.sleep(0.25)
        return begin_ns

    def run_flight(self, direction: str, flight_dir: Path) -> dict[str, Any]:
        assert self.clock is not None
        started_at = utc_now()
        staging = self.resolve_waypoint(dataset.Waypoint(STAGING_FIXTURE, "staging", hold_sec=1.0))
        mapping = staging["fixture_resolution"]["live_mapping"]
        if not self._geometry_mapped_to_live_ros:
            mapped = dataset.map_geometry_data_to_live_ros(self.geometry.data, mapping)
            self.geometry = dataclasses.replace(self.geometry, data=mapped)
            self._geometry_mapped_to_live_ros = True
        plan = plan_flight(self.catalog, direction, mapping)
        write_json(flight_dir / "flight_plan.json", {"direction": direction, "live_mapping": mapping,
                                                     "staging": staging, "legs": plan})
        self.activate_custom_operation_with_recovery()
        self.reposition({**staging, "z": plan[0]["live_target"]["z"]})
        self.start_pl_mapper_with_retries("reset PL mapper after staging")
        samples: list[dict[str, Any]] = []
        begins: list[int] = []
        self._leg_attempts = {}
        recording_id = f"{direction}_{int(time.time())}"
        try:
            self.start_bag(flight_dir, recording_id)
            pre_roll_end = self.clock.now_ns() + int(PRE_ROLL_SEC * 1e9)
            self.clock.sleep_until_ns(pre_roll_end)
            for index, leg in enumerate(plan):
                begins.append(self.fly_leg(samples, leg, index))
            end_ns = self.clock.now_ns()
            self.clock.sleep_until_ns(end_ns + int(POST_ROLL_SEC * 1e9))
            truth_stamps = self.clock.wait_for_stamps(end_ns, TRUTH_DRAIN_TIMEOUT_SEC)
            self.stop_bag()
        except Exception:
            self.safe_recover()
            raise
        self.merge_flight_bag(flight_dir)
        evidence = mission_phase_evidence(direction, plan, begins, end_ns, self.catalog_sha256)
        write_json(flight_dir / "mission_phase_evidence.json", evidence)
        write_json(flight_dir / "trajectory.json", {"samples": samples})
        bag = verify_bag(flight_dir / "bag/metadata.yaml")
        truth_covered = all(stamp is not None and stamp >= end_ns for stamp in truth_stamps.values())
        result = {
            "status": "passed" if bag["success"] and truth_covered else "failed",
            "direction": direction,
            "sim_model": SIM_MODEL,
            "started_at": started_at,
            "completed_at": utc_now(),
            "recording_id": recording_id,
            "bag_verification": bag,
            "recorder_transport_losses": dict(self.recorder_losses),
            "leg_count": len(plan),
            "source_time_span_ns": [begins[0], end_ns],
            "camera_truth_drain": {"covers_last_leg": truth_covered, "required_stamp_ns": end_ns,
                                   "latest_stamps_ns": truth_stamps},
            "leg_command_attempts": dict(self._leg_attempts),
            "tools_rebuilds": list(self.tools_rebuilds),
        }
        write_json(flight_dir / "verification.json", result)
        return result

    def close(self) -> None:
        try:
            try:
                super().close()
            except RuntimeError as exc:
                if "timed out" not in str(exc):
                    raise
                self.rebuild_tools(f"close: {exc}")
                self.restore_motion_configuration()
        finally:
            if self.clock is not None:
                self.clock.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the flight catalog and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print each flight's live targets for a zero Gazebo->ROS offset and exit")
    parser.add_argument("--flights", nargs="+", default=["a_to_b", "b_to_a"])
    parser.add_argument("--run-id", default=dt.datetime.now().strftime("powerline_slam_%Y%m%d_%H%M%S"))
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--geometry", type=Path, default=dataset.DEFAULT_GEOMETRY_PATH)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--keep-running", action="store_true",
                        help="leave the simulation running (run_isolated_powerline_slam_flights.sh otherwise stops it)")
    parser.add_argument("--append", action="store_true",
                        help="add the flights to the existing run directory of --run-id")
    parser.add_argument("--single-process", action="store_true",
                        help="fly every flight in this process instead of one process per flight")
    return parser


def fly_in_child_processes(args: argparse.Namespace, run_dir: Path) -> int:
    """One process per flight, each appending to the run directory."""
    status = 0
    for direction in args.flights:
        command = [sys.executable, str(Path(__file__).resolve()), "--run-id", args.run_id,
                   "--output-root", str(args.output_root), "--geometry", str(args.geometry),
                   "--flights", direction, "--append", "--single-process"]
        if args.headless:
            command.append("--headless")
        if args.keep_running:
            command.append("--keep-running")
        status = max(status, subprocess.run(command, check=False).returncode)
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    failed = [name for name in args.flights if manifest["flights"].get(name, {}).get("status") != "passed"]
    manifest["completed_at"] = utc_now()
    manifest["status"] = "recorded" if not failed else "failed"
    write_json(run_dir / "run_manifest.json", manifest)
    return 1 if failed or status else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog = load_catalog()
    unknown = [name for name in args.flights if name not in catalog["flights"]]
    if unknown:
        raise SystemExit(f"unknown flights: {unknown}; known: {sorted(catalog['flights'])}")
    if args.list:
        for direction, flight in catalog["flights"].items():
            total = sum(leg["duration_s"] for leg in flight["legs"])
            print(f"{direction}: {len(flight['legs'])} legs, >= {total:.0f} s, pylons {flight['pylon_order']}")
        return 0
    if args.dry_run:
        zero = {"offset": {"x": 0.0, "y": 0.0, "z": 0.0}}
        for direction in args.flights:
            print(json.dumps({direction: plan_flight(catalog, direction, zero)}, indent=2))
        return 0

    run_dir = args.output_root / args.run_id
    if args.append:
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if manifest.get("catalog", {}).get("sha256") != sha256_file(CATALOG_PATH) or manifest.get("sim_model") != SIM_MODEL:
            raise SystemExit(f"run {run_dir} was recorded with another catalog or model")
        repeated = [name for name in args.flights if (run_dir / name).exists()]
        if repeated:
            raise SystemExit(f"flights already in {run_dir}: {repeated}")
    else:
        if run_dir.exists():
            raise SystemExit(f"run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        manifest = {
            "schema": "iii.powerline-slam-flight-run/v1",
            "run_id": args.run_id,
            "created_at": utc_now(),
            "sim_model": SIM_MODEL,
            "catalog": {"path": str(CATALOG_PATH), "sha256": sha256_file(CATALOG_PATH)},
            "record_topics": list(RECORD_TOPICS),
            "simulation_seed": os.environ.get("III_SIMULATION_SEED"),
            "flights": {},
        }
        write_json(run_dir / "run_manifest.json", manifest)
    if len(args.flights) > 1 and not args.single_process:
        return fly_in_child_processes(args, run_dir)
    runner = CorridorRunner(run_dir, args.geometry, catalog, headless=args.headless,
                            keep_running=args.keep_running)
    failures = []
    try:
        runner.ensure_ready()
        manifest["ground_imu"] = runner.ground_imu
        write_json(run_dir / "run_manifest.json", manifest)
        for direction in args.flights:
            flight_dir = run_dir / direction
            try:
                manifest["flights"][direction] = runner.run_flight(direction, flight_dir)
            except Exception as exc:  # noqa: BLE001 - recorded per flight, run continues
                manifest["flights"][direction] = {"status": "failed", "error": str(exc)}
                write_json(flight_dir / "failure.json", {"failed_at": utc_now(), "error": str(exc)})
                failures.append(f"{direction}: {exc}")
            write_json(run_dir / "run_manifest.json", manifest)
    finally:
        try:
            runner.close()
        except Exception as exc:  # noqa: BLE001 - recorded; the flights stand
            manifest.setdefault("close_errors", []).append(str(exc))
            print(f"WARNING: closing the runner failed: {exc}", file=sys.stderr)
        manifest.setdefault("tools_rebuilds", []).extend(runner.tools_rebuilds)
    manifest["completed_at"] = utc_now()
    manifest["status"] = "recorded" if not failures else "failed"
    write_json(run_dir / "run_manifest.json", manifest)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
