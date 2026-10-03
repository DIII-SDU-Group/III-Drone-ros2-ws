#!/usr/bin/env python3
"""Classify a passive HIL perception-seam observation.

The probe deliberately writes a small, transport-independent JSON summary after
recording.  Keeping classification separate from ROS 2 makes the result
reproducible in unit tests and keeps missing topics as explicit negative
evidence instead of silently treating them as zeroes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "hil-perception-seam-summary/v1"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(observations: dict[str, Any], name: str) -> int | None:
    value = _mapping(observations.get(name)).get("count")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def _present(observations: dict[str, Any], name: str) -> bool | None:
    stage = _mapping(observations.get(name))
    if "present" in stage:
        return bool(stage["present"])
    count = _count(observations, name)
    return None if count is None else count > 0


def _active(observations: dict[str, Any], name: str) -> bool:
    stage = _mapping(observations.get(name))
    if "active" in stage:
        return bool(stage["active"])
    if stage.get("type") == "sensor_msgs/msg/PointCloud2":
        return int(stage.get("max_point_count", 0)) > 0 or int(stage.get("nonempty_count", 0)) > 0
    count = _count(observations, name)
    return count is not None and count > 0


def _mapper_advancing(observations: dict[str, Any]) -> bool:
    stage = _mapping(observations.get("mapper"))
    if "advancing" in stage:
        return bool(stage["advancing"])
    first = stage.get("first_stamp_ns")
    last = stage.get("last_stamp_ns")
    if isinstance(first, (int, float)) and isinstance(last, (int, float)):
        return last > first
    count = _count(observations, "mapper")
    return count is not None and count > 1


def _mapper_line_max(observations: dict[str, Any]) -> int | None:
    stage = _mapping(observations.get("mapper"))
    value = stage.get("max_line_count", stage.get("line_max"))
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def _tf_ok(summary: dict[str, Any]) -> bool:
    tf = _mapping(summary.get("tf"))
    lookup = _mapping(tf.get("lookup"))
    if "success" in lookup:
        return bool(lookup["success"])
    if "lookup_success" in tf:
        return bool(tf["lookup_success"])
    samples = lookup.get("samples", tf.get("samples"))
    return isinstance(samples, (int, float)) and samples > 0


def _topic_name(observations: dict[str, Any], name: str) -> str | None:
    topic = _mapping(observations.get(name)).get("topic")
    return str(topic) if topic else None


def _required_negative_evidence(summary: dict[str, Any]) -> list[dict[str, Any]]:
    observations = _mapping(summary.get("observations"))
    missing: list[dict[str, Any]] = []
    for stage in (
        "workstation_source",
        "workstation_relay",
        "pi_camera",
        "hough",
        "direction",
        "mmwave",
        "mapper",
    ):
        present = _present(observations, stage)
        count = _count(observations, stage)
        if present is not True or count == 0:
            missing.append(
                {
                    "stage": stage,
                    "topic": _topic_name(observations, stage),
                    "present": present,
                    "count": count,
                }
            )
    tf = _mapping(summary.get("tf"))
    if not _tf_ok(summary):
        missing.append(
            {
                "stage": "tf_lookup",
                "topic": tf.get("topic", "/tf"),
                "present": bool(tf.get("present", False)),
                "count": _mapping(tf.get("lookup")).get("samples", tf.get("samples")),
            }
        )
    return missing


def classify_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative classification and all relevant evidence.

    A summary is intentionally not repaired here.  Unknown or contradictory
    inputs become ``inconclusive`` and are retained verbatim in the result.
    """

    observations = _mapping(summary.get("observations"))
    recording_complete = summary.get("recording_complete")
    fixture_valid = summary.get("fixture_valid")
    contradictions: list[str] = []

    if recording_complete is not True:
        classification = "recording-incomplete"
        reason = "recording_complete was not true"
    elif fixture_valid is not True:
        classification = "inconclusive"
        reason = "fixture_valid was not true"
    else:
        source = _active(observations, "workstation_source")
        relay = _active(observations, "workstation_relay")
        pi_camera = _active(observations, "pi_camera")
        hough = _active(observations, "hough")
        direction = _active(observations, "direction")
        mmwave = _active(observations, "mmwave")
        mapper = _active(observations, "mapper")
        tf_ok = _tf_ok(summary)
        mapper_advancing = _mapper_advancing(observations)
        line_max = _mapper_line_max(observations)

        if _count(observations, "workstation_source") not in (None, 0) and not source:
            contradictions.append("workstation_source has samples but is marked inactive")
        if _count(observations, "pi_camera") not in (None, 0) and not pi_camera:
            contradictions.append("pi_camera has samples but is marked inactive")
        if line_max is not None and line_max > 0 and not mapper:
            contradictions.append("mapper has positive line evidence but is marked inactive")

        candidates: list[str] = []
        reasons: dict[str, str] = {}
        if not source:
            candidates.append("H1 source stall")
            reasons["H1 source stall"] = "workstation camera source has no positive traffic evidence"
        if source and (not relay or not pi_camera):
            candidates.append("H2 cross-host relay loss")
            reasons["H2 cross-host relay loss"] = (
                "workstation source is active but relay or Pi camera receipt is absent"
            )
        if pi_camera and not hough:
            candidates.append("H3 camera present but no Hough output")
            reasons["H3 camera present but no Hough output"] = (
                "Pi camera traffic is present but no Hough angle was observed"
            )
        if hough and (not direction or not tf_ok):
            candidates.append("H4 Hough present but no direction/TF")
            reasons["H4 Hough present but no direction/TF"] = (
                "Hough output exists but direction traffic or the required TF lookup is absent"
            )
        if direction and tf_ok and mmwave and (not mapper or not mapper_advancing or (line_max is not None and line_max <= 0)):
            candidates.append("H5 direction+mmWave present but mapper cannot form/advance")
            reasons["H5 direction+mmWave present but mapper cannot form/advance"] = (
                "direction and mmWave traffic exist but mapper output is absent, static, or empty"
            )
        if source and relay and pi_camera and hough and direction and tf_ok and mmwave and mapper and mapper_advancing and (line_max is not None and line_max > 0):
            candidates.append("positive seam")
            reasons["positive seam"] = "all observed seam stages are active and mapper output advances with lines"

        if not candidates:
            contradictions.append("no hypothesis predicate was satisfied by the available evidence")
        if len(candidates) > 1:
            contradictions.append("multiple mutually exclusive seam predicates were satisfied")

        if contradictions:
            classification = "inconclusive"
            reason = "; ".join(contradictions)
        elif candidates:
            classification = candidates[0]
            reason = reasons[classification]
        else:
            classification = "inconclusive"
            reason = "insufficient positive or negative evidence"

    return {
        "schema": "hil-perception-seam-analysis/v1",
        "run_id": summary.get("run_id"),
        "classification": classification,
        "reason": reason,
        "candidate_hypotheses": locals().get("candidates", []),
        "contradictions": contradictions,
        "negative_evidence": _required_negative_evidence(summary),
        "summary": summary,
    }


