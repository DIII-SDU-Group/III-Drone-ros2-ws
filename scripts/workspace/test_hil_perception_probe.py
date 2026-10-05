"""Focused tests for safe bounds and lightweight image evidence helpers."""

import argparse
import importlib.util
import json
from pathlib import Path
import signal

import numpy as np
import pytest


SCRIPT = Path(__file__).with_name("hil_perception_probe.py")
SPEC = importlib.util.spec_from_file_location("hil_perception_probe", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def test_probe_duration_has_a_finite_one_hour_bound() -> None:
    assert PROBE.duration_seconds("0.25") == pytest.approx(0.25)
    assert PROBE.duration_seconds("3600") == 3600.0
    for invalid in ("0", "-1", "nan", "inf", "3600.1"):
        with pytest.raises(argparse.ArgumentTypeError):
            PROBE.duration_seconds(invalid)


def test_image_metrics_capture_brightness_and_edge_contrast() -> None:
    uniform = np.full((24, 32, 3), 128, dtype=np.uint8)
    metrics = PROBE.image_metrics(uniform)
    assert metrics == {
        "brightness_mean": 128.0,
        "brightness_std": 0.0,
        "edge_contrast": 0.0,
    }

    edge = np.zeros((24, 32, 3), dtype=np.uint8)
    edge[:, 16:, :] = 255
    assert PROBE.image_metrics(edge)["edge_contrast"] > 0.0


def test_non_finite_topic_values_are_json_safe() -> None:
    assert PROBE._json_float(1.25) == 1.25
    assert PROBE._json_float(float("nan")) is None
    assert PROBE._json_float(float("inf")) is None


class _ControlledNode:
    def __init__(self, _artifact_dir: Path, _duration_sec: float) -> None:
        self._jsonl_path = _artifact_dir / "perception_probe.jsonl"
        self._alive_at_finish: bool | None = None
        self._alive_at_destroy: bool | None = None
        self.poll_hook = lambda: None

    def poll(self) -> None:
        self.poll_hook()

    def flush_closed_buckets(self) -> None:
        pass

    def finish(self, reason: str) -> None:
        self._alive_at_finish = PROBE.rclpy.ok()
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_path.write_text(
            json.dumps({"record_type": "summary", "reason": reason}) + "\n",
            encoding="utf-8",
        )

    def destroy_node(self) -> None:
        self._alive_at_destroy = PROBE.rclpy.ok()


def _install_controlled_ros(
    monkeypatch, *, spin_error: BaseException | None, during_spin=None
) -> dict[str, object]:
    state = {
        "alive": False,
        "init_options": None,
        "shutdown_after_destroy": False,
        "spin_count": 0,
    }
    node: _ControlledNode | None = None

    def init(*, args, signal_handler_options=None) -> None:
        assert args is None
        state["init_options"] = signal_handler_options
        state["alive"] = True

    def ok() -> bool:
        return bool(state["alive"])

    def poll() -> None:
        state["spin_count"] = int(state["spin_count"]) + 1
        if during_spin is not None:
            during_spin(state)
        if isinstance(spin_error, PROBE.ExternalShutdownException):
            state["alive"] = False
        if spin_error is not None:
            raise spin_error

    def make_node(artifact_dir: Path, duration_sec: float) -> _ControlledNode:
        nonlocal node
        node = _ControlledNode(artifact_dir, duration_sec)
        node.poll_hook = poll
        return node

    def shutdown() -> None:
        state["shutdown_after_destroy"] = bool(node and node._alive_at_destroy)
        state["alive"] = False

    monkeypatch.setattr(PROBE.rclpy, "init", init)
    monkeypatch.setattr(PROBE.rclpy, "ok", ok)
    monkeypatch.setattr(PROBE.rclpy, "spin_once", lambda *_args, **_kwargs: pytest.fail("the probe polls"))
    monkeypatch.setattr(PROBE.rclpy, "shutdown", shutdown)
    monkeypatch.setattr(PROBE, "HilPerceptionProbe", make_node)
    state["node_ref"] = lambda: node
    return state


def test_main_keeps_ros_alive_through_sigint_finalization(tmp_path, monkeypatch) -> None:
    state = _install_controlled_ros(monkeypatch, spin_error=KeyboardInterrupt())
    old_handler = signal.getsignal(signal.SIGINT)

    assert PROBE.main(["--artifact-dir", str(tmp_path), "--duration-sec", "10"]) == 0

    node = state["node_ref"]()
    assert node._alive_at_finish is True
    assert node._alive_at_destroy is True
    assert state["shutdown_after_destroy"] is True
    assert signal.getsignal(signal.SIGINT) == old_handler
    assert json.loads(node._jsonl_path.read_text(encoding="utf-8")) == {
        "record_type": "summary",
        "reason": "interrupted",
    }
    assert state["init_options"] == PROBE.rclpy.signals.SignalHandlerOptions.NO


def test_main_sigint_during_spin_is_cooperative(tmp_path, monkeypatch) -> None:
    def deliver_sigint_during_work(state: dict[str, object]) -> None:
        state["spin_work_started"] = True
        signal.raise_signal(signal.SIGINT)
        state["spin_work_completed"] = True

    state = _install_controlled_ros(
        monkeypatch, spin_error=None, during_spin=deliver_sigint_during_work
    )
    old_handler = signal.getsignal(signal.SIGINT)

    assert PROBE.main(["--artifact-dir", str(tmp_path), "--duration-sec", "10"]) == 0

    node = state["node_ref"]()
    assert state["spin_count"] == 1
    assert state["spin_work_started"] is True
    assert state["spin_work_completed"] is True
    assert node._alive_at_finish is True
    assert node._alive_at_destroy is True
    assert state["shutdown_after_destroy"] is True
    assert signal.getsignal(signal.SIGINT) == old_handler
    assert json.loads(node._jsonl_path.read_text(encoding="utf-8")) == {
        "record_type": "summary",
        "reason": "interrupted",
    }


def test_main_records_and_propagates_spin_error(tmp_path, monkeypatch) -> None:
    state = _install_controlled_ros(monkeypatch, spin_error=RuntimeError("spin failed"))
    old_handler = signal.getsignal(signal.SIGINT)

    with pytest.raises(RuntimeError, match="spin failed"):
        PROBE.main(["--artifact-dir", str(tmp_path), "--duration-sec", "10"])

    node = state["node_ref"]()
    summary = json.loads(node._jsonl_path.read_text(encoding="utf-8"))
    assert summary["reason"] == "error"
    assert node._alive_at_finish is True
    assert node._alive_at_destroy is True
    assert state["shutdown_after_destroy"] is True
    assert signal.getsignal(signal.SIGINT) == old_handler


def test_main_records_external_ros_shutdown(tmp_path, monkeypatch) -> None:
    state = _install_controlled_ros(
        monkeypatch, spin_error=PROBE.ExternalShutdownException()
    )

    assert PROBE.main(["--artifact-dir", str(tmp_path), "--duration-sec", "10"]) == 0

    node = state["node_ref"]()
    summary = json.loads(node._jsonl_path.read_text(encoding="utf-8"))
    assert summary["reason"] == "external_shutdown"
    assert node._alive_at_finish is False
    assert node._alive_at_destroy is False
    assert state["shutdown_after_destroy"] is False


def test_probe_decodes_the_lossless_compressed_camera_stream(tmp_path) -> None:
    import types

    import cv2
    import numpy as np
    from cv_bridge import CvBridge
    from sensor_msgs.msg import CompressedImage

    # compressed_image_transport's PNG output for an rgb8 camera.
    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
    rgb[:, 32:] = (255, 0, 0)  # red right half
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok
    message = CompressedImage()
    message.format = "rgb8; png compressed bgr8"
    message.data = png.tobytes()

    buckets = {}
    probe = types.SimpleNamespace(
        _record=lambda name, msg, metadata: buckets.setdefault(name, {"metadata": metadata}),
        _elapsed=lambda: 5.0,
        _last_image_saved_at=float("-inf"),
        _bridge=CvBridge(),
        _image_sequence=0,
        _jsonl_path=tmp_path / "probe.jsonl",
    )
    probe_class = next(value for value in vars(PROBE).values()
                       if isinstance(value, type) and hasattr(value, "_on_image"))
    probe_class._on_image(probe, message)

    saved = buckets["camera_image"]["saved_camera"]
    assert "error" not in saved, saved
    frame = cv2.imread(str(tmp_path / saved["file"]))
    assert frame.shape == (48, 64, 3)
    # Decoded as BGR: the red half has a high third channel.
    assert frame[:, 40:, 2].mean() > 200 and frame[:, :24, 2].mean() < 30


def test_probe_takes_queued_messages_without_an_executor(tmp_path) -> None:
    import time

    rclpy = pytest.importorskip("rclpy")
    from nav_msgs.msg import Odometry

    rclpy.init(domain_id=88)
    probe = publisher_node = None
    try:
        probe = PROBE.HilPerceptionProbe(tmp_path, 10.0)
        publisher_node = rclpy.create_node("probe_poll_test_publisher")
        publisher = publisher_node.create_publisher(Odometry, PROBE.TOPICS["gazebo_odometry"], 10)
        deadline = time.monotonic() + 5.0
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        for _ in range(20):
            publisher.publish(Odometry())
            time.sleep(0.002)
        deadline = time.monotonic() + 2.0
        while probe._totals["gazebo_odometry"] < 20 and time.monotonic() < deadline:
            probe.poll()
            time.sleep(PROBE.POLL_PERIOD_SEC)

        # Every queued message reached its callback; nothing spun an executor.
        assert probe._totals["gazebo_odometry"] == 20
        assert probe.executor is None
    finally:
        if probe is not None:
            probe.finish("test")
            probe.destroy_node()
        if publisher_node is not None:
            publisher_node.destroy_node()
        rclpy.shutdown()
