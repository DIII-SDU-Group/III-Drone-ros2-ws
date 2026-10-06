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

import rclpy.executors

from iii_drone_powerline_slam import realtime
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
def source_time_ns(key, message, clock):
    return header_ns(message) if key == "camera" else 0
''',
    "iii_r1_pipeline": '''
import json
import time
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
    built = True
class IncrementalPipeline:
    def __init__(self, cfg, *, clock, streams_dir, prefetch_workers=0, doppler_workers=0):
        if cfg.get("fail_epoch"):
            raise RuntimeError("stand-in epoch failure")
        self.core, self.absent, self.L, self.clock = _Core(), set(), None, clock
        self.received, self.rejected = Counter(), Counter()
        self.last = {key: None for key in RUNTIME_KEYS}
        self.pushed, self.pending = [], []
        self.main = {"camera": self.pending}
        self.slow_s = float(cfg.get("slow_s", 0.0))
        self.hold_until_close = bool(cfg.get("hold_until_close"))
        self.finalize_s = float(cfg.get("finalize_s", 0.0))
        self.processed_through, self.closed = -1, False
    def _ready(self, t):
        return not self.hold_until_close
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
        if self.hold_until_close and not self.closed:
            return 0
        while self.pending and (max_groups is None or done < max_groups):
            time.sleep(self.slow_s)
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
        time.sleep(self.finalize_s)
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


# stand-ins of the real-time pipeline and of its live-prior variant (node.mission_prior: live)
FAKE_MODULES["iii_rt_pipeline"] = '''
import iii_r1_pipeline as pl
class CameraOptions:
    @classmethod
    def from_node_config(cls, node):
        return cls()
class RealtimePipeline(pl.IncrementalPipeline):
    def __init__(self, cfg, *, clock, streams_dir, camera=None, doppler_workers=0, evidence=True):
        super().__init__(cfg, clock=clock, streams_dir=streams_dir)
    def wait_ready(self, timeout=None):
        return {}
    def push(self, key, message, index, raw=None):
        super().push(key, message, index)
def realtime_frame_converter(cls):
    return cls
'''
FAKE_MODULES["iii_ol_pipeline"] = '''
import iii_rt_pipeline as rtp
class OnlinePipeline(rtp.RealtimePipeline):
    def __init__(self, cfg, **kwargs):
        super().__init__(cfg, **kwargs)
        self.live_priors = [] if cfg.get("mission_sidecar") == "live-contract" else None
        self.commands = []
    def command(self, document):
        if document.get("kind") not in ("intent", "accepted", "end"):
            return {"refused": "unknown kind"}
        self.commands.append(document)
        return {"kind": document["kind"]}
    def prior_summary(self):
        return {"commands": {"applied": len(self.commands)}, "causality": {}, "priors": [], "log": []}
'''


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    tools = tmp_path / "checkout" / "corridor_simulation" / "tools"
    tools.mkdir(parents=True)
    for name, text in FAKE_MODULES.items():
        (tools / f"{name}.py").write_text(text)
    monkeypatch.setenv("POWERLINE_SLAM_CHECKOUT", str(tmp_path / "checkout"))
    return tools


def _config(tmp_path, node=None, **extra):
    config = tmp_path / f"runtime_config_{len(list(tmp_path.glob('runtime_config_*.json')))}.json"
    config.write_text(json.dumps({"schema": "stand-in/v1", **extra,
                                  "node": {"output_dir": str(tmp_path / "out"), "ledger": True, "flush_timeout_s": 60,
                                           **(node or {})}}))
    return config


OVERLOAD = {"contract": "REALTIME_OVERLOAD_v1", "live_age_budget_s": 0.2, "startup_timeout_s": 5.0,
            "max_unprocessed_events": 100, "max_node_queue": 3}


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


