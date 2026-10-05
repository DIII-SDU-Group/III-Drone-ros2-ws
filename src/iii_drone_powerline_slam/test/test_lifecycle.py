"""Lifecycle semantics of the powerline_slam node with a stand-in pipeline (no estimator environment needed).

The stand-in modules replace the pinned checkout's ``iii_r1_*`` tools inside the estimator host process: each camera
message becomes one processed frame, so the tests observe intake order, per-epoch state, fail-closed handling,
finalization and the host's lifetime through what the node receives back (runtime records, flush results, written
files).  The estimator itself is covered by the controlled-replay determinism and live11 parity evidence of
WO-2026-10-05-001.
"""

import json
import time

import pytest
import rclpy
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter
from rclpy.serialization import serialize_message

from px4_msgs.msg import SensorCombined, TimesyncStatus
from sensor_msgs.msg import Image
from std_srvs.srv import Trigger

from iii_drone_powerline_slam.node import RUNTIME_CONFIG_PARAMETER, PowerlineSlamNode

FAKE_MODULES = {
    "iii_r1_runtime": '''
def activate():
    return {"runtime": "stand-in"}
''',
    "iii_r1_source": '''
TOPICS = {"radar_u": "/sensor/mmwave/points_full", "radar_f": "/sensor/mmwave_forward/points_full",
          "camera": "/sensor/cable_camera/image_raw", "imu": "/fmu/out/sensor_combined",
          "odometry": "/fmu/out/vehicle_odometry", "local_position": "/fmu/out/vehicle_local_position"}
TYPES = {"radar_u": "sensor_msgs/msg/PointCloud2", "radar_f": "sensor_msgs/msg/PointCloud2", "camera": "sensor_msgs/msg/Image",
         "imu": "px4_msgs/msg/SensorCombined", "odometry": "px4_msgs/msg/VehicleOdometry",
         "local_position": "px4_msgs/msg/VehicleLocalPosition"}
class IIIClockContract:
    contract_id = "stand-in"
def header_ns(message):
    return int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
''',
    "iii_r1_pipeline": '''
import json
from collections import Counter
from pathlib import Path
RUNTIME_KEYS = ("radar_u", "radar_f", "camera", "imu", "odometry", "local_position")
class PipelineConfig:
    @classmethod
    def load(cls, path):
        document = json.loads(Path(path).read_text())
        if document.get("schema") != "stand-in/v1":
            raise ValueError("unknown runtime configuration schema")
        return document
class _Core:
    on_frame = None
class IncrementalPipeline:
    def __init__(self, cfg, *, clock, streams_dir, prefetch_workers=0, doppler_workers=0):
        if cfg.get("fail_epoch"):
            raise RuntimeError("stand-in epoch failure")
        self.core, self.absent, self.L = _Core(), set(), None
        self.received, self.rejected = Counter(), Counter()
        self.last = {key: None for key in RUNTIME_KEYS}
        self.pushed, self.pending = [], []
        self.processed_through, self.closed = -1, False
    def push(self, key, message, index):
        self.received[key] += 1
        self.pushed.append([key, index])
        if key == "camera":
            t = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
            self.last[key] = t
            self.pending.append(t)
    def verify_camera_info(self, message):
        pass
    def advance(self, max_groups=None):
        done = 0
        while self.pending and (max_groups is None or done < max_groups):
            t = self.pending.pop(0)
            self.processed_through = t
            self.core.on_frame({"t": t}, None, None)
            done += 1
        return done
    def backlog(self):
        return {"camera": len(self.pending)}
    def close_input(self):
        self.closed = True
        return self.advance()
    def finalize(self, output=None):
        return {"status": "DEVELOPMENT_REPLAY_MEASURED", "contract_failures": 0, "iii": {"streams_summary": {}},
                "measured_frame_count": sum(1 for key, _ in self.pushed if key == "camera"), "pushed": self.pushed}
    def prefetch_summary(self):
        return None
    def release(self):
        pass
def layer_report(L, streams_info=None):
    return {}, {"jsonl": {}, "gz": {}, "json_gz": {}}
def write_outputs(output, report, jsonl, gz, json_gz):
    output = Path(output)
    output.mkdir(parents=True)
    (output / "raw_replay_measurements.json").write_text(json.dumps(report))
    for name, rows in jsonl.items():
        (output / name).write_text("".join(json.dumps(row) + "\\n" for row in rows))
''',
    "iii_r1_frames": '''
class FrameConverter:
    def __init__(self, pipe):
        self.pipe = pipe
    def frame_record(self, result, step, record):
        t = result["t"]
        reason = "V13_UNINITIALIZED" if t < 2_000_000_000 else None
        lines = [] if reason else [{"id": 1_000_001, "frame_id": "drone", "stamp_ns": t, "position": [0.0, 1.0, 2.0],
                                    "orientation_xyzw": [0.0, 0.0, 0.0, 1.0], "projected_position": [0.0, 1.0, 2.0],
                                    "in_field_of_view": True}]
        return {"t": t, "powerline": {"stamp_ns": t, "lines": lines,
                                      "projection_plane": {"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}},
                "diagnostics": {"t": t, "fail_closed_reason": reason}}
''',
}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    tools = tmp_path / "checkout" / "corridor_simulation" / "tools"
    tools.mkdir(parents=True)
    for name, text in FAKE_MODULES.items():
        (tools / f"{name}.py").write_text(text)
    monkeypatch.setenv("POWERLINE_SLAM_CHECKOUT", str(tmp_path / "checkout"))
    return tools


