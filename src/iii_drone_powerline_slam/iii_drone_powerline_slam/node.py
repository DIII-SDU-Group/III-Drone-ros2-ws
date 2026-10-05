"""powerline_slam lifecycle node: /perception/powerline_slam/powerline_slam (III_POWERLINE_SLAM_PASSIVE_OUTPUT_v1).

The node only moves data.  Its subscriptions take the III-native inputs as raw serialized messages and number them per
topic; a forwarder thread hands them, in arrival order and without bound, to the estimator host process
(``estimator_host``), which runs the pinned powerline_slam checkout's ``IncrementalPipeline`` -- the code path of the
offline replay -- and returns each processed frame's passive ``Powerline`` output and native diagnostics for
publication.  The estimator never shares this interpreter, so intake keeps up with the sensor rates however long a
processing step takes.

Lifecycle:
* configure  -- read the runtime configuration and start the estimator host, which activates the v13-derived runtime
                and validates the configuration and every path in it; publishers and the flush service; no subscription;
* activate   -- a fresh processing epoch in the host (new pipeline), then subscriptions;
* deactivate -- subscriptions stop; the epoch ends (forwarded but unprocessed events are discarded and counted, the
                pipeline is released), so a later activate never reuses stale state;
* cleanup / shutdown -- the host exits; publishers and the service are released.

``flush_and_finalize`` (std_srvs/Trigger, development/evaluation) closes the input after every forwarded message,
processes every buffered event, runs the single post-traversal finalization and writes the replay report layout to the
configured output directory.
"""
from __future__ import annotations

from collections import Counter, deque
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import Node, State, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from iii_drone_interfaces.msg import Powerline, StringStamped
from std_srvs.srv import Trigger

from . import estimator_host
from .messages import powerline_message, string_stamped

RUNTIME_CONFIG_PARAMETER = "/perception/powerline_slam/runtime_config"
PX4_QOS = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE,
                     history=HistoryPolicy.KEEP_LAST, depth=1000)
SENSOR_QOS = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
                        history=HistoryPolicy.KEEP_LAST, depth=100)
CAMERA_QOS = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
                        history=HistoryPolicy.KEEP_LAST, depth=10)
OUTPUT_QOS = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
                        history=HistoryPolicy.KEEP_LAST, depth=10)
# runtime inputs (III_NATIVE_INPUT_CONTRACT_v1) and contract checks: key -> (topic, ROS type, QoS)
INPUT_TOPICS = {
    "radar_u": ("/sensor/mmwave/points_full", "sensor_msgs/msg/PointCloud2", SENSOR_QOS),
    "radar_f": ("/sensor/mmwave_forward/points_full", "sensor_msgs/msg/PointCloud2", SENSOR_QOS),
    "camera": ("/sensor/cable_camera/image_raw", "sensor_msgs/msg/Image", CAMERA_QOS),
    "imu": ("/fmu/out/sensor_combined", "px4_msgs/msg/SensorCombined", PX4_QOS),
    "odometry": ("/fmu/out/vehicle_odometry", "px4_msgs/msg/VehicleOdometry", PX4_QOS),
    "local_position": ("/fmu/out/vehicle_local_position", "px4_msgs/msg/VehicleLocalPosition", PX4_QOS),
    "camera_info": ("/sensor/cable_camera/camera_info", "sensor_msgs/msg/CameraInfo", SENSOR_QOS),
    "timesync": ("/fmu/out/timesync_status", "px4_msgs/msg/TimesyncStatus", PX4_QOS),
}
HOST_START_TIMEOUT_S = 300.0       # runtime activation, native-module and configuration checks
EPOCH_START_TIMEOUT_S = 300.0      # pipeline construction and worker start-up
EXIT_HOST_LOST = 71                # the estimator host died: exit so supervision respawns a clean node
FORWARD_BATCH = 256


def _tools() -> Path:
    checkout = os.environ.get("POWERLINE_SLAM_CHECKOUT")
    if not checkout:
        raise RuntimeError("POWERLINE_SLAM_CHECKOUT is not set (the estimator environment was not sourced)")
    return Path(checkout) / "corridor_simulation/tools"