# ------------------------------------------------------------------------------------------- real-time operation
def test_waitset_intake_reads_the_same_subscriptions_without_the_executor(node, tmp_path):
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(_config(tmp_path, node={"intake": "waitset"})))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert node._inputs == [] and list(node.subscriptions) == [] and len(node._intake_entities) == 8
    assert node.count_subscribers("/sensor/cable_camera/image_raw") == 1        # the same graph entry as before
    talker = rclpy.create_node("stand_in_camera")
    qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
                     history=HistoryPolicy.KEEP_LAST, depth=10)
    publisher = talker.create_publisher(Image, "/sensor/cable_camera/image_raw", qos)
    try:
        assert _wait(lambda: publisher.get_subscription_count() == 1)
        for sec in (1, 2, 3):
            message = Image()
            message.header.stamp.sec = sec
            publisher.publish(message)
        assert _wait(lambda: _counter(node, "frames") == 3)                    # nothing spins this node's executor
        success, summary = _flush(node)
        report = json.loads((next((tmp_path / "out").glob("epoch001_*/replay")) / "raw_replay_measurements.json").read_text())
        assert success and report["pushed"] == [["camera", 0], ["camera", 1], ["camera", 2]]
    finally:
        talker.destroy_node()
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._intake_thread is None and node._intake_entities == []
    assert node.count_subscribers("/sensor/cable_camera/image_raw") == 0


def test_a_stale_frame_fails_closed_and_only_a_new_epoch_recovers(node, tmp_path):
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER,
                                   value=str(_config(tmp_path, node={"overload": OVERLOAD}, slow_s=0.5)))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(3))                                         # a frame with lines, 0.5 s in processing
    assert _wait(lambda: node._overload is not None)
    assert node._overload["fail_closed_reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
    assert node._overload["trigger"]["has_lines"] and not node._overload_by_node and node._state == "FailClosed"
    node._enqueue("camera", _image(4))                                         # the epoch is dead: counted, not forwarded
    assert node._overload_counters["messages_dropped_after_overload"] == 1
    assert _wait(lambda: (node._last_runtime or {}).get("fail_closed_reason") == "REALTIME_OVERLOAD_v1:OUTPUT_AGE")
    assert _counter(node, "frames") == 0 and node._last_runtime["overload"]["code"] == "OUTPUT_AGE"
    success, message = _flush(node)
    assert not success and message["fail_closed_reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._host.is_alive()                                               # the host answered: it is kept
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert node._overload is None and node._state == "Waiting"
    node._enqueue("camera", _image(1))                                         # an empty start-up frame may be late
    assert _wait(lambda: (node._last_runtime or {}).get("epoch") == 2 and _counter(node, "frames") == 1)
    assert node._overload is None and node._last_runtime["overload"]["frames"]["startup_stale_empty"] == 1


def test_a_host_that_does_not_read_trips_the_node_queue_bound_and_is_replaced(node, tmp_path):
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(_config(tmp_path, node={"overload": OVERLOAD})))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    stuck = node._host
    with node._send_lock:                                                      # the pipe to the host is blocked
        node._enqueue("camera", _image(1))
        assert _wait(lambda: not node._outbox)                                 # the forwarder holds it and waits to send
        for sec in (2, 3, 4, 5):
            node._enqueue("camera", _image(sec))
        assert node._overload is not None and node._overload_by_node
        assert node._overload["fail_closed_reason"] == "REALTIME_OVERLOAD_v1:NODE_QUEUE"
        assert node._overload["trigger"]["node_queue"] == 4 and node._state == "FailClosed"
        assert not node._outbox and node._overload_counters["messages_discarded_at_overload"] == 4
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._host is None and not stuck.is_alive()                         # no state of that host is reused
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert node._host is not None and node._host is not stuck and node._overload is None
    node._enqueue("camera", _image(1))
    assert _wait(lambda: _counter(node, "frames") == 1)


def test_frames_finished_by_the_flush_are_not_bound_by_the_live_age_budget(node, tmp_path):
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER,
                                   value=str(_config(tmp_path, node={"overload": OVERLOAD}, hold_until_close=True)))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(3))                                         # waits for input that never comes
    assert _wait(lambda: _counter(node, "received_camera") == 1)
    time.sleep(3 * OVERLOAD["live_age_budget_s"])
    assert node._overload is None and _counter(node, "frames") == 0            # starved, not overloaded
    success, summary = _flush(node)                                            # the explicit post-traversal step finishes it
    assert success and summary["frames"] == 1
    assert _wait(lambda: _counter(node, "frames") == 1)
    assert node._overload is None and node._state == "Running"


