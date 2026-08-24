#!/usr/bin/env python3
"""Inspect synchronization and identity invariants in a perception dataset bag."""

from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path
from typing import Any


TOPICS = {
    "drone": "/simulation/ground_truth/drone/state",
    "radar": "/sensor/mmwave/points_full",
    "radar_truth": "/simulation/ground_truth/mmwave/scan",
    "camera": "/sensor/cable_camera/image_raw",
    "camera_mask": "/simulation/ground_truth/cable_camera/conductor_instance_mask",
    "camera_truth": "/simulation/ground_truth/cable_camera/frame",
    "geometry": "/simulation/ground_truth/conductors/geometry",
}


def stamp_ns(message: Any) -> int:
    return int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)


def finite(values: list[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def inspect_bag(bag: Path) -> dict[str, Any]:
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id=""),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    message_types = {name: get_message(type_name) for name, type_name in topic_types.items()}
    counts: collections.Counter[str] = collections.Counter()
    messages: dict[str, list[Any]] = collections.defaultdict(list)
    wanted = set(TOPICS.values())
    while reader.has_next():
        topic, data, _ = reader.read_next()
        counts[topic] += 1
        if topic in wanted:
            messages[topic].append(deserialize_message(data, message_types[topic]))

    geometry_messages = messages[TOPICS["geometry"]]
    physical_ids = (
        [item.physical_id for item in geometry_messages[-1].conductors]
        if geometry_messages else []
    )
    valid_ids = set(physical_ids)

    drone = messages[TOPICS["drone"]]
    drone_stamps = [stamp_ns(item) for item in drone]
    drone_duration = (drone_stamps[-1] - drone_stamps[0]) / 1e9 if len(drone_stamps) > 1 else 0.0
    drone_rate = (len(drone_stamps) - 1) / drone_duration if drone_duration > 0 else 0.0
    drone_valid = all(
        abs(math.sqrt(
            item.pose_world.orientation.x ** 2 + item.pose_world.orientation.y ** 2 +
            item.pose_world.orientation.z ** 2 + item.pose_world.orientation.w ** 2
        ) - 1.0) < 1e-5 and finite([
            item.twist_world.linear.x, item.twist_world.linear.y, item.twist_world.linear.z,
            item.twist_world.angular.x, item.twist_world.angular.y, item.twist_world.angular.z,
        ]) and item.header.frame_id == "world" and item.source_link_name == "base_link"
        for item in drone
    ) and all(first < second for first, second in zip(drone_stamps, drone_stamps[1:]))

    radar_by_stamp = {stamp_ns(item): item for item in messages[TOPICS["radar"]]}
    radar_truth_by_stamp = {stamp_ns(item): item for item in messages[TOPICS["radar_truth"]]}
    radar_matches = sorted(set(radar_by_stamp) & set(radar_truth_by_stamp))
    conductor_returns: collections.Counter[str] = collections.Counter()
    source_classes: collections.Counter[str] = collections.Counter()
    # rosbag creates subscriptions independently, so a truth publisher may be
    # connected a few frames before its sensor bridge at the bag boundary. The
    # invariant is that every recorded sensor measurement has exactly one
    # matching truth message; boundary-only extra truth is reported separately.
    radar_alignment_valid = len(radar_matches) == len(radar_by_stamp)
    for stamp in radar_matches:
        cloud = radar_by_stamp[stamp]
        truth = radar_truth_by_stamp[stamp]
        radar_alignment_valid &= cloud.width * cloud.height == len(truth.points)
        for index, point in enumerate(truth.points):
            radar_alignment_valid &= point.source_point_index == index
            class_name = {
                1: "VALID_PHYSICAL_CONDUCTOR", 2: "PHANTOM",
                3: "CLUTTER_NO_PHYSICAL_SOURCE",
            }.get(point.source_class, f"UNKNOWN_{point.source_class}")
            source_classes[class_name] += 1
            if point.source_class == 1:
                conductor_returns[point.physical_conductor_id] += 1
                radar_alignment_valid &= point.physical_conductor_id in valid_ids
            else:
                radar_alignment_valid &= not point.physical_conductor_id
            radar_alignment_valid &= finite([
                point.ideal_generating_point_world.x, point.ideal_generating_point_world.y,
                point.ideal_generating_point_world.z, point.ideal_generating_point_sensor.x,
                point.ideal_generating_point_sensor.y, point.ideal_generating_point_sensor.z,
            ])

    camera_by_stamp = {stamp_ns(item): item for item in messages[TOPICS["camera"]]}
    mask_by_stamp = {stamp_ns(item): item for item in messages[TOPICS["camera_mask"]]}
    camera_truth_by_stamp = {stamp_ns(item): item for item in messages[TOPICS["camera_truth"]]}
    camera_matches = sorted(set(camera_by_stamp) & set(mask_by_stamp) & set(camera_truth_by_stamp))
    visible_frames: collections.Counter[str] = collections.Counter()
    camera_alignment_valid = len(camera_matches) == len(camera_by_stamp)
    for stamp in camera_matches:
        image, mask, truth = camera_by_stamp[stamp], mask_by_stamp[stamp], camera_truth_by_stamp[stamp]
        camera_alignment_valid &= (
            image.width == mask.width == truth.image_width and
            image.height == mask.height == truth.image_height
        )
        camera_alignment_valid &= {item.physical_conductor_id for item in truth.conductors} == valid_ids
        for item in truth.conductors:
            if item.visibility_state == item.VISIBLE:
                visible_frames[item.physical_conductor_id] += 1
                camera_alignment_valid &= (
                    item.has_visible_bounding_box and item.min_u <= item.max_u < image.width and
                    item.min_v <= item.max_v < image.height and item.visible_pixel_count > 0
                )
            else:
                camera_alignment_valid &= not item.has_visible_bounding_box and item.visible_pixel_count == 0

    checks = {
        "static_geometry_present": bool(geometry_messages) and len(valid_ids) == len(physical_ids),
        "drone_state_valid": bool(drone) and drone_valid,
        "radar_truth_aligned": bool(radar_by_stamp) and radar_alignment_valid,
        "camera_truth_aligned": bool(camera_by_stamp) and camera_alignment_valid,
        "shared_ids_consistent": bool(valid_ids) and
            set(conductor_returns).issubset(valid_ids) and set(visible_frames).issubset(valid_ids),
    }
    return {
        "success": all(checks.values()),
        "checks": checks,
        "topics": {
            name: {"type": type_name, "message_count": counts[name]}
            for name, type_name in sorted(topic_types.items())
        },
        "physical_conductor_ids": physical_ids,
        "drone_ground_truth": {"message_count": len(drone), "rate_hz": drone_rate},
        "radar": {
            "scan_count": len(radar_by_stamp), "truth_count": len(radar_truth_by_stamp),
            "matched_count": len(radar_matches), "returns_per_conductor": dict(conductor_returns),
            "boundary_truth_without_recorded_scan": len(set(radar_truth_by_stamp) - set(radar_by_stamp)),
            "source_class_counts": dict(source_classes),
            "phantom_count": source_classes["PHANTOM"],
            "clutter_count": source_classes["CLUTTER_NO_PHYSICAL_SOURCE"],
        },
        "camera": {
            "frame_count": len(camera_by_stamp), "mask_count": len(mask_by_stamp),
            "truth_count": len(camera_truth_by_stamp), "matched_count": len(camera_matches),
            "boundary_masks_without_recorded_frame": len(set(mask_by_stamp) - set(camera_by_stamp)),
            "boundary_truth_without_recorded_frame": len(set(camera_truth_by_stamp) - set(camera_by_stamp)),
            "visible_frame_count_per_conductor": dict(visible_frames),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path, help="rosbag2 directory")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = inspect_bag(args.bag.resolve())
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json_output:
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