SEAM_TOPICS = {
    "workstation_source": "/simulation/local/cable_camera/image_raw",
    "workstation_relay": "/sensor/cable_camera/image_raw",
    "pi_camera": "/sensor/cable_camera/image_raw",
    "hough": "/perception/hough_transformer/cable_yaw_angle",
    "direction": "/perception/pl_dir_computer/powerline_direction_quat",
    "mmwave": "/sensor/mmwave/points",
    "mapper": "/perception/pl_mapper/powerline",
}

WORKSTATION_RECORDING_CONTRACT = {
    "/simulation/local/cable_camera/image_raw": "sensor_msgs/msg/Image",
    "/sensor/cable_camera/image_raw": "sensor_msgs/msg/Image",
    "/sensor/mmwave/points": "sensor_msgs/msg/PointCloud2",
    "/simulation/ground_truth/drone/odometry": "nav_msgs/msg/Odometry",
    "/tf": "tf2_msgs/msg/TFMessage",
    "/tf_static": "tf2_msgs/msg/TFMessage",
    "/clock": "rosgraph_msgs/msg/Clock",
    "/rosout": "rcl_interfaces/msg/Log",
}

PI_RECORDING_CONTRACT = {
    "/sensor/cable_camera/image_raw": "sensor_msgs/msg/Image",
    "/sensor/mmwave/points": "sensor_msgs/msg/PointCloud2",
    "/perception/hough_transformer/cable_yaw_angle": "std_msgs/msg/Float32",
    "/perception/pl_dir_computer/status": "iii_drone_interfaces/msg/StringStamped",
    "/perception/pl_dir_computer/powerline_direction_quat": "geometry_msgs/msg/QuaternionStamped",
    "/perception/pl_mapper/powerline": "iii_drone_interfaces/msg/Powerline",
    "/perception/pl_mapper/points_est": "sensor_msgs/msg/PointCloud2",
    "/perception/pl_mapper/transformed_points": "sensor_msgs/msg/PointCloud2",
    "/perception/pl_mapper/projected_points": "sensor_msgs/msg/PointCloud2",
    "/fmu/out/vehicle_status_v1": "px4_msgs/msg/VehicleStatus",
    "/fmu/out/vehicle_land_detected": "px4_msgs/msg/VehicleLandDetected",
    "/fmu/out/vehicle_odometry": "px4_msgs/msg/VehicleOdometry",
    "/tf": "tf2_msgs/msg/TFMessage",
    "/tf_static": "tf2_msgs/msg/TFMessage",
    "/clock": "rosgraph_msgs/msg/Clock",
    "/rosout": "rcl_interfaces/msg/Log",
}