def test_live_mission_prior_commands_reach_the_pipeline_in_arrival_order(node, tmp_path):
    from iii_drone_interfaces.msg import StringStamped
    from iii_drone_powerline_slam.node import COMMAND_QOS
    config = _config(tmp_path, node={"pipeline": "rt", "mission_prior": "live"}, mission_sidecar="live-contract")
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    talker = rclpy.create_node("stand_in_exercise")
    topic = node.resolve_topic_name("nominal_command")          # in the node's namespace, like its outputs
    publisher = talker.create_publisher(StringStamped, topic, COMMAND_QOS)
    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    try:
        for kind in ("intent", "accepted"):                       # latched before the node activates
            publisher.publish(StringStamped(data=json.dumps({"kind": kind, "source_time_ns": 5})))
        assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
        assert node.count_subscribers(topic) == 1
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and _counter(node, "command_applied") < 2:
            executor.spin_once(timeout_sec=0.05)
        assert _counter(node, "command_applied") == 2             # the latched leg command reached the new epoch
        publisher.publish(StringStamped(data=json.dumps({"kind": "truth", "source_time_ns": 6})))
        while time.monotonic() < deadline and _counter(node, "command_refused") < 1:
            executor.spin_once(timeout_sec=0.05)
        assert _counter(node, "command_refused") == 1
        assert node._last_runtime["live_mission_prior"]["commands"] == {"applied": 2}
        assert node._last_runtime["fail_closed_reason"] is None
    finally:
        executor.remove_node(node)
        talker.destroy_node()
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node.count_subscribers(topic) == 0


def test_live_mission_prior_needs_its_contract_and_the_real_time_pipeline(node, tmp_path):
    config = _config(tmp_path, node={"pipeline": "rt", "mission_prior": "live"}, mission_sidecar="a-replay-sidecar")
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.FAILURE       # not a live mission prior contract
    with pytest.raises(ValueError):
        realtime.validate_node_config({"mission_prior": "live"})            # the reference pipeline has no live prior
    with pytest.raises(ValueError):
        realtime.validate_node_config({"pipeline": "rt", "mission_prior": "future"})


def test_worker_cpus_exclude_the_node_and_host_cores():
    """Workers are started from the union of the worker roles' CPUs: never from the host's or the node's core."""
    from iii_drone_powerline_slam import realtime

    plan = {"node": [13], "host": [12, 28], "detector_workers": [20, 21], "mask_worker": [21, 22], "doppler_workers": [27]}
    assert realtime.worker_cpus(plan) == [20, 21, 22, 27]
    assert realtime.worker_cpus({"node": [13], "host": [12, 28]}) == []


# ---------------------------------------------------------------------------------------------- TRAVERSAL_EPOCH_v1
def _command(kind: str, source_time_s: float, **extra) -> bytes:
    from iii_drone_interfaces.msg import StringStamped
    return serialize_message(StringStamped(data=json.dumps({"kind": kind, "source_time_ns": int(source_time_s * 1e9), **extra})))


def _rollover_node(node, tmp_path, timeout_s=60.0, **extra):
    config = _config(tmp_path, node={"pipeline": "rt", "mission_prior": "live",
                                     "rollover": {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": timeout_s,
                                                  "record_dir": str(tmp_path / "epochs")}},
                     mission_sidecar="live-contract", **extra)
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS


