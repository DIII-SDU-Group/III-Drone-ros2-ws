#!/usr/bin/env python3
"""Focused tests for the host development rosbag helper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "scripts" / "workspace" / "iii_rosbag.sh"


def test_stop_accepts_the_requested_start_prefix() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        log = root / "calls.jsonl"
        ros2 = root / "ros2"
        ros2.write_text(
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with Path(os.environ['ROS2_CALL_LOG']).open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if any(value.endswith('/recording_status') for value in sys.argv):
    print("response: recording=True, recording_id='field-test_20260904_084311'")
else:
    print('response: success=True')
""",
            encoding="utf-8",
        )
        ros2.chmod(0o755)
        result = subprocess.run(
            [str(SCRIPT), "stop", "--id", "field-test", "--timeout", "20"],
            cwd=WORKSPACE,
            env={
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "ROS2_CALL_LOG": str(log),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(calls) == 2
        assert "/mission/rosbag_recorder/recording_status" in calls[0]
        assert "/mission/rosbag_recorder/stop_recording" in calls[1]
        assert "recording_id: 'field-test_20260904_084311'" in calls[1][-1]