REQUIRED_TYPES = {
    **WORKSTATION_RECORDING_CONTRACT,
    **PI_RECORDING_CONTRACT,
}


def _message_stamp_ns(message: Any) -> int | None:
    stamp = getattr(message, "stamp", None)
    if stamp is None:
        header = getattr(message, "header", None)
        stamp = getattr(header, "stamp", None)
    if stamp is None or not hasattr(stamp, "sec"):
        return None
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _pointcloud_point_count(message: Any) -> int:
    return max(0, int(getattr(message, "width", 0))) * max(0, int(getattr(message, "height", 0)))


def _bag_counts(
    bag: Path,
    wanted: set[str],
    barrier_bag_timestamp_ns: int | None,
) -> dict[str, Any]:
    """Read a rosbag2 bag when ROS is available; keep this import optional."""

    try:
        import rosbag2_py  # type: ignore
        from rclpy.serialization import deserialize_message  # type: ignore
        from rosidl_runtime_py.utilities import get_message  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only off ROS hosts
        raise RuntimeError("rosbag2_py and ROS message support are required for bag analysis") from exc

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    result: dict[str, Any] = {
        topic: {
            "topic": topic,
            "type": topic_types.get(topic),
            "expected_type": REQUIRED_TYPES.get(topic),
            "bag_topic_present": topic in topic_types,
            "present": topic in topic_types,
            "type_valid": topic in topic_types and (
                topic not in REQUIRED_TYPES or topic_types[topic] == REQUIRED_TYPES[topic]
            ),
            "count": 0,
            "pre_barrier_count": 0,
            "header_stamps_ns": [],
            "values": [],
            "quaternions": [],
            "nonempty_count": 0,
            "point_counts": [],
            "total_point_count": 0,
            "max_point_count": 0,
            "line_counts": [],
            "line_ids": [],
            "internal_stamps": [],
            "decode_errors": [],
        }
        for topic in wanted
    }
    message_classes: dict[str, Any] = {}
    while reader.has_next():
        topic, raw, bag_timestamp = reader.read_next()
        if topic not in wanted:
            continue
        stage = result[topic]
        if barrier_bag_timestamp_ns is not None and bag_timestamp < barrier_bag_timestamp_ns:
            stage["pre_barrier_count"] += 1
            continue
        stage["count"] += 1
        stage.setdefault("first_bag_timestamp_ns", bag_timestamp)
        stage["last_bag_timestamp_ns"] = bag_timestamp
        type_name = topic_types.get(topic)
        if not type_name or not stage["type_valid"]:
            continue
        try:
            message_class = message_classes.setdefault(type_name, get_message(type_name))
            message = deserialize_message(raw, message_class)
            stamp_ns = _message_stamp_ns(message)
            if stamp_ns is not None:
                stage["header_stamps_ns"].append(stamp_ns)
            if topic.endswith("cable_yaw_angle"):
                stage["values"].append(float(getattr(message, "data", 0.0)))
            elif topic.endswith("powerline_direction_quat"):
                quaternion = getattr(message, "quaternion", None)
                value = {
                    "x": float(getattr(quaternion, "x", 0.0)),
                    "y": float(getattr(quaternion, "y", 0.0)),
                    "z": float(getattr(quaternion, "z", 0.0)),
                    "w": float(getattr(quaternion, "w", 0.0)),
                }
                stage["quaternions"].append(value)
            elif type_name == "sensor_msgs/msg/PointCloud2":
                point_count = _pointcloud_point_count(message)
                stage["point_counts"].append(point_count)
                stage["total_point_count"] += point_count
                stage["max_point_count"] = max(stage["max_point_count"], point_count)
                if point_count > 0:
                    stage["nonempty_count"] += 1
            elif topic == SEAM_TOPICS["mapper"]:
                stage["internal_stamps"].append(stamp_ns)
                lines = list(getattr(message, "lines", []))
                stage["line_counts"].append(len(lines))
                stage["line_ids"].extend(int(getattr(line, "id", -1)) for line in lines)
        except Exception as exc:  # retain the record but make decoding failure visible
            stage["decode_errors"].append(str(exc))
    for stage in result.values():
        stamps = [item for item in stage.pop("internal_stamps", []) if item is not None]
        stage["unique_internal_stamp_count"] = len(set(stamps))
        stage["advancing"] = len(set(stamps)) > 1 if stamps else stage["count"] > 1
        stage["max_line_count"] = max(stage["line_counts"], default=0)
        stage["min_gap_ns"] = min(
            (right - left for left, right in zip(stage["header_stamps_ns"], stage["header_stamps_ns"][1:])),
            default=None,
        )
        stage["max_gap_ns"] = max(
            (right - left for left, right in zip(stage["header_stamps_ns"], stage["header_stamps_ns"][1:])),
            default=None,
        )
        stage["non_identity_count"] = sum(
            not (
                abs(item.get("x", 0.0)) < 1e-9
                and abs(item.get("y", 0.0)) < 1e-9
                and abs(item.get("z", 0.0)) < 1e-9
                and abs(item.get("w", 0.0) - 1.0) < 1e-9
            )
            for item in stage["quaternions"]
        )
    return result