def test_a_completed_traversal_rolls_the_epoch_into_a_new_host(node, tmp_path):
    _rollover_node(node, tmp_path, finalize_s=0.5)
    first_host = node._host.pid
    for sec in (3, 4, 5):
        node._enqueue("camera", _image(sec))
    node._enqueue("command", _command("intent", 5.5))
    node._enqueue("command", _command("traversal_complete", 6.0, traversal="01_a_to_b"))
    assert _wait(lambda: node._rolling)
    for sec in (7, 8):                                   # between the epochs: counted, in neither
        node._enqueue("camera", _image(sec))
    assert _wait(lambda: node._rollovers == 1)
    ready = node._last_rollover
    assert ready["phase"] == "ready" and ready["previous_generation"] == 1 and ready["generation"] == 2 == node._epoch_count
    assert ready["host_pid"]["previous"] == first_host != ready["host_pid"]["new"] == node._host.pid
    assert ready["rollover_gap_messages"] == {"camera": 2}
    assert ready["previous_final"]["success"] and ready["previous_final"]["summary"]["frames"] == 3
    assert list(ready["steps_s"]) == ["1_traversal_complete_received", "2_old_epoch_closed_to_new_input", "3_flushed_and_finalized",
                                      "4_final_record_emitted", "5_old_host_destroyed", "6_new_epoch_started"]
    directory = tmp_path / "epochs" / "generation_0001"
    final = json.loads((directory / "traversal_epoch_finalized.json").read_text())
    assert final["event"]["traversal"] == "01_a_to_b" and final["forwarded"] == {"camera": 3, "command": 2}
    product = json.loads((directory / "traversal_product.json").read_text())
    assert product["generation"] == 1 and product["measured_frame_count"] == 3
    assert json.loads((tmp_path / "epochs" / "generation_0002" / "traversal_epoch_ready.json").read_text())["generation"] == 2
    # the new epoch starts from nothing and processes on
    node._enqueue("camera", _image(9))
    assert _wait(lambda: _counter(node, "frames") == 1 and node._last_runtime["epoch"] == 2)
    assert node._last_runtime["counters"].get("received_camera") == 1
    assert node._overload is None and node._state in ("Waiting", "Running")


def test_a_traversal_event_of_an_earlier_traversal_is_ignored(node, tmp_path):
    _rollover_node(node, tmp_path)
    node._enqueue("camera", _image(10))
    node._enqueue("command", _command("traversal_complete", 6.0))          # latched from before this epoch's first input
    assert _wait(lambda: _counter(node, "traversal_complete_stale") == 1)
    assert node._rollovers == 0 and not node._rolling and node._epoch_count == 1


def test_a_rollover_that_takes_too_long_fails_closed_and_a_new_epoch_recovers(node, tmp_path):
    _rollover_node(node, tmp_path, timeout_s=0.5, finalize_s=3.0)
    node._enqueue("camera", _image(3))
    node._enqueue("command", _command("traversal_complete", 4.0))
    assert _wait(lambda: node._overload is not None)
    assert node._overload["fail_closed_reason"] == "TRAVERSAL_EPOCH_v1:ROLLOVER_TIMEOUT" and node._state == "FailClosed"
    assert node._rollovers == 0
    node._enqueue("camera", _image(5))                                       # dead epoch: counted, never forwarded
    assert node._overload_counters["messages_dropped_after_overload"] == 1
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._host is None                                                # nothing of that host is reused
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(6))
    assert _wait(lambda: _counter(node, "frames") == 1)
    assert node._overload is None


def test_a_deactivation_during_a_rollover_abandons_it_cleanly(node, tmp_path):
    _rollover_node(node, tmp_path, finalize_s=2.0)
    node._enqueue("camera", _image(3))
    node._enqueue("command", _command("traversal_complete", 4.0))
    assert _wait(lambda: node._rolling)
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node._host is None and not node._rolling and node._rollovers == 0
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(6))
    assert _wait(lambda: _counter(node, "frames") == 1)
    assert node._overload is None


