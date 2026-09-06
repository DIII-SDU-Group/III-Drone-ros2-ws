#!/usr/bin/env python3
"""Resolve or apply a simulation-only aircraft fixture.

This is deliberately a setup-only helper. It uses Gazebo truth to map a test
fixture into the current estimator frame, then emits coordinates consumed by
the E2E runner, or applies the fixture directly through Gazebo for test setup.
Mission planning never consumes this data.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys


MARKER = "III_FIXTURE_RESULT="


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--container-workspace", default="/home/iii/ws")
    parser.add_argument("--profile", choices=("sim", "hil"), default="sim")
    parser.add_argument("--apply", action="store_true", help="Set the Gazebo aircraft pose to the fixture pose.")
    parser.add_argument(
        "--hold-duration-s",
        type=float,
        default=0.0,
        help="When applying, keep the fixture pose pinned for this setup-only estimator dwell.",
    )
    parser.add_argument("--hold-rate-hz", type=float, default=10.0)
    return parser.parse_args()


def discover_container(workspace: Path) -> str:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=devcontainer.local_folder={workspace}",
            "--format",
            "{{.ID}}",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(containers) != 1:
        raise RuntimeError(f"expected one workspace devcontainer, found {len(containers)}")
    return containers[0]


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).resolve()
    if args.hold_duration_s < 0 or args.hold_rate_hz <= 0:
        raise ValueError("fixture hold duration must be non-negative and rate must be positive")
    if args.hold_duration_s and not args.apply:
        raise ValueError("--hold-duration-s requires --apply")
    container = discover_container(workspace)
    operation = "apply_fixture_pose" if args.apply else "resolve_fixture_target"
    snippet = f"""
import json
import time
from iii_drone_mcp.agent_tools import DroneAgentTools
tools = DroneAgentTools()
try:
    deadline = time.monotonic() + {args.hold_duration_s!r}
    applications = 0
    while True:
        result = tools.{operation}({args.fixture_id!r})
        applications += 1
        if not result.success or time.monotonic() >= deadline:
            break
        time.sleep({1.0 / args.hold_rate_hz!r})
    data = dict(result.data or {{}})
    data['fixture_applications'] = applications
    data['hold_duration_s'] = {args.hold_duration_s!r}
    print({MARKER!r} + json.dumps({{
        'success': bool(result.success),
        'message': result.message,
        'data': data,
    }}, default=str))
finally:
    tools.close()
"""
    encoded_snippet = base64.b64encode(snippet.encode("utf-8")).decode("ascii")
    runtime_environment = ""
    if args.profile == "hil":
        workstation_address = os.environ.get("III_HIL_WORKSTATION_ADDRESS", "10.42.0.1")
        pi_address = os.environ.get("III_HIL_PI_ADDRESS", "10.42.0.15")
        ros_domain_id = os.environ.get("III_HIL_ROS_DOMAIN_ID", "42")
        gz_partition = os.environ.get("III_HIL_GZ_PARTITION", "iii_hil_0")
        cyclone_uri = (
            "<CycloneDDS><Domain><General><Interfaces>"
            f'<NetworkInterface address="{workstation_address}" priority="default" multicast="default"/>'
            "</Interfaces></General><Discovery><Peers>"
            f'<Peer address="{pi_address}"/>'
            "</Peers></Discovery></Domain></CycloneDDS>"
        )
        runtime_environment = (
            f"export ROS_DOMAIN_ID={ros_domain_id} ROS_LOCALHOST_ONLY=0 "
            "ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET ROS2CLI_DISABLE_DAEMON=1 "
            "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp III_DRONE_MCP_KEEP_RMW=1 "
            f"GZ_PARTITION={gz_partition!r} CYCLONEDDS_URI={cyclone_uri!r} && "
        )
    shell = (
        "source /opt/ros/jazzy/setup.bash && "
        f"cd {args.container_workspace} && "
        "source setup/setup_dev.bash >/dev/null 2>&1 && "
        f"{runtime_environment}"
        f"PYTHONPATH={args.container_workspace}/tools/III-Drone-MCP:$PYTHONPATH "
        "python3 -c \"import base64; exec(base64.b64decode('"
        + encoded_snippet
        + "').decode('utf-8'))\""
    )
    result = subprocess.run(
        ["docker", "exec", "--user", "iii", container, "bash", "-lc", shell],
        text=True,
        capture_output=True,
        check=False,
        timeout=max(60.0, args.hold_duration_s + 15.0),
    )
    marked = [line[len(MARKER) :] for line in result.stdout.splitlines() if line.startswith(MARKER)]
    if result.returncode != 0 or not marked:
        detail = (result.stdout + result.stderr).strip()
        print(json.dumps({"success": False, "message": detail or "fixture resolver failed", "data": {}}))
        return 1
    print(marked[-1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"success": False, "message": str(exc), "data": {}}))
        raise SystemExit(1)