def _apply_recording_contract(
    counts: dict[str, dict[str, Any]],
    advertised_types: dict[str, Any],
    contract: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], bool]:
    completed: dict[str, dict[str, Any]] = {}
    for topic, expected_type in contract.items():
        stage = dict(
            counts.get(
                topic,
                {
                    "topic": topic,
                    "type": None,
                    "bag_topic_present": False,
                    "present": False,
                    "count": 0,
                    "decode_errors": [],
                },
            )
        )
        bag_type = stage.get("type")
        advertised_type = advertised_types.get(topic)
        stage["topic"] = topic
        stage["expected_type"] = expected_type
        stage["bag_topic_present"] = bool(stage.get("bag_topic_present", False))
        stage["advertised_type"] = advertised_type if isinstance(advertised_type, str) else None
        stage["present"] = stage["bag_topic_present"] or stage["advertised_type"] is not None
        stage["type"] = bag_type if isinstance(bag_type, str) else stage["advertised_type"]
        stage["type_valid"] = stage["type"] == expected_type
        stage["message_count_observed"] = (
            type(stage.get("count")) is int and stage["count"] >= 0
        )
        completed[topic] = stage

    contract_complete = all(
        stage["present"] and stage["type_valid"] and stage["message_count_observed"]
        for stage in completed.values()
    )
    return completed, contract_complete