def test_rollover_configuration_is_checked():
    base = {"pipeline": "rt", "mission_prior": "live"}
    realtime.validate_node_config({**base, "rollover": {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 120.0, "record_dir": None}})
    for bad in ({"contract": "OTHER", "timeout_s": 1.0}, {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 0},
                {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 1.0, "boundary": "truth"}):
        with pytest.raises(ValueError):
            realtime.validate_node_config({**base, "rollover": bad})
    with pytest.raises(ValueError):                                          # the boundary comes with the command stream
        realtime.validate_node_config({"pipeline": "rt", "rollover": {"contract": "TRAVERSAL_EPOCH_v1", "timeout_s": 1.0}})


# --------------------------------------------------------------------------------------------- BOUNDED_RECOVERY_v1
def _recovery(tmp_path, **overrides):
    return {"contract": "BOUNDED_RECOVERY_v1", "max_attempts": 2, "window_s": 600.0, "cooldown_s": 0.1, "backoff": 1.0,
            "state_file": str(tmp_path / "recovery_state.json"), **overrides}


def test_bounded_recovery_starts_fresh_epochs_then_stays_exhausted_until_an_operator_resets(node, tmp_path):
    config = _config(tmp_path, node={"overload": OVERLOAD, "recovery": _recovery(tmp_path)}, slow_s=0.5)
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    hosts = [node._host.pid]
    for attempt in (1, 2):
        node._enqueue("camera", _image(3))                                   # a stale frame with lines: OUTPUT_AGE
        assert _wait(lambda: node._recoveries == attempt)
        assert node._overload is None and node._state == "Waiting" and node._epoch_count == attempt + 1
        assert node._recovery_record["phase"] == "recovered" and node._recovery_record["attempt"] == attempt
        assert node._recovery_record["reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
        assert node._host.pid not in hosts                                   # a new host process each time
        hosts.append(node._host.pid)
        node._enqueue("camera", _image(1))                                   # the fresh epoch processes from nothing
        assert _wait(lambda: (node._last_runtime or {}).get("epoch") == attempt + 1 and _counter(node, "frames") == 1)
    node._enqueue("camera", _image(3))                                       # the third failure: the budget is used up
    assert _wait(lambda: (node._recovery_record or {}).get("phase") == "exhausted")
    assert node._state == "FailClosed" and node._overload["fail_closed_reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
    time.sleep(1.0)
    assert node._recoveries == 2 and node._host.pid == hosts[-1]             # nothing more is attempted
    ledger = json.loads((tmp_path / "recovery_state.json").read_text())
    assert len(ledger["attempts"]) == 2 and ledger["exhausted"]["reason"] == "REALTIME_OVERLOAD_v1:OUTPUT_AGE"
    # a re-activation alone does not help: the node is active, failed closed and starts no epoch
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert node._overload["fail_closed_reason"] == "BOUNDED_RECOVERY_v1:EXHAUSTED" and node._forwarder is None
    # the operator resets the ledger and re-activates
    response = node._on_reset_recovery(Trigger.Request(), Trigger.Response())
    assert response.success and json.loads(response.message)["exhausted"] is None
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("camera", _image(1))
    assert _wait(lambda: _counter(node, "frames") == 1) and node._overload is None


def test_a_respawned_process_counts_as_an_attempt_and_an_exhausted_ledger_blocks_it(node, tmp_path):
    state = tmp_path / "recovery_state.json"
    state.write_text(json.dumps({"attempts": [], "exhausted": None, "active": True, "host_group": None}))
    config = _config(tmp_path, node={"overload": OVERLOAD, "recovery": _recovery(tmp_path)})
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS      # the ledger says the last process died active
    assert node._recovery_record["phase"] == "recovered" and node._recovery_record["reason"] == "PROCESS_RESPAWN"
    assert len(json.loads(state.read_text())["attempts"]) == 1 and node._overload is None
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    assert json.loads(state.read_text())["active"] is False                  # a clean end: the next start is no respawn
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    assert len(json.loads(state.read_text())["attempts"]) == 1
    assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
    # two more respawns within the window: the second one finds the budget used up
    for expected in (2, None):
        ledger = json.loads(state.read_text())
        ledger["active"] = True
        state.write_text(json.dumps(ledger))
        node._recovery = realtime.RecoveryLedger(_recovery(tmp_path))
        assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
        if expected is None:
            assert node._overload["fail_closed_reason"] == "BOUNDED_RECOVERY_v1:EXHAUSTED" and node._forwarder is None
            assert node._recovery_record["phase"] == "exhausted"
        else:
            assert len(json.loads(state.read_text())["attempts"]) == expected and node._overload is None
            node._recovery.state["active"] = True                            # as if this process died too
            node._recovery.save()
            node._end_epoch()
            assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
            ledger = json.loads(state.read_text())
            assert ledger["active"] is False


def test_an_input_contract_violation_is_not_recovered(node, tmp_path):
    config = _config(tmp_path, node={"recovery": _recovery(tmp_path)})
    node.set_parameters([Parameter(RUNTIME_CONFIG_PARAMETER, value=str(config))])
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    node._enqueue("timesync", serialize_message(TimesyncStatus()))
    assert _wait(lambda: (node._recovery_record or {}).get("phase") == "not_attempted")
    assert node._recovery_record["reason"].startswith("UXRCE_DDS_TIMESYNC_ACTIVE") and node._recoveries == 0
    assert node._state == "FailClosed"


def test_recovery_ledger_budget_window_and_backoff(tmp_path):
    now = [1000.0]
    ledger = realtime.RecoveryLedger(_recovery(tmp_path, max_attempts=3, cooldown_s=2.0, backoff=2.0, window_s=100.0), clock=lambda: now[0])
    assert [ledger.next_attempt("REALTIME_OVERLOAD_v1:INPUT_AGE")["delay_s"] for _ in range(3)] == [2.0, 4.0, 8.0]
    now[0] += 101.0                                                          # the window has passed: the budget is back
    assert ledger.next_attempt("PIPELINE_EXCEPTION: lost worker")["number"] == 1
    for _ in range(2):
        ledger.next_attempt("HOST_LOST")
    assert ledger.next_attempt("HOST_LOST") is None and ledger.exhausted["attempts_in_window"] == 3
    now[0] += 1000.0                                                         # exhausted is latched: time does not clear it
    assert realtime.RecoveryLedger(_recovery(tmp_path, max_attempts=3), clock=lambda: now[0]).next_attempt("HOST_LOST") is None
    ledger.reset()
    assert ledger.next_attempt("HOST_LOST")["number"] == 1
    assert not realtime.RecoveryLedger.recoverable("UXRCE_DDS_TIMESYNC_ACTIVE: ...") and not realtime.RecoveryLedger.recoverable("INPUT_CONTRACT: camera")
    with pytest.raises(ValueError):
        realtime.validate_node_config({"recovery": {"contract": "BOUNDED_RECOVERY_v1", "max_attempts": 0, "window_s": 1.0,
                                                    "cooldown_s": 1.0, "backoff": 1.0, "state_file": "x"}})


def test_no_worker_outlives_a_killed_host(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    script = ("import multiprocessing, os, time\n"
              "if __name__ == '__main__':\n"
              "    os.setpgid(0, 0)\n"
              "    p = multiprocessing.get_context('spawn').Process(target=time.sleep, args=(120,))\n"
              "    p.start(); print(p.pid, flush=True); time.sleep(120)\n")
    host = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    worker = int(host.stdout.readline())
    ticks = realtime.process_start_ticks(host.pid)
    os.kill(host.pid, signal.SIGKILL)                                        # the host dies without cleaning up
    host.wait()
    assert os.path.exists(f"/proc/{worker}")                                 # its worker is an orphan now
    assert worker in realtime.kill_process_group(host.pid, ticks)
    assert _wait(lambda: not os.path.exists(f"/proc/{worker}") or open(f"/proc/{worker}/stat").read().split(")")[1].split()[0] == "Z", 10.0)