class PowerlineSlamNode(Node):
    def __init__(self) -> None:
        super().__init__("powerline_slam")
        self.declare_parameter(RUNTIME_CONFIG_PARAMETER, "none")
        self._lock = threading.Lock()
        self._outbox: deque = deque()               # (key, raw, index, received) and the flush marker, arrival order
        self._outbox_ready = threading.Condition(self._lock)
        self._outbox_high_water = 0
        self._send_lock = threading.Lock()          # the forwarder and lifecycle callbacks share the pipe
        self._sequence: Counter = Counter()
        self._inputs = []
        self._config_path: Path | None = None
        self._node_config: dict = {}
        self._host = None
        self._to_host = None
        self._from_host = None
        self._forwarder: threading.Thread | None = None
        self._receiver: threading.Thread | None = None
        self._forwarding = False
        self._closing = False
        self._replies: dict[str, deque] = {k: deque() for k in ("ready", "epoch_started", "flushed", "epoch_ended")}
        self._reply_ready = threading.Condition()
        self._state = "Idle"
        self._epoch_count = 0
        self._last_runtime: dict | None = None
        self._last_epoch_end: dict = {}
        self._group = MutuallyExclusiveCallbackGroup()
        self._powerline_pub = None
        self._state_pub = None
        self._diagnostics_pub = None
        self._flush_service = None

    # ------------------------------------------------------------------ lifecycle
    def on_configure(self, state: State) -> TransitionCallbackReturn:
        value = str(self.get_parameter(RUNTIME_CONFIG_PARAMETER).value)
        if not value or value == "none":
            self.get_logger().error(f"{RUNTIME_CONFIG_PARAMETER} is not set; the powerline_slam backend cannot be configured")
            return TransitionCallbackReturn.FAILURE
        try:
            self._config_path = Path(value).expanduser().resolve()
            self._node_config = dict(json.loads(self._config_path.read_text()).get("node") or {})
            self._start_host()
            error = self._await("ready", HOST_START_TIMEOUT_S)
            if error is not None:
                raise RuntimeError(f"estimator host: {error}")
            self._powerline_pub = self.create_lifecycle_publisher(Powerline, "powerline", OUTPUT_QOS)
            self._state_pub = self.create_lifecycle_publisher(StringStamped, "state", OUTPUT_QOS)
            self._diagnostics_pub = self.create_lifecycle_publisher(StringStamped, "diagnostics", OUTPUT_QOS)
            self._flush_service = self.create_service(Trigger, "flush_and_finalize", self._on_flush, callback_group=self._group)
        except Exception as exc:  # fail the transition clearly; nothing stays half-built
            self.get_logger().error(f"powerline_slam configure failed: {type(exc).__name__}: {exc}")
            self._stop_host()
            self._release_outputs()
            return TransitionCallbackReturn.FAILURE
        self.get_logger().info(f"powerline_slam configured from {self._config_path}")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        self._epoch_count += 1
        run = f"epoch{self._epoch_count:03d}_{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        with self._lock:
            self._outbox.clear()
            self._outbox_high_water = 0
            self._sequence = Counter()
        try:
            self._send(("start_epoch", self._epoch_count, run))
            error = self._await("epoch_started", EPOCH_START_TIMEOUT_S)
        except (RuntimeError, OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            self.get_logger().error(f"powerline_slam activate failed: {error}")
            return TransitionCallbackReturn.FAILURE
        self._last_runtime = None
        result = super().on_activate(state)
        self._start_forwarder()
        self._create_subscriptions()
        self._set_state("Waiting")
        return result

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        self._set_state("Idle")
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        self._stop_host()
        self._release_outputs()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        self._stop_host()
        self._release_outputs()
        return TransitionCallbackReturn.SUCCESS

    # ------------------------------------------------------------------ estimator host
    def _start_host(self) -> None:
        context = multiprocessing.get_context("spawn")
        host_in, self._to_host = context.Pipe(duplex=False)
        self._from_host, host_out = context.Pipe(duplex=False)
        for kind in self._replies:
            self._replies[kind].clear()
        # not a daemonic process: the host starts its own worker pools.  It exits on its own when this process goes
        # away (end of file on its input pipe), so no estimator state can outlive the node.
        self._host = context.Process(target=estimator_host.main, name="powerline_slam_estimator",
                                     args=(host_in, host_out, str(self._config_path), str(_tools())))
        self._host.start()
        host_in.close()
        host_out.close()
        self._closing = False
        self._receiver = threading.Thread(target=self._receive, name="powerline_slam_receiver", daemon=True)
        self._receiver.start()

    def _stop_host(self) -> None:
        if self._host is None:
            return
        self._closing = True
        try:
            self._send(("exit",))
        except (OSError, ValueError):
            pass
        self._host.join(timeout=60.0)
        if self._host.is_alive():
            self._host.kill()
            self._host.join(timeout=10.0)
        for connection in (self._to_host, self._from_host):
            try:
                connection.close()
            except OSError:
                pass
        if self._receiver is not None:
            self._receiver.join(timeout=10.0)
        self._host = self._to_host = self._from_host = self._receiver = None

    def _send(self, item) -> None:
        with self._send_lock:
            self._to_host.send(item)

    def _await(self, kind: str, timeout: float):
        deadline = time.monotonic() + timeout
        with self._reply_ready:
            while not self._replies[kind]:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._host is None or not self._host.is_alive():
                    raise RuntimeError(f"no '{kind}' reply from the estimator host")
                self._reply_ready.wait(timeout=min(remaining, 1.0))
            return self._replies[kind].popleft()

    def _receive(self) -> None:
        """Publish what the host returns; route replies to the waiting lifecycle/service callbacks."""
        while True:
            try:
                item = self._from_host.recv()
            except (EOFError, OSError):
                if not self._closing:
                    self.get_logger().fatal("powerline_slam estimator host died; exiting so supervision respawns a clean node")
                    os._exit(EXIT_HOST_LOST)
                return
            kind = item[0]
            if kind == "frame":
                _, t, powerline, diagnostics = item
                if self._powerline_pub is not None:
                    self._powerline_pub.publish(powerline_message(powerline))
                    self._diagnostics_pub.publish(string_stamped(t, diagnostics))
            elif kind == "state":
                self._set_state(item[1])
            elif kind == "runtime":
                record = dict(item[1], forwarder_queue=len(self._outbox), forwarder_high_water=self._outbox_high_water)
                self._last_runtime = record
                self._publish_state()
                if self._diagnostics_pub is not None:
                    self._diagnostics_pub.publish(string_stamped(max(record.get("processed_through_ns") or 0, 0), record))
            elif kind == "log":
                getattr(self.get_logger(), item[1])(item[2])
            elif kind in self._replies:
                with self._reply_ready:
                    self._replies[kind].append(item[1] if len(item) == 2 else tuple(item[1:]))
                    self._reply_ready.notify_all()

    # ------------------------------------------------------------------ inputs
    def _create_subscriptions(self) -> None:
        from rosidl_runtime_py.utilities import get_message
        for key, (topic, type_name, qos) in INPUT_TOPICS.items():
            self._inputs.append(self.create_subscription(
                get_message(type_name), topic, lambda raw, key=key: self._enqueue(key, raw), qos,
                callback_group=self._group, raw=True))

    def _destroy_subscriptions(self) -> None:
        for subscription in self._inputs:
            self.destroy_subscription(subscription)
        self._inputs = []

    def _enqueue(self, key: str, raw: bytes) -> None:
        received = time.monotonic()
        with self._outbox_ready:
            index = self._sequence[key]
            self._sequence[key] += 1
            self._outbox.append((key, raw, index, received))
            self._outbox_high_water = max(self._outbox_high_water, len(self._outbox))
            self._outbox_ready.notify()

    def _start_forwarder(self) -> None:
        self._forwarding = True
        self._forwarder = threading.Thread(target=self._forward, name="powerline_slam_forwarder", daemon=True)
        self._forwarder.start()

    def _forward(self) -> None:
        """Hand arrivals to the host in arrival order, batched; never drops (the queue is unbounded)."""
        while True:
            with self._outbox_ready:
                while self._forwarding and not self._outbox:
                    self._outbox_ready.wait(timeout=0.5)
                if not self._forwarding:
                    return
                batch, flush = [], False
                while self._outbox and len(batch) < FORWARD_BATCH:
                    item = self._outbox.popleft()
                    if item[0] == "__flush__":
                        flush = True
                        break
                    batch.append(item)
            # sending outside the lock: callbacks keep enqueueing while a large batch is written to the pipe
            if batch:
                self._send(("msgs", batch))
            if flush:
                self._send(("flush",))

    def _end_epoch(self) -> None:
        """Stop intake, discard what the host has not processed, release the epoch."""
        self._destroy_subscriptions()
        if self._forwarder is None:
            return
        with self._outbox_ready:
            self._forwarding = False
            unsent = sum(1 for item in self._outbox if item[0] != "__flush__")
            self._outbox.clear()
            self._outbox_ready.notify_all()
        self._forwarder.join(timeout=60.0)
        self._forwarder = None
        if self._host is not None and self._host.is_alive():
            try:
                self._send(("end_epoch",))
                counts = dict(self._await("epoch_ended", 120.0) or {})
            except (RuntimeError, OSError, ValueError) as exc:
                counts = {"error": f"{type(exc).__name__}: {exc}"}
            counts["discarded_unforwarded_messages_at_epoch_end"] = unsent
            self._last_epoch_end = counts
            self.get_logger().info(f"powerline_slam processing epoch {self._epoch_count} ended: {json.dumps(counts)}")

    # ------------------------------------------------------------------ outputs
    def _release_outputs(self) -> None:
        for name in ("_powerline_pub", "_state_pub", "_diagnostics_pub"):
            publisher = getattr(self, name)
            if publisher is not None:
                self.destroy_publisher(publisher)
                setattr(self, name, None)
        if self._flush_service is not None:
            self.destroy_service(self._flush_service)
            self._flush_service = None

    def _set_state(self, value: str) -> None:
        if value != self._state:
            self._state = value
            self._publish_state()

    def _publish_state(self) -> None:
        publisher = self._state_pub
        if publisher is not None:
            publisher.publish(string_stamped(self.get_clock().now().nanoseconds, self._state))

    # ------------------------------------------------------------------ flush and finalize
    def _on_flush(self, request, response):
        if self._forwarder is None or self._host is None:
            response.success = False
            response.message = "not active"
            return response
        with self._outbox_ready:                  # after every message forwarded so far, in order
            self._outbox.append(("__flush__",))
            self._outbox_ready.notify()
        try:
            success, summary = self._await("flushed", float(self._node_config.get("flush_timeout_s", 3600.0)))
        except RuntimeError as exc:
            response.success = False
            response.message = str(exc)
            return response
        response.success = bool(success)
        response.message = json.dumps(summary, default=str)
        return response


def main(args=None) -> int:
    rclpy.init(args=args)
    node = PowerlineSlamNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node._end_epoch()
        node._stop_host()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