def summary_from_bags(
    workstation_bag: Path,
    pi_bag: Path,
    manifest: dict[str, Any] | None = None,
    tf_lookup_success: bool | None = None,
    fixture_valid: bool = True,
    fixture_reason: str | None = None,
    run_id: str | None = None,
    workstation_barrier_bag_timestamp_ns: int | None = None,
    pi_barrier_bag_timestamp_ns: int | None = None,
    barrier_valid: bool = False,
    mapper_ack_success: bool | None = None,
) -> dict[str, Any]:
    """Build the classifier input from the two host-local recordings."""

    manifest = manifest or {}
    workstation_manifest = _mapping(manifest.get("workstation"))
    pi_manifest = _mapping(manifest.get("pi"))
    ws_topics = set(WORKSTATION_RECORDING_CONTRACT)
    pi_topics = set(PI_RECORDING_CONTRACT)
    ws_raw_counts = _bag_counts(
        workstation_bag,
        ws_topics,
        workstation_barrier_bag_timestamp_ns,
    )
    pi_raw_counts = _bag_counts(
        pi_bag,
        pi_topics,
        pi_barrier_bag_timestamp_ns,
    )
    ws_counts, ws_contract_complete = _apply_recording_contract(
        ws_raw_counts,
        workstation_manifest,
        WORKSTATION_RECORDING_CONTRACT,
    )
    pi_counts, pi_contract_complete = _apply_recording_contract(
        pi_raw_counts,
        pi_manifest,
        PI_RECORDING_CONTRACT,
    )
    observations: dict[str, Any] = {}
    for stage, topic in SEAM_TOPICS.items():
        source = ws_counts if stage in {"workstation_source", "workstation_relay"} else pi_counts
        observations[stage] = dict(source[topic])
    for stage, topic in {
        "direction_status": "/perception/pl_dir_computer/status",
        "mapper_points_est": "/perception/pl_mapper/points_est",
        "mapper_transformed_points": "/perception/pl_mapper/transformed_points",
        "mapper_projected_points": "/perception/pl_mapper/projected_points",
    }.items():
        observations[stage] = dict(pi_counts[topic])
    tf_topic = "/tf"
    tf_count = pi_counts.get(tf_topic, {"topic": tf_topic, "present": tf_topic in pi_manifest, "count": 0})
    tf_success = bool(tf_lookup_success) if tf_lookup_success is not None else bool(tf_count.get("count", 0))
    all_stages = list(ws_counts.values()) + list(pi_counts.values())
    metadata_complete = all((bag / "metadata.yaml").exists() for bag in (workstation_bag, pi_bag))
    type_complete = all(stage["type_valid"] for stage in all_stages)
    message_count_complete = all(stage["message_count_observed"] for stage in all_stages)
    decode_complete = all(not stage.get("decode_errors") for stage in all_stages)
    recording_contract_complete = ws_contract_complete and pi_contract_complete
    quality = {
        "metadata_complete": metadata_complete,
        "recording_contract_complete": recording_contract_complete,
        "required_types_valid": type_complete,
        "message_count_complete": message_count_complete,
        "decode_complete": decode_complete,
        "barrier_valid": barrier_valid,
        "mapper_ack_success": mapper_ack_success is True,
        "workstation_barrier_bag_timestamp_ns": workstation_barrier_bag_timestamp_ns,
        "pi_barrier_bag_timestamp_ns": pi_barrier_bag_timestamp_ns,
    }
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "recording_complete": all(quality.values()),
        "recording_quality": quality,
        "recording_contract": {
            "workstation": ws_counts,
            "pi": pi_counts,
            "workstation_complete": ws_contract_complete,
            "pi_complete": pi_contract_complete,
        },
        "barrier": {
            "valid": barrier_valid,
            "workstation_bag_timestamp_ns": workstation_barrier_bag_timestamp_ns,
            "pi_bag_timestamp_ns": pi_barrier_bag_timestamp_ns,
        },
        "mapper_ack_success": mapper_ack_success,
        "fixture_valid": fixture_valid,
        "fixture": {"reason": fixture_reason} if fixture_reason else {},
        "observations": observations,
        "tf": {
            "topic": tf_topic,
            "present": bool(tf_count.get("present")),
            "lookup": {"success": tf_success, "samples": int(tf_count.get("count", 0))},
        },
        "camera_stamp_matching": {
            "source_to_relay": _stamp_match(
                ws_counts.get(SEAM_TOPICS["workstation_source"], {}).get("header_stamps_ns", []),
                ws_counts.get(SEAM_TOPICS["workstation_relay"], {}).get("header_stamps_ns", []),
            ),
            "relay_to_pi": _stamp_match(
                ws_counts.get(SEAM_TOPICS["workstation_relay"], {}).get("header_stamps_ns", []),
                pi_counts.get(SEAM_TOPICS["pi_camera"], {}).get("header_stamps_ns", []),
            ),
        },
    }


