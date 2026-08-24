#!/usr/bin/env python3
"""Inject one bounded producer stall into an active maneuver reference stream."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import time

import rclpy
from rclpy.node import Node

from iii_drone_interfaces.msg import ManeuverReferenceStream


def find_controller_pid() -> int:
    executable = "/iii_drone_core/maneuver_controller"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if executable in command and "--ros-args" in command:
            return int(entry.name)
    raise RuntimeError("maneuver_controller process not found")


class StreamStallInjector(Node):
    def __init__(self, provider: str, samples: int) -> None:
        super().__init__("reference_stream_stall_injector")
        self.provider = provider
        self.samples = samples
        self.match_count = 0
        self.triggered = False
        self.stream_id = ""
        self.sequence = 0
        self.create_subscription(
            ManeuverReferenceStream,
            "/control/maneuver_controller/reference_stream",
            self.on_reference,
            rclpy.qos.QoSProfile(
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=rclpy.qos.ReliabilityPolicy.BEST_EFFORT,
                durability=rclpy.qos.DurabilityPolicy.VOLATILE,
            ),
        )

    def on_reference(self, message: ManeuverReferenceStream) -> None:
        if (
            message.provider != self.provider
            or message.state != ManeuverReferenceStream.STATE_ACTIVE
            or not message.is_valid
        ):
            self.match_count = 0
            self.stream_id = ""
            return
        if self.stream_id != message.stream_id:
            self.stream_id = message.stream_id
            self.match_count = 0
        self.match_count += 1
        self.sequence = message.sequence
        self.triggered = self.match_count >= self.samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SIGSTOP the maneuver controller during a selected active reference stream."
    )
    parser.add_argument("--provider", default="fly_to_position")
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--stall-sec", type=float, default=0.8)
    parser.add_argument("--timeout-sec", type=float, default=30.0)
    parser.add_argument("--pid", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.samples < 1 or args.stall_sec <= 0.0 or args.timeout_sec <= 0.0:
        raise SystemExit("samples, stall-sec, and timeout-sec must be positive")

    rclpy.init()
    node = StreamStallInjector(args.provider, args.samples)
    deadline = time.monotonic() + args.timeout_sec
    try:
        while rclpy.ok() and not node.triggered and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if not node.triggered:
            print(f"no active {args.provider!r} stream observed before timeout", flush=True)
            return 1

        pid = args.pid or find_controller_pid()
        print(
            f"stopping pid={pid} stream={node.stream_id} sequence={node.sequence}",
            flush=True,
        )
        os.kill(pid, signal.SIGSTOP)
        try:
            time.sleep(args.stall_sec)
        finally:
            os.kill(pid, signal.SIGCONT)
        print(f"resumed pid={pid} after {args.stall_sec:.3f}s", flush=True)
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