def _config(tmp_path, **extra):
    config = tmp_path / f"runtime_config_{len(list(tmp_path.glob('runtime_config_*.json')))}.json"
    config.write_text(json.dumps({"schema": "stand-in/v1", **extra,
                                  "node": {"output_dir": str(tmp_path / "out"), "ledger": True, "flush_timeout_s": 60}}))
    return config


@pytest.fixture
def node(checkout, tmp_path):
    rclpy.init()
    instance = PowerlineSlamNode()
    instance.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(_config(tmp_path)))])
    yield instance
    instance._end_epoch()
    instance._stop_host()
    instance.destroy_node()
    rclpy.shutdown()


def _image(sec: int) -> bytes:
    message = Image()
    message.header.stamp.sec = sec
    return serialize_message(message)


def _wait(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _counter(node, name):
    return ((node._last_runtime or {}).get("counters") or {}).get(name, 0)


def _flush(node):
    response = node._on_flush(Trigger.Request(), Trigger.Response())
    return response.success, json.loads(response.message) if response.message.startswith("{") else response.message


def test_configure_fails_without_a_runtime_configuration(checkout):
    rclpy.init()
    try:
        instance = PowerlineSlamNode()
        assert instance.get_parameter(RUNTIME_CONFIG_PARAMETER).value == "none"
        assert instance.trigger_configure() == TransitionCallbackReturn.FAILURE
        assert instance._host is None and instance._powerline_pub is None and instance._flush_service is None
        instance.destroy_node()
    finally:
        rclpy.shutdown()


def test_configure_fails_cleanly_when_the_host_rejects_the_configuration(node, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "other"}))
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(bad))])
    assert node.trigger_configure() == TransitionCallbackReturn.FAILURE
    assert node._host is None and node._powerline_pub is None and node._state_pub is None and node._flush_service is None


def test_configure_starts_the_host_and_outputs_but_no_subscription(node):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node._host is not None and node._host.is_alive()
    assert node._powerline_pub is not None and node._diagnostics_pub is not None and node._flush_service is not None
    assert node._inputs == [] and list(node.subscriptions) == []