def _stamp_match(left: list[int], right: list[int]) -> dict[str, Any]:
    left_set, right_set = set(left), set(right)
    matched = len(left_set & right_set)
    return {
        "left_unique": len(left_set),
        "right_unique": len(right_set),
        "matched_unique": matched,
        "all_left_matched": bool(left_set) and left_set <= right_set,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workstation-bag", type=Path)
    parser.add_argument("--pi-bag", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--tf-lookup-success", choices=("true", "false"))
    parser.add_argument("--fixture-invalid-reason")
    parser.add_argument("--barrier-json", type=Path)
    parser.add_argument("--mapper-ack-success", choices=("true", "false"))
    parser.add_argument("--run-id")
    return parser.parse_args()


def _validated_reset_barrier(value: Any, run_id: str | None) -> dict[str, Any] | None:
    barrier = _mapping(value)
    workstation_ts = barrier.get("workstation_bag_timestamp_ns")
    pi_ts = barrier.get("pi_bag_timestamp_ns")
    if (
        barrier.get("schema") != "hil-perception-seam-reset-barrier/v2"
        or barrier.get("run_id") != run_id
        or barrier.get("mapper_service") != "/perception/pl_mapper/pl_mapper_command"
        or barrier.get("request") != {"command": 0, "reset": True}
        or barrier.get("mapper_ack") != 0
        or not isinstance(workstation_ts, int)
        or workstation_ts < 0
        or not isinstance(pi_ts, int)
        or pi_ts < 0
    ):
        return None
    return barrier


def main() -> int:
    args = _parse_args()
    if args.workstation_bag or args.pi_bag:
        if not args.workstation_bag or not args.pi_bag:
            raise SystemExit("--workstation-bag and --pi-bag must be supplied together")
        if not args.barrier_json or not args.mapper_ack_success or not args.run_id:
            raise SystemExit(
                "--barrier-json, --mapper-ack-success, and --run-id are required with bag inputs"
            )
        manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else {}
        raw_barrier = (
            json.loads(args.barrier_json.read_text(encoding="utf-8"))
            if args.barrier_json
            else {}
        )
        barrier = _validated_reset_barrier(raw_barrier, args.run_id)
        summary = summary_from_bags(
            args.workstation_bag,
            args.pi_bag,
            manifest=manifest,
            tf_lookup_success=None if args.tf_lookup_success is None else args.tf_lookup_success == "true",
            fixture_valid=args.fixture_invalid_reason is None,
            fixture_reason=args.fixture_invalid_reason,
            run_id=args.run_id,
            workstation_barrier_bag_timestamp_ns=(
                barrier["workstation_bag_timestamp_ns"] if barrier else None
            ),
            pi_barrier_bag_timestamp_ns=barrier["pi_bag_timestamp_ns"] if barrier else None,
            barrier_valid=barrier is not None,
            mapper_ack_success=(
                barrier is not None and args.mapper_ack_success == "true"
            ),
        )
        args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
    result = classify_summary(summary)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
