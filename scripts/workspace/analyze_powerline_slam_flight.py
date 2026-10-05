#!/usr/bin/env python3
"""Inspect a powerline SLAM corridor flight recorded by powerline_slam_flights.py.

Reports, from the bag alone:

- topic inventory and per-topic message rates against the recording contract;
- the real-time factor of the simulation (/clock against the recording clock);
- the time contract between PX4 uXRCE-DDS timestamps and the simulation clock
  (PX4_PARAM_UXRCE_DDS_SYNCT=0): the offset timestamp - /clock at receipt;
- sensor stamps: radar and camera header stamps against /clock, the Radar-U to
  Radar-F trigger offset, and camera_info/image stamp agreement;
- the simulator-v2 radar point counts per scan;
- the heading of PX4's estimate (vehicle_odometry) against the Gazebo ground
  truth, both in NED, matched in source time;
- the clock pairing of PX4's sensor_combined timestamps with the stamps of
  Gazebo's own IMU samples (/simulation/gazebo/imu), when recorded;
- missing samples on the periodic high-rate streams (gaps in their stamps);
- with --mission-evidence, whether every image during the legs has its
  camera_info and camera truth (the recorder subscribes to them just after the
  image stream, so the first frames of the pre-roll may lack them).

Run inside the devcontainer with the workspace sourced. Exits nonzero when the
bag misses a contract topic or a stamp invariant fails.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
import rosbag2_py

sys.path.insert(0, str(Path(__file__).resolve().parent))
from powerline_slam_flights import EMPTY_BY_CONTRACT, RECORD_TOPICS  # noqa: E402

PX4_TOPICS = (
    "/fmu/out/sensor_combined",
    "/fmu/out/vehicle_odometry",
    "/fmu/out/vehicle_local_position",
)
RADAR_TOPICS = ("/sensor/mmwave/points_full", "/sensor/mmwave_forward/points_full")
CAMERA_TOPICS = ("/sensor/cable_camera/image_raw", "/sensor/cable_camera/camera_info")
TRUTH_ODOMETRY = "/simulation/ground_truth/drone/odometry"
HEADING_MATCH_NS = 20_000_000
GAZEBO_IMU = "/simulation/gazebo/imu"
CAMERA_TRUTH = "/simulation/ground_truth/cable_camera/frame"
# Share of PX4 IMU timestamps that must equal a Gazebo IMU sample stamp; the
# rest fall where the bridge dropped a Gazebo sample.
CLOCK_PAIRING_MIN_SHARE = 0.99
# Periodic streams whose stamp gaps reveal samples lost before the bag.
PERIODIC_TOPICS = ("/clock", "/fmu/out/sensor_combined", GAZEBO_IMU, "/simulation/ground_truth/drone/state")


def missing_samples(values: list[int]) -> int:
    """Samples missing from a periodic stream: gaps beyond 1.5 median periods."""
    gaps = [b - a for a, b in zip(values, values[1:]) if b > a]
    if len(gaps) < 10:
        return 0
    period = statistics.median(gaps)
    return sum(max(0, round(gap / period) - 1) for gap in gaps if gap > 1.5 * period)


def quaternion_yaw(w: float, x: float, y: float, z: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def stamp_ns(header: Any) -> int:
    return int(header.stamp.sec) * 1_000_000_000 + int(header.stamp.nanosec)


def summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0],
        "p50": ordered[len(ordered) // 2],
        "p99": ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))],
        "max": ordered[-1],
        "mean": statistics.fmean(values),
    }


def analyze(bag_dir: Path, mission_evidence: Path | None = None) -> dict[str, Any]:
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id=""),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    classes = {name: get_message(kind) for name, kind in types.items()}
    decode = set(PX4_TOPICS) | set(RADAR_TOPICS) | set(CAMERA_TOPICS) | {"/clock", TRUTH_ODOMETRY, CAMERA_TRUTH}
    decode |= ({GAZEBO_IMU} | set(PERIODIC_TOPICS)) & set(types)

    counts: dict[str, int] = defaultdict(int)
    first_receipt: dict[str, int] = {}
    last_receipt: dict[str, int] = {}
    clock_receipt: list[int] = []
    clock_sim: list[int] = []
    px4_offsets: dict[str, list[float]] = defaultdict(list)
    px4_sample_lag: dict[str, list[float]] = defaultdict(list)
    sensor_lag: dict[str, list[float]] = defaultdict(list)
    stamps: dict[str, list[int]] = defaultdict(list)
    radar_points: dict[str, list[int]] = defaultdict(list)
    px4_heading: list[tuple[int, float]] = []
    truth_heading: list[tuple[int, float]] = []
    px4_imu_stamps: list[int] = []
    gazebo_imu_stamps: list[int] = []

    while reader.has_next():
        topic, raw, receipt_ns = reader.read_next()
        counts[topic] += 1
        first_receipt.setdefault(topic, receipt_ns)
        last_receipt[topic] = receipt_ns
        if topic not in decode:
            continue
        message = deserialize_message(raw, classes[topic])
        if topic == "/clock":
            clock_receipt.append(receipt_ns)
            clock_sim.append(int(message.clock.sec) * 1_000_000_000 + int(message.clock.nanosec))
            continue
        if topic == GAZEBO_IMU:
            gazebo_imu_stamps.append(stamp_ns(message.header))
            continue
        if topic == TRUTH_ODOMETRY:
            # Gazebo world ENU, body FLU -> heading in NED.
            q = message.pose.pose.orientation
            truth_heading.append((stamp_ns(message.header), wrap(math.pi / 2.0 - quaternion_yaw(q.w, q.x, q.y, q.z))))
            continue
        index = bisect.bisect_right(clock_receipt, receipt_ns) - 1
        sim_now = clock_sim[index] if index >= 0 else None
        if topic in PX4_TOPICS:
            timestamp_ns = int(message.timestamp) * 1000
            if sim_now is not None:
                px4_offsets[topic].append((timestamp_ns - sim_now) / 1e6)
            if hasattr(message, "timestamp_sample") and int(message.timestamp_sample) > 0:
                px4_sample_lag[topic].append((int(message.timestamp) - int(message.timestamp_sample)) / 1e3)
            if topic == "/fmu/out/vehicle_odometry":
                # Body FRD to NED.
                px4_heading.append((timestamp_ns, quaternion_yaw(*(float(value) for value in message.q))))
            if topic == "/fmu/out/sensor_combined":
                px4_imu_stamps.append(timestamp_ns)
            continue
        header_ns = stamp_ns(message.header)
        stamps[topic].append(header_ns)
        if sim_now is not None:
            sensor_lag[topic].append((sim_now - header_ns) / 1e6)
        if topic in RADAR_TOPICS:
            radar_points[topic].append(int(message.width) * int(message.height))

    duration_s = (max(last_receipt.values()) - min(first_receipt.values())) / 1e9 if counts else 0.0
    rates = {
        topic: counts[topic] / ((last_receipt[topic] - first_receipt[topic]) / 1e9)
        for topic in counts if counts[topic] > 1 and last_receipt[topic] > first_receipt[topic]
    }
    rtf = None
    if len(clock_receipt) > 1:
        rtf = (clock_sim[-1] - clock_sim[0]) / (clock_receipt[-1] - clock_receipt[0])

    def stamp_gaps(topic: str) -> dict[str, float] | None:
        values = stamps[topic]
        return summary([(b - a) / 1e6 for a, b in zip(values, values[1:])])

    # Radar-F is hardware-triggered 5.10112 ms after Radar-U within each frame.
    trigger_offsets = []
    up, forward = stamps[RADAR_TOPICS[0]], stamps[RADAR_TOPICS[1]]
    for stamp in forward:
        index = bisect.bisect_right(up, stamp) - 1
        if index >= 0:
            trigger_offsets.append((stamp - up[index]) / 1e6)
    truth_heading.sort()
    truth_times = [stamp for stamp, _ in truth_heading]
    heading_errors = []
    for stamp, heading in px4_heading:
        index = bisect.bisect_left(truth_times, stamp)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(truth_times)]
        if candidates:
            nearest = min(candidates, key=lambda i: abs(truth_times[i] - stamp))
            if abs(truth_times[nearest] - stamp) <= HEADING_MATCH_NS:
                heading_errors.append(wrap(heading - truth_heading[nearest][1]))
    periodic_stamps = {
        "/clock": sorted(clock_sim),
        "/fmu/out/sensor_combined": sorted(px4_imu_stamps),
        GAZEBO_IMU: sorted(gazebo_imu_stamps),
        "/simulation/ground_truth/drone/state": sorted(stamps["/simulation/ground_truth/drone/state"]),
    }
    clock_pairing = None
    if gazebo_imu_stamps:
        gazebo_set = set(gazebo_imu_stamps)
        exact = sum(1 for stamp in px4_imu_stamps if stamp in gazebo_set)
        clock_pairing = {"px4_imu_samples": len(px4_imu_stamps), "equal_to_a_gazebo_imu_stamp": exact,
                         "share": exact / len(px4_imu_stamps) if px4_imu_stamps else 0.0}
    # At the bag boundaries a camera_info can lack its image or the reverse
    # (earlier recordings emitted camera_info after the frame's truth render).
    # Compare the stamps in the span both topics cover.
    images, infos = stamps[CAMERA_TOPICS[0]], stamps[CAMERA_TOPICS[1]]
    span = (max(images[0], infos[0]), min(images[-1], infos[-1])) if images and infos else (1, 0)
    image_span = {stamp for stamp in images if span[0] <= stamp <= span[1]}
    info_span = {stamp for stamp in infos if span[0] <= stamp <= span[1]}
    mission_coverage = None
    if mission_evidence is not None:
        phases = json.loads(mission_evidence.read_text(encoding="utf-8"))["planned_phases"]
        mission_begin = int(phases[0]["source_time_interval_ns"]["begin"])
        mission_end = int(phases[-1]["source_time_interval_ns"]["end"])
        mission_images = [stamp for stamp in images if mission_begin <= stamp <= mission_end]
        info_set, truth_set = set(infos), set(stamps[CAMERA_TRUTH])
        mission_coverage = {
            "legs_source_time_ns": [mission_begin, mission_end],
            "images": len(mission_images),
            "without_camera_info": sum(1 for stamp in mission_images if stamp not in info_set),
            "without_camera_truth": sum(1 for stamp in mission_images if stamp not in truth_set),
        }

    missing = [topic for topic in RECORD_TOPICS if counts.get(topic, 0) == 0 and topic not in EMPTY_BY_CONTRACT]
    extra = sorted(set(counts) - set(RECORD_TOPICS))
    checks = {
        "every contract topic has messages": not missing,
        "timesync topic is empty (UXRCE_DDS_SYNCT=0)": all(counts.get(topic, 0) == 0 for topic in EMPTY_BY_CONTRACT),
        "no topic outside the contract": not extra,
        "camera_info stamps equal image stamps in their common span": bool(info_span) and info_span == image_span,
        "radar and camera stamps are monotonic": all(
            all(b > a for a, b in zip(stamps[t], stamps[t][1:])) for t in (*RADAR_TOPICS, CAMERA_TOPICS[0])
        ),
    }
    if clock_pairing is not None:
        checks["PX4 IMU timestamps pair with Gazebo IMU samples"] = clock_pairing["share"] >= CLOCK_PAIRING_MIN_SHARE
    if mission_coverage is not None:
        checks["every image during the legs has camera_info and camera truth"] = (
            mission_coverage["without_camera_info"] == 0 and mission_coverage["without_camera_truth"] == 0
        )
    return {
        "bag": str(bag_dir),
        "duration_s": duration_s,
        "real_time_factor": rtf,
        "message_counts": dict(sorted(counts.items())),
        "rates_hz": dict(sorted(rates.items())),
        "missing_contract_topics": missing,
        "extra_topics": extra,
        "px4_timestamp_minus_sim_clock_ms": {t: summary(v) for t, v in px4_offsets.items()},
        "px4_timestamp_minus_timestamp_sample_ms": {t: summary(v) for t, v in px4_sample_lag.items()},
        "sim_clock_at_receipt_minus_header_stamp_ms": {t: summary(v) for t, v in sensor_lag.items()},
        "header_stamp_gaps_ms": {t: stamp_gaps(t) for t in (*RADAR_TOPICS, *CAMERA_TOPICS)},
        "radar_forward_minus_up_trigger_ms": summary(trigger_offsets),
        "radar_points_per_scan": {t: summary([float(v) for v in values]) for t, values in radar_points.items()},
        "px4_heading_minus_truth_heading_rad": summary(heading_errors),
        "px4_gazebo_imu_clock_pairing": clock_pairing,
        "missing_samples": {topic: missing_samples(values) for topic, values in periodic_stamps.items() if values},
        "camera_coverage_during_legs": mission_coverage,
        "camera_info_and_image_stamps_in_common_span": {
            "matching": len(info_span & image_span), "camera_info": len(info_span), "image": len(image_span),
            "camera_info_outside_span": len(infos) - len(info_span), "images_outside_span": len(images) - len(image_span),
        },
        "checks": checks,
        "success": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bag", type=Path, help="bag directory of a recorded flight")
    parser.add_argument("--output", type=Path, help="write the report as JSON")
    parser.add_argument("--mission-evidence", type=Path,
                        help="the flight's mission_phase_evidence.json, to check camera coverage during the legs")
    args = parser.parse_args()
    report = analyze(args.bag, args.mission_evidence)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