def test_activate_processes_in_arrival_order_and_deactivate_ends_the_epoch(node, tmp_path):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert len(node._inputs) == 8 and len(list(node.subscriptions)) == 8
    imu = serialize_message(SensorCombined())
    for sec in (1, 2, 3):
        node._enqueue("imu", imu)
        node._enqueue("camera", _image(sec))
    assert _wait(lambda: _counter(node, "frames") == 3)
    assert node._state == "Running" and node._last_runtime["epoch"] == 1
    success, summary = _flush(node)
    assert success and summary["frames"] == 3
    replay = next((tmp_path / "out").glob("epoch001_*/replay"))
    report = json.loads((replay / "raw_replay_measurements.json").read_text())
    assert report["pushed"] == [["imu", 0], ["camera", 0], ["imu", 1], ["camera", 1], ["imu", 2], ["camera", 2]]
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._inputs == [] and list(node.subscriptions) == [] and node._forwarder is None
    assert node._state == "Idle" and node._host.is_alive()


def test_reactivation_starts_a_fresh_epoch_without_stale_state(node, tmp_path):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(3))
    assert _wait(lambda: _counter(node, "frames") == 1)
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(4))
    assert _wait(lambda: (node._last_runtime or {}).get("epoch") == 2 and _counter(node, "frames") == 1)
    assert _counter(node, "received_camera") == 1
    success, summary = _flush(node)
    report = json.loads((next((tmp_path / "out").glob("epoch002_*/replay")) / "raw_replay_measurements.json").read_text())
    assert success and report["pushed"] == [["camera", 0]]                 # sequence numbers restart with the epoch
    assert not list((tmp_path / "out").glob("epoch001_*/replay"))         # the ended epoch was never finalized


def test_deactivate_reports_discarded_events(node):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(3))
    assert _wait(lambda: _counter(node, "frames") == 1)
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert "discarded_unforwarded_messages_at_epoch_end" in node._last_epoch_end
    assert node._last_epoch_end.get("discarded_inbox_messages_at_epoch_end") == 0


def test_timesync_traffic_fails_closed_and_stops_intake(node):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("timesync", serialize_message(TimesyncStatus()))
    node._enqueue("camera", _image(3))
    assert _wait(lambda: _counter(node, "ignored_after_fail_closed_camera") == 1)
    assert node._state == "FailClosed"
    assert node._last_runtime["fail_closed_reason"].startswith("UXRCE_DDS_TIMESYNC_ACTIVE")
    assert _counter(node, "frames") == 0
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS


def test_flush_and_finalize_runs_once_per_epoch(node, tmp_path):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    for sec in (1, 2, 3):
        node._enqueue("camera", _image(sec))
    success, summary = _flush(node)
    assert success and summary["frames"] == 3
    replay = next((tmp_path / "out").glob("epoch001_*/replay"))
    assert json.loads((replay / "raw_replay_measurements.json").read_text())["iii"]["mode"] == "lifecycle_node"
    ledger = (replay / "frame_ledger.jsonl").read_text().splitlines()
    assert [json.loads(row)["t"] for row in ledger] == [1_000_000_000, 2_000_000_000, 3_000_000_000]
    node._enqueue("camera", _image(9))
    assert _wait(lambda: _counter(node, "ignored_after_finalize_camera") == 1)
    again_success, again = _flush(node)
    assert again_success and again == summary
    assert len(list((tmp_path / "out").glob("epoch001_*/replay"))) == 1
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS


def test_a_failing_epoch_start_fails_activation(node, tmp_path):
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(_config(tmp_path, fail_epoch=True)))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.FAILURE
    assert node._inputs == []


def test_cleanup_and_shutdown_stop_the_host(node):
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    host = node._host
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_cleanup() == TransitionCallbackReturn.SUCCESS
    assert node._host is None and not host.is_alive() and host.exitcode == 0
    assert node._powerline_pub is None and node._flush_service is None
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    host = node._host
    assert node.trigger_shutdown() == TransitionCallbackReturn.SUCCESS
    assert node._host is None and not host.is_alive() and node._inputs == []
