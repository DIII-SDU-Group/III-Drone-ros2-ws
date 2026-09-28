#!/usr/bin/env python3
"""Record bounded, read-only HIL perception topic diagnostics.

Example::

    source setup/setup_hil.bash
    python3 scripts/workspace/hil_perception_probe.py \
      --artifact-dir runtime/hil-perception-attempt-004 --duration-sec 300

The probe writes one compact JSONL record per elapsed second and saves camera
snapshots at no more than 1 Hz. It never publishes or calls ROS services.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import signal
import time
from typing import Any

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Odometry
from px4_msgs.msg import VehicleOdometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import Image, PointCloud2
from std_msgs.msg import Float32
from iii_drone_interfaces.msg import Powerline


MAX_DURATION_SEC = 3600.0
IMAGE_SAVE_PERIOD_SEC = 1.0
TOPICS = {
    "camera_image": "/sensor/cable_camera/image_raw",
    "mmwave_points": "/sensor/mmwave/points",
    "hough_yaw": "/perception/hough_transformer/cable_yaw_angle",
    "direction_pose": "/perception/pl_dir_computer/powerline_direction_pose",
    "transformed_points": "/perception/pl_mapper/transformed_points",
    "powerline": "/perception/pl_mapper/powerline",
    "gazebo_odometry": "/simulation/ground_truth/drone/odometry",
    "px4_odometry": "/fmu/out/vehicle_odometry",
}


def duration_seconds(value: str) -> float:
    """Parse a finite positive duration capped at one hour."""
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a number of seconds") from exc
    if not math.isfinite(duration) or not 0.0 < duration <= MAX_DURATION_SEC:
        raise argparse.ArgumentTypeError(
            f"duration must be greater than 0 and at most {MAX_DURATION_SEC:g} seconds"
        )
    return duration


def image_metrics(frame_bgr: np.ndarray) -> dict[str, float]:
    """Estimate brightness and edge contrast on a small grayscale thumbnail."""
    height, width = frame_bgr.shape[:2]
    scale = min(1.0, 320.0 / max(height, width))
    if scale < 1.0:
        frame_bgr = cv2.resize(
            frame_bgr,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Laplacian(gray, cv2.CV_32F)
    return {
        "brightness_mean": round(float(np.mean(gray)), 2),
        "brightness_std": round(float(np.std(gray)), 2),
        "edge_contrast": round(float(np.var(edges)), 2),
    }


def _stamp_ns(stamp: Any) -> int | None:
    if stamp is None or not hasattr(stamp, "sec"):
        return None
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def _json_float(value: Any) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _header_metadata(message: Any) -> dict[str, Any]:
    header = getattr(message, "header", None)
    if header is None:
        return {}
    return {
        "source_stamp_ns": _stamp_ns(getattr(header, "stamp", None)),
        "frame_id": str(getattr(header, "frame_id", "")),
    }


class HilPerceptionProbe(Node):
    """Collect bounded topic counts and sparse camera evidence."""

    def __init__(self, artifact_dir: Path, duration_sec: float) -> None:
        super().__init__("hil_perception_probe")
        self._duration_sec = duration_sec
        self._start_monotonic = time.monotonic()
        self._wall_start = datetime.now(timezone.utc)
        self._next_bucket = 0
        self._buckets: dict[int, dict[str, Any]] = {}
        self._totals: Counter[str] = Counter()
        self._last_image_saved_at = float("-inf")
        self._image_sequence = 0
        self._bridge = CvBridge()

        artifact_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = artifact_dir / "perception_probe.jsonl"
        self._jsonl = self._jsonl_path.open("x", encoding="utf-8", buffering=1)

        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._subscriptions = [
            self.create_subscription(Image, TOPICS["camera_image"], self._on_image, qos),
            self.create_subscription(PointCloud2, TOPICS["mmwave_points"], self._on_mmwave, qos),
            self.create_subscription(Float32, TOPICS["hough_yaw"], self._on_hough_yaw, qos),
            self.create_subscription(PoseStamped, TOPICS["direction_pose"], self._on_direction_pose, qos),
            self.create_subscription(
                PointCloud2, TOPICS["transformed_points"], self._on_transformed_points, qos
            ),
            self.create_subscription(Powerline, TOPICS["powerline"], self._on_powerline, qos),
            self.create_subscription(Odometry, TOPICS["gazebo_odometry"], self._on_gazebo_odometry, qos),
            self.create_subscription(
                VehicleOdometry, TOPICS["px4_odometry"], self._on_px4_odometry, qos
            ),
        ]
        self.get_logger().info(
            f"Recording read-only perception diagnostics for {duration_sec:g}s to {self._jsonl_path}"
        )

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_monotonic

    def _bucket(self, index: int) -> dict[str, Any]:
        return self._buckets.setdefault(
            index,
            {"counts": {name: 0 for name in TOPICS}, "last": {}, "saved_camera": None},
        )

    def _record(self, topic: str, message: Any, metadata: dict[str, Any]) -> dict[str, Any]:
        index = int(self._elapsed())
        bucket = self._bucket(index)
        bucket["counts"][topic] += 1
        self._totals[topic] += 1
        data = {**_header_metadata(message), **metadata}
        bucket["last"][topic] = data
        return bucket

    @staticmethod
    def _pointcloud_metadata(message: PointCloud2) -> dict[str, Any]:
        return {
            "width": int(message.width),
            "height": int(message.height),
            "point_count": int(message.width) * int(message.height),
            "point_step": int(message.point_step),
            "row_step": int(message.row_step),
            "fields": [field.name for field in message.fields],
            "is_dense": bool(message.is_dense),
        }

    def _on_image(self, message: Image) -> None:
        metadata = {
            "width": int(message.width),
            "height": int(message.height),
            "encoding": str(message.encoding),
            "step": int(message.step),
            "is_bigendian": bool(message.is_bigendian),
        }
        bucket = self._record("camera_image", message, metadata)
        now = self._elapsed()
        if now - self._last_image_saved_at < IMAGE_SAVE_PERIOD_SEC:
            return
        self._last_image_saved_at = now
        try:
            frame = self._bridge.imgmsg_to_cv2(message, desired_encoding="bgr8")
            metrics = image_metrics(frame)
            self._image_sequence += 1
            filename = f"camera_{self._image_sequence:06d}.jpg"
            saved = cv2.imwrite(
                str(self._jsonl_path.parent / filename),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 85],
            )
            if not saved:
                raise OSError("OpenCV did not save the camera frame")
            bucket["saved_camera"] = {"file": filename, **metrics}
        except Exception as exc:  # Continue counting and diagnosing other topics.
            bucket["saved_camera"] = {"error": f"{type(exc).__name__}: {exc}"}

    def _on_mmwave(self, message: PointCloud2) -> None:
        self._record("mmwave_points", message, self._pointcloud_metadata(message))

    def _on_hough_yaw(self, message: Float32) -> None:
        self._record("hough_yaw", message, {"yaw": _json_float(message.data)})

    def _on_direction_pose(self, message: PoseStamped) -> None:
        pose = message.pose
        self._record(
            "direction_pose",
            message,
            {
                "position": [
                    _json_float(pose.position.x),
                    _json_float(pose.position.y),
                    _json_float(pose.position.z),
                ],
                "orientation_xyzw": [
                    _json_float(pose.orientation.x),
                    _json_float(pose.orientation.y),
                    _json_float(pose.orientation.z),
                    _json_float(pose.orientation.w),
                ],
            },
        )

    def _on_transformed_points(self, message: PointCloud2) -> None:
        self._record("transformed_points", message, self._pointcloud_metadata(message))

    def _on_powerline(self, message: Powerline) -> None:
        line_ids = [int(line.id) for line in message.lines]
        self._record(
            "powerline",
            message,
            {
                "source_stamp_ns": _stamp_ns(message.stamp),
                "line_count": len(line_ids),
                "line_ids": line_ids,
            },
        )

    def _on_gazebo_odometry(self, message: Odometry) -> None:
        position = message.pose.pose.position
        self._record(
            "gazebo_odometry",
            message,
            {
                "position_xyz": [
                    _json_float(position.x),
                    _json_float(position.y),
                    _json_float(position.z),
                ]
            },
        )

    def _on_px4_odometry(self, message: VehicleOdometry) -> None:
        self._record(
            "px4_odometry",
            message,
            {
                "source_timestamp_us": int(message.timestamp),
                "source_stamp_ns": int(message.timestamp) * 1000,
                "position_xyz": [_json_float(value) for value in message.position],
                "pose_frame": int(message.pose_frame),
                "reset_counter": int(message.reset_counter),
            },
        )

    def flush_closed_buckets(self) -> None:
        closed_until = int(self._elapsed())
        while self._next_bucket < closed_until:
            self._write_bucket(self._next_bucket)
            self._next_bucket += 1

    def _write_bucket(self, index: int) -> None:
        data = self._buckets.pop(index, None)
        if data is None:
            data = self._bucket(index)
        record = {
            "record_type": "second",
            "bucket_start_elapsed_sec": index,
            "bucket_start_utc": (self._wall_start + timedelta(seconds=index)).isoformat(),
            **data,
        }
        self._jsonl.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        self._jsonl.flush()

    def finish(self, reason: str) -> None:
        elapsed = min(self._elapsed(), self._duration_sec)
        final_bucket = math.ceil(elapsed) - 1
        while self._next_bucket <= final_bucket:
            self._write_bucket(self._next_bucket)
            self._next_bucket += 1
        summary = {
            "record_type": "summary",
            "reason": reason,
            "duration_sec": round(elapsed, 3),
            "totals": {name: self._totals[name] for name in TOPICS},
        }
        self._jsonl.write(json.dumps(summary, separators=(",", ":"), allow_nan=False) + "\n")
        self._jsonl.flush()
        self._jsonl.close()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        required=True,
        help="output directory; perception_probe.jsonl must not already exist there",
    )
    parser.add_argument(
        "--duration-sec",
        type=duration_seconds,
        default=300.0,
        help="capture duration in seconds, greater than 0 and no more than 3600 (default: 300)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    interrupted = False

    def request_interruption(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, request_interruption)
    node: HilPerceptionProbe | None = None
    reason = "duration_complete"
    try:
        rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
        node = HilPerceptionProbe(args.artifact_dir, args.duration_sec)
        deadline = time.monotonic() + args.duration_sec
        while rclpy.ok():
            if interrupted:
                reason = "interrupted"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            rclpy.spin_once(node, timeout_sec=min(0.2, remaining))
            if interrupted:
                reason = "interrupted"
                break
            node.flush_closed_buckets()
        if interrupted:
            reason = "interrupted"
        elif time.monotonic() < deadline and not rclpy.ok():
            reason = "external_shutdown"
    except KeyboardInterrupt:
        reason = "interrupted"
    except ExternalShutdownException:
        reason = "external_shutdown"
    except Exception:
        reason = "error"
        raise
    finally:
        try:
            if node is not None:
                try:
                    node.finish(reason)
                finally:
                    node.destroy_node()
        finally:
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            finally:
                signal.signal(signal.SIGINT, previous_sigint_handler)
    if node is not None:
        print(f"Perception probe artifacts: {node._jsonl_path.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
