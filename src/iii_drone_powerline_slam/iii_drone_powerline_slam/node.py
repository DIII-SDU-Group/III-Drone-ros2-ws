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

Real-time operation (``realtime``; every key of the configuration's ``node`` block is optional):
* ``intake: waitset`` takes the runtime inputs in a dedicated thread with a wait set of its own, several messages per
  wake-up, instead of one executor callback per message.  The subscriptions are the same (node, topics, types, QoS);
* ``affinity`` pins this process, the estimator host and the workers;
* ``overload`` enables REALTIME_OVERLOAD_v1: bounded queues and bounded output age, fail-closed on violation;
* ``rollover`` enables TRAVERSAL_EPOCH_v1: one completed traversal is one estimator epoch.  When the host meets the
  exercise's ``traversal_complete`` event it finalizes the traversal; this node then destroys the host process with
  its workers, starts a new one (generation + 1) and publishes ``traversal_epoch`` / ``ready`` on ``diagnostics``.
  The simulation, PX4 and every other node keep running; a failed or timed-out transaction fails closed;
* ``recovery`` enables BOUNDED_RECOVERY_v1: after a recoverable fail-closed epoch (or a respawn of this process) a
  bounded number of attempts start a new host and a fresh epoch; when the budget is used up the node stays failed
  closed until an operator calls ``reset_recovery`` (std_srvs/Trigger).

The estimator host is the leader of a process group of its own; whenever this node stops, kills or loses a host it ends
what is left of that group, so no worker process outlives its host.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import ExitStack
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

from . import estimator_host, realtime
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
# live nominal mission prior (node.mission_prior: live): the flight exercise's command events, in the node's namespace.
# Latched, so a node activated during a leg learns that leg's command.  Never an estimator measurement.
COMMAND_QOS = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         history=HistoryPolicy.KEEP_LAST, depth=8)
COMMAND_TOPIC = ("nominal_command", "iii_drone_interfaces/msg/StringStamped", COMMAND_QOS)
HOST_START_TIMEOUT_S = 300.0       # runtime activation, native-module and configuration checks
EPOCH_START_TIMEOUT_S = 300.0      # pipeline construction and worker start-up
EXIT_HOST_LOST = 71                # the estimator host died: exit so supervision respawns a clean node
EXIT_INTAKE_LOST = 72              # the wait-set intake thread failed: likewise
FORWARD_BATCH = 256
INTAKE_WAIT_NS = 200_000_000       # wait-set intake: longest wait before the thread re-checks that it should run
FORWARDER_STOP_TIMEOUT_S = 10.0    # a forwarder still blocked in the pipe after this means the host does not read
OVERLOAD_REPEAT_S = 1.0            # the fail-closed overload outputs are repeated at this period
ROLLOVER_ABORT_JOIN_S = 90.0       # a deactivation waits this long for a rollover in progress to give up
EMPTY_PLANE = {"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}


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
        self._replies: dict[str, deque] = {k: deque() for k in ("ready", "epoch_started", "flushed", "epoch_ended",
                                                                 "traversal_flushed")}
        self._reply_ready = threading.Condition()
        self._state = "Idle"
        self._epoch_count = 0
        self._last_runtime: dict | None = None
        self._last_epoch_end: dict = {}
        self._group = MutuallyExclusiveCallbackGroup()
        self._intake_entities: list = []            # wait-set intake: (key, rcl subscription, message type)
        self._intake_guard = None
        self._intake_thread: threading.Thread | None = None
        self._intaking = False
        self._overload_limits: dict | None = None   # REALTIME_OVERLOAD_v1 limits of the configuration, or None
        self._overload: dict | None = None          # the latched overload record of the current epoch
        self._overload_by_node = False
        self._overload_counters: Counter = Counter()
        self._rollover_config: dict | None = None   # TRAVERSAL_EPOCH_v1 settings of the configuration, or None
        self._rolling = False                       # a rollover is in progress: arrivals are counted, not forwarded
        self._rollover_thread: threading.Thread | None = None
        self._rollover_abort = False
        self._rollover_gap: Counter = Counter()
        self._rollovers = 0
        self._last_rollover: dict | None = None
        self._recovery: realtime.RecoveryLedger | None = None   # BOUNDED_RECOVERY_v1 ledger, or None
        self._recovery_thread: threading.Thread | None = None
        self._recovery_record: dict | None = None   # the latest recovery record (repeated while exhausted)
        self._recoveries = 0
        self._host_group: tuple[int, int | None] | None = None  # (process group, start ticks) of the current host
        self._reset_service = None
        self._watchdog: threading.Thread | None = None
        self._watching = False
        self._publish_lock = threading.Lock()
        self._publish_age_max = 0.0
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
            realtime.validate_node_config(self._node_config)
            self._overload_limits = self._node_config.get("overload")
            self._rollover_config = self._node_config.get("rollover")
            self._recovery = realtime.RecoveryLedger(self._node_config["recovery"]) if self._node_config.get("recovery") else None
            self._sweep_host_group()
            pinned = realtime.pin_process(os.getpid(), (self._node_config.get("affinity") or {}).get("node"))
            if pinned:
                self.get_logger().info(f"powerline_slam node process pinned to CPUs {pinned}")
            self._start_host()
            error = self._await("ready", HOST_START_TIMEOUT_S)
            if error is not None:
                raise RuntimeError(f"estimator host: {error}")
            self._powerline_pub = self.create_lifecycle_publisher(Powerline, "powerline", OUTPUT_QOS)
            self._state_pub = self.create_lifecycle_publisher(StringStamped, "state", OUTPUT_QOS)
            self._diagnostics_pub = self.create_lifecycle_publisher(StringStamped, "diagnostics", OUTPUT_QOS)
            self._flush_service = self.create_service(Trigger, "flush_and_finalize", self._on_flush, callback_group=self._group)
            if self._recovery is not None:
                self._reset_service = self.create_service(Trigger, "reset_recovery", self._on_reset_recovery,
                                                          callback_group=self._group)
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
            self._overload = None
            self._overload_by_node = False
            self._overload_counters = Counter()
            self._publish_age_max = 0.0
            self._rolling = False
            self._rollover_abort = False
            self._rollover_gap = Counter()
        self._recovery_record = None
        respawn = None
        if self._recovery is not None:
            # a start while the ledger still says "active" follows a process that ended without deactivating: a respawn
            if self._recovery.exhausted is None and self._recovery.state["active"]:
                respawn = self._recovery.next_attempt("PROCESS_RESPAWN")
            if self._recovery.exhausted is not None:
                # the budget is used up: no epoch is started, the node is active and failed closed until an operator resets
                result = super().on_activate(state)
                self._start_watchdog()
                self._exhausted("PROCESS_RESPAWN" if respawn is None and self._recovery.state["active"] else "EXHAUSTED_BEFORE_START")
                self._latch_overload(self._exhausted_record(), by_node=False)
                return result
            self._recovery.mark_active(True)
        try:
            if self._host is None:                  # the previous epoch ended with an unresponsive host: a new one
                self._start_host()
                error = self._await("ready", HOST_START_TIMEOUT_S)
                if error is not None:
                    raise RuntimeError(f"estimator host: {error}")
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
        self._start_watchdog()
        self._create_subscriptions()
        self._set_state("Waiting")
        if respawn is not None:
            self._recoveries += 1
            self._emit_recovery({"phase": "recovered", "attempt": respawn["number"], "reason": "PROCESS_RESPAWN",
                                 "generation": self._epoch_count, "how": "the process was respawned and re-activated: a new host, a fresh epoch"})
        return result

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        if self._recovery is not None:
            self._recovery.mark_active(False)       # a clean end: the next start is not a respawn
        self._set_state("Idle")
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        if self._recovery is not None:
            self._recovery.mark_active(False)
        self._stop_host()
        self._release_outputs()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        self._end_epoch()
        if self._recovery is not None:
            self._recovery.mark_active(False)
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
        self._host_group = (self._host.pid, realtime.process_start_ticks(self._host.pid))
        if self._recovery is not None:
            self._recovery.set_host_group({"pid": self._host_group[0], "start_ticks": self._host_group[1]})
        self._closing = False
        self._receiver = threading.Thread(target=self._receive, name="powerline_slam_receiver", daemon=True)
        self._receiver.start()

    def _stop_host(self, *, unresponsive: bool = False) -> None:
        if self._host is None:
            return
        self._closing = True
        if unresponsive:                            # it does not read its pipe: asking it to exit would block
            self._host.kill()
        else:
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
        self._end_host_group()

    def _end_host_group(self) -> None:
        """No worker process outlives its host: end what is left of the host's process group."""
        if self._host_group is None:
            return
        group, ticks = self._host_group
        self._host_group = None
        killed = realtime.kill_process_group(group, ticks)
        if killed:
            self.get_logger().warning(f"powerline_slam ended {len(killed)} leftover process(es) of estimator host {group}")
        if self._recovery is not None:
            self._recovery.set_host_group(None)

    def _sweep_host_group(self) -> None:
        """At configuration: end the workers a previous process of this node left behind (recorded in the ledger)."""
        if self._recovery is None or not self._recovery.state.get("host_group"):
            return
        group = self._recovery.state["host_group"]
        killed = realtime.kill_process_group(int(group["pid"]), group.get("start_ticks"))
        if killed:
            self.get_logger().warning(f"powerline_slam ended {len(killed)} process(es) left by an earlier estimator host {group['pid']}")
        self._recovery.set_host_group(None)

    def _send(self, item) -> None:
        with self._send_lock:
            self._to_host.send(item)

    def _await(self, kind: str, timeout: float, *, abortable: bool = False):
        deadline = time.monotonic() + timeout
        with self._reply_ready:
            while not self._replies[kind]:
                remaining = deadline - time.monotonic()
                if abortable and self._rollover_abort:
                    raise RuntimeError("rollover aborted by a lifecycle transition")
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
                    if self._host_group is not None:
                        realtime.kill_process_group(*self._host_group)
                    os._exit(EXIT_HOST_LOST)
                return
            try:
                self._handle(item)
            except Exception as exc:  # noqa: BLE001 - this thread must never die silently: nothing would be published any more
                self._receiver_failed(exc)

    def _log(self, level: str, text: str) -> None:
        """One call site per severity: rclpy refuses a logging call whose severity changes between calls."""
        logger = self.get_logger()
        if level == "error":
            logger.error(text)
        elif level == "warning":
            logger.warning(text)
        elif level == "fatal":
            logger.fatal(text)
        else:
            logger.info(text)

    def _receiver_failed(self, exc: Exception) -> None:
        """An internal error while handling what the host returned: fail closed visibly instead of going silent."""
        record = {"kind": "overload", "contract": "NODE_INTERNAL", "fail_closed_reason": "NODE_INTERNAL:RECEIVER_EXCEPTION",
                  "trigger": {"code": "RECEIVER_EXCEPTION", "error": f"{type(exc).__name__}: {exc}"[:400]}, "limits": {}, "status": None,
                  "recovery": "deactivate and activate (fresh processing epoch) or restart the process"}
        try:
            self._latch_overload(record, by_node=True)
        except Exception:  # noqa: BLE001
            os._exit(EXIT_HOST_LOST)

    def _handle(self, item) -> None:
        kind = item[0]
        if kind == "frame":
            self._publish_frame(*item[1:])
        elif kind == "overload":
            self._latch_overload(item[1], by_node=False)
        elif kind == "state":
            if self._overload is None:          # after an overload the state stays FailClosed for the epoch
                self._set_state(item[1])
        elif kind == "runtime":
            record = dict(item[1], forwarder_queue=len(self._outbox), forwarder_high_water=self._outbox_high_water)
            if self._recovery is not None:
                record["recovery"] = dict(self._recovery.status(), recovered=self._recoveries)
            if self._rollover_config is not None:
                record["rollover"] = {"contract": realtime.ROLLOVER_CONTRACT, "generation": self._epoch_count,
                                      "rolling": self._rolling, "completed": self._rollovers}
            if self._overload_limits is not None:
                latched = None if self._overload is None else self._overload["fail_closed_reason"]
                record["node_overload"] = {"latched": latched, "by_node": self._overload_by_node,
                                           "counters": dict(self._overload_counters),
                                           "publish_age_max_s": round(self._publish_age_max, 3)}
            self._last_runtime = record
            reason = record.get("fail_closed_reason")
            if reason and self._overload is None and self._recovery is not None and not self._rolling:
                # the host failed closed on its own (no overload trigger): latched here too, so recovery can judge it
                self._latch_overload({"kind": "overload", "contract": "HOST_FAIL_CLOSED", "fail_closed_reason": str(reason),
                                      "trigger": {"code": "HOST_FAIL_CLOSED"}, "limits": {}, "status": None,
                                      "recovery": "see BOUNDED_RECOVERY_v1"}, by_node=False)
            self._publish_state()
            if self._diagnostics_pub is not None:
                self._diagnostics_pub.publish(string_stamped(max(record.get("processed_through_ns") or 0, 0), record))
        elif kind == "rollover_begin":
            self._begin_rollover(item[1])
        elif kind == "log":
            self._log(item[1], item[2])
        elif kind in self._replies:
            with self._reply_ready:
                self._replies[kind].append(item[1] if len(item) == 2 else tuple(item[1:]))
                self._reply_ready.notify_all()

    def _publish_frame(self, t, powerline, diagnostics, received=None) -> None:
        """One processed frame.  ``received`` (overload contract) is the node receipt time of its triggering message."""
        if self._overload is not None:
            self._overload_counters["frames_suppressed_after_overload"] += 1
            return
        if received is not None and self._overload_limits is not None:
            age = time.monotonic() - received
            self._publish_age_max = max(self._publish_age_max, age)
            if powerline["lines"] and age > float(self._overload_limits["live_age_budget_s"]):
                # a stale frame with conductor lines is never published, whatever delayed it
                self._latch_overload(self._node_overload_record("NODE_PUBLISH_AGE", publish_age_s=round(age, 3),
                                                                frame_stamp_ns=t), by_node=True)
                return
        with self._publish_lock:
            if self._powerline_pub is not None:
                self._powerline_pub.publish(powerline_message(powerline))
                self._diagnostics_pub.publish(string_stamped(t, diagnostics))

    # ------------------------------------------------------------------ REALTIME_OVERLOAD_v1 (node side)
    def _node_overload_record(self, code: str, **detail) -> dict:
        limits = {k: v for k, v in (self._overload_limits or {}).items() if k != "contract"}
        return {"kind": "overload", "contract": realtime.CONTRACT, "fail_closed_reason": f"{realtime.CONTRACT}:{code}",
                "trigger": {"code": code, **detail}, "limits": limits, "status": None,
                "recovery": "deactivate and activate (fresh processing epoch) or restart the process"}

    def _latch_overload(self, record: dict, *, by_node: bool) -> None:
        """The epoch is dead: stop forwarding, publish the fail-closed outputs, keep them repeated."""
        with self._outbox_ready:
            if self._overload is not None:
                return
            self._overload = record
            self._overload_by_node = by_node
            self._overload_counters["messages_discarded_at_overload"] += sum(1 for item in self._outbox if item[0] != "__flush__")
            self._outbox = deque(item for item in self._outbox if item[0] == "__flush__")
            self._outbox_ready.notify_all()
        self.get_logger().error(f"powerline_slam failed closed: {record['fail_closed_reason']} {json.dumps(record['trigger'])}")
        self._state = "FailClosed"
        self._publish_overload()
        self._consider_recovery(record["fail_closed_reason"])

    def _publish_overload(self) -> None:
        record = self._overload
        if record is None:
            return
        stamp = self.get_clock().now().nanoseconds      # not an estimator output: the node's ROS time
        with self._publish_lock:
            if self._powerline_pub is not None:
                self._powerline_pub.publish(powerline_message({"stamp_ns": stamp, "lines": [], "projection_plane": EMPTY_PLANE}))
                self._diagnostics_pub.publish(string_stamped(stamp, dict(record, node_counters=dict(self._overload_counters))))
                if self._recovery_record is not None and self._recovery_record.get("phase") in ("exhausted", "not_attempted"):
                    self._diagnostics_pub.publish(string_stamped(stamp, self._recovery_record))
        self._publish_state()

    def _start_watchdog(self) -> None:
        if self._overload_limits is None and self._rollover_config is None and self._recovery is None:
            return
        self._watching = True
        self._watchdog = threading.Thread(target=self._watch, name="powerline_slam_watchdog", daemon=True)
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        self._watching = False
        if self._watchdog is not None:
            self._watchdog.join(timeout=5.0)
            self._watchdog = None

    def _watch(self) -> None:
        """Repeat the fail-closed overload outputs so that late subscribers see them too."""
        while self._watching:
            time.sleep(OVERLOAD_REPEAT_S)
            if self._watching and self._overload is not None:
                self._publish_overload()

    # ------------------------------------------------------------------ BOUNDED_RECOVERY_v1
    def _emit_recovery(self, fields: dict) -> None:
        record = {"kind": "recovery", "contract": realtime.RECOVERY_CONTRACT, **fields, "ledger": self._recovery.status()}
        self._recovery_record = record
        with self._publish_lock:
            if self._diagnostics_pub is not None:
                self._diagnostics_pub.publish(string_stamped(self.get_clock().now().nanoseconds, record))
        text = f"powerline_slam recovery: {json.dumps(fields, default=str)[:400]}"
        if fields.get("phase") in ("exhausted", "failed"):
            self.get_logger().error(text)
        else:
            self.get_logger().warning(text)

    def _exhausted_record(self) -> dict:
        return {"kind": "overload", "contract": realtime.RECOVERY_CONTRACT,
                "fail_closed_reason": f"{realtime.RECOVERY_CONTRACT}:EXHAUSTED", "trigger": {"code": "EXHAUSTED", **self._recovery.status()},
                "limits": {"max_attempts": self._recovery.max_attempts, "window_s": self._recovery.window}, "status": None,
                "recovery": "none until an operator calls reset_recovery and re-activates the node"}

    def _exhausted(self, reason: str) -> None:
        self._emit_recovery({"phase": "exhausted", "fail_closed_reason": f"{realtime.RECOVERY_CONTRACT}:EXHAUSTED",
                             "last_reason": reason, "generation": self._epoch_count,
                             "until": "an operator calls reset_recovery and re-activates the node"})

    def _consider_recovery(self, reason: str) -> None:
        """An epoch failed closed: start a bounded recovery attempt if the policy permits one."""
        ledger = self._recovery
        if ledger is None or self._rollover_abort or str(reason).startswith(realtime.RECOVERY_CONTRACT):
            return
        if self._recovery_thread is not None and self._recovery_thread.is_alive() \
                and self._recovery_thread is not threading.current_thread():
            return
        if not ledger.recoverable(reason):
            self._emit_recovery({"phase": "not_attempted", "reason": reason, "why": "not a recoverable condition",
                                 "generation": self._epoch_count})
            return
        attempt = ledger.next_attempt(reason)
        if attempt is None:
            self._exhausted(reason)
            return
        self._recovery_thread = threading.Thread(target=self._recover, args=(attempt, reason),
                                                 name="powerline_slam_recovery", daemon=True)
        self._recovery_thread.start()

    def _recover(self, attempt: dict, reason: str) -> None:
        began = time.monotonic()
        failed = self._epoch_count
        base = {"attempt": attempt["number"], "max_attempts": self._recovery.max_attempts, "reason": reason,
                "failed_generation": failed, "delay_s": attempt["delay_s"]}
        self._emit_recovery({"phase": "scheduled", **base})
        try:
            while time.monotonic() - began < attempt["delay_s"]:      # cool-down with back-off
                if self._rollover_abort:
                    return
                time.sleep(0.05)
            try:
                self._halt_forwarder()
            except RuntimeError:
                pass                                    # blocked in the pipe: the host is killed next
            self._stop_host(unresponsive=True)          # nothing of the failed epoch is reused
            deadline = time.monotonic() + HOST_START_TIMEOUT_S

            def left() -> float:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("the recovery attempt exceeded its time budget")
                return remaining

            run = self._fresh_epoch(left, clear_fail_closed=True)
            self._recoveries += 1
            self._set_state("Waiting")
            self._emit_recovery({"phase": "recovered", **base, "generation": self._epoch_count, "run": run,
                                 "host_pid": self._host.pid, "seconds": round(time.monotonic() - began, 3)})
        except Exception as exc:  # noqa: BLE001 - a failed attempt is an attempt; the next one is judged by the ledger
            if self._rollover_abort:
                return
            self._emit_recovery({"phase": "failed", **base, "error": f"{type(exc).__name__}: {exc}"[:400]})
            self._consider_recovery(f"RECOVERY_FAILED: {type(exc).__name__}")

    def _on_reset_recovery(self, request, response):
        """Operator action: forget the attempts and the exhausted state (the node is then re-activated by the operator)."""
        self._recovery.reset()
        self._recovery_record = None
        response.success = True
        response.message = json.dumps(self._recovery.status())
        self.get_logger().warning("powerline_slam recovery ledger reset by an operator")
        return response

    def _fresh_epoch(self, left, *, clear_fail_closed: bool = False) -> str:
        """A new host process and a new epoch (generation + 1) that starts from nothing."""
        self._start_host()
        error = self._await("ready", left(), abortable=True)
        if error is not None:
            raise RuntimeError(f"estimator host: {error}")
        with self._outbox_ready:
            self._epoch_count += 1
            self._sequence = Counter()
            self._outbox.clear()
            self._outbox_high_water = 0
            self._publish_age_max = 0.0
        run = f"epoch{self._epoch_count:03d}_{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        self._send(("start_epoch", self._epoch_count, run))
        error = self._await("epoch_started", left(), abortable=True)
        if error is not None:
            raise RuntimeError(f"epoch start: {error}")
        self._last_runtime = None
        if clear_fail_closed:
            with self._outbox_ready:
                self._overload = None
                self._overload_by_node = False
                self._overload_counters = Counter()
        self._start_forwarder()
        return run

    # ------------------------------------------------------------------ TRAVERSAL_EPOCH_v1
    def _begin_rollover(self, event: dict) -> None:
        """The host met the traversal_complete event and is finalizing: run the rest of the transaction."""
        if self._rollover_config is None or (self._rollover_thread is not None and self._rollover_thread.is_alive()):
            return
        self._rollover_abort = False
        self._rollover_thread = threading.Thread(target=self._rollover, args=(event, time.monotonic()),
                                                 name="powerline_slam_rollover", daemon=True)
        self._rollover_thread.start()

    def _emit_rollover(self, record: dict) -> None:
        """Persist (when a record directory is configured) and publish one traversal_epoch record."""
        directory = self._rollover_config.get("record_dir")
        if directory:
            try:
                path = Path(directory).expanduser() / f"generation_{record['generation']:04d}"
                path.mkdir(parents=True, exist_ok=True)
                (path / f"traversal_epoch_{record['phase']}.json").write_text(json.dumps(record, indent=1, default=str) + "\n")
            except OSError as exc:
                self.get_logger().error(f"powerline_slam traversal record not written: {exc}")
        with self._publish_lock:
            if self._diagnostics_pub is not None:
                self._diagnostics_pub.publish(string_stamped(self.get_clock().now().nanoseconds, record))

    def _rollover(self, event: dict, began: float) -> None:
        config = self._rollover_config
        deadline = began + float(config["timeout_s"])
        old = self._epoch_count
        record = {"kind": "traversal_epoch", "contract": realtime.ROLLOVER_CONTRACT, "phase": "finalized", "generation": old,
                  "event": event, "timeout_s": float(config["timeout_s"]), "steps_s": {}}

        def mark(step: str) -> None:
            record["steps_s"][step] = round(time.monotonic() - began, 3)

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the transaction exceeded its time budget")
            return remaining

        try:
            mark("1_traversal_complete_received")
            with self._outbox_ready:                    # 2. nothing more enters the old epoch
                self._rolling = True
                self._rollover_gap = Counter(item[0] for item in self._outbox if item[0] != "__flush__")
                for key, count in self._rollover_gap.items():
                    self._sequence[key] -= count
                self._outbox.clear()
            self._set_state("Rollover")
            mark("2_old_epoch_closed_to_new_input")
            success, summary = self._await("traversal_flushed", left(), abortable=True)       # 3. finalized
            mark("3_flushed_and_finalized")
            record["final"] = {"success": bool(success), "summary": summary}
            runtime = self._last_runtime or {}
            record["final_runtime"] = {key: runtime.get(key) for key in ("epoch", "state", "counters", "accounting", "backlog", "pid",
                                                                           "latency_ms_armed", "processed_through_ns", "traversal")}
            record["forwarded"] = dict(self._sequence)
            if not success:
                raise RuntimeError(f"finalization failed: {json.dumps(summary, default=str)[:400]}")
            self._emit_rollover(record)                 # 4. the traversal's final record
            mark("4_final_record_emitted")
            self._halt_forwarder()                      # 5. the old epoch, its host process and its workers go
            self._send(("end_epoch",))
            record["old_epoch_end"] = dict(self._await("epoch_ended", min(left(), 120.0), abortable=True) or {})
            old_pid = None if self._host is None else self._host.pid
            self._stop_host()
            mark("5_old_host_destroyed")
            if self._rollover_abort:
                raise RuntimeError("rollover aborted by a lifecycle transition")
            run = self._fresh_epoch(left)               # 6. generation + 1: a new host, a fresh pipeline
            with self._outbox_ready:
                gap = dict(self._rollover_gap)
                self._rolling = False
            mark("6_new_epoch_started")
            self._rollovers += 1
            ready = {"kind": "traversal_epoch", "contract": realtime.ROLLOVER_CONTRACT, "phase": "ready",
                     "generation": self._epoch_count, "previous_generation": old, "run": run,
                     "host_pid": {"previous": old_pid, "new": self._host.pid}, "rollover_gap_messages": gap,
                     "steps_s": record["steps_s"], "event": event, "rollovers_completed": self._rollovers,
                     "previous_final": record["final"], "previous_epoch_end": record["old_epoch_end"]}
            self._last_rollover = ready
            self._set_state("Waiting")
            self._emit_rollover(ready)                  # 7. epoch-ready
            self.get_logger().info(f"powerline_slam traversal epoch {old} -> {self._epoch_count} in "
                                   f"{time.monotonic() - began:.1f} s (gap {json.dumps(gap)})")
        except Exception as exc:  # noqa: BLE001 - every failure of the transaction fails closed
            if self._rollover_abort:
                return                                  # a lifecycle transition took over; it cleans up
            code = "ROLLOVER_TIMEOUT" if isinstance(exc, TimeoutError) or time.monotonic() >= deadline else "ROLLOVER_FAILED"
            failure = {"kind": "overload", "contract": realtime.ROLLOVER_CONTRACT,
                       "fail_closed_reason": f"{realtime.ROLLOVER_CONTRACT}:{code}",
                       "trigger": {"code": code, "generation": old, "error": f"{type(exc).__name__}: {exc}"[:600],
                                   "steps_s": record["steps_s"]},
                       "limits": {"timeout_s": float(config["timeout_s"])}, "status": None,
                       "recovery": "deactivate and activate (fresh processing epoch) or restart the process"}
            with self._outbox_ready:
                self._rolling = False
            self._latch_overload(failure, by_node=True)

    def _halt_forwarder(self) -> None:
        if self._forwarder is None:
            return
        with self._outbox_ready:
            self._forwarding = False
            self._outbox_ready.notify_all()
        self._forwarder.join(timeout=FORWARDER_STOP_TIMEOUT_S)
        blocked = self._forwarder.is_alive()
        self._forwarder = None
        if blocked:
            raise RuntimeError("the forwarder is blocked: the estimator host does not read")

    # ------------------------------------------------------------------ inputs
    def _input_topics(self) -> dict:
        """The runtime inputs, plus the command events of the flight exercise when the mission prior is live."""
        if self._node_config.get("mission_prior", "sidecar") != "live":
            return INPUT_TOPICS
        return {**INPUT_TOPICS, "command": COMMAND_TOPIC}

    def _create_subscriptions(self) -> None:
        from rosidl_runtime_py.utilities import get_message
        if self._node_config.get("intake", "executor") == "waitset":
            self._start_intake()
            return
        for key, (topic, type_name, qos) in self._input_topics().items():
            self._inputs.append(self.create_subscription(
                get_message(type_name), topic, lambda raw, key=key: self._enqueue(key, raw), qos,
                callback_group=self._group, raw=True))

    def _destroy_subscriptions(self) -> None:
        self._stop_intake()
        for subscription in self._inputs:
            self.destroy_subscription(subscription)
        self._inputs = []

    def _start_intake(self) -> None:
        """Wait-set intake: the same subscriptions on this node, read by one thread instead of the executor."""
        from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
        from rclpy.type_support import check_is_valid_msg_type
        from rosidl_runtime_py.utilities import get_message
        with self.handle:
            for key, (topic, type_name, qos) in self._input_topics().items():
                message_type = get_message(type_name)
                check_is_valid_msg_type(message_type)
                handle = _rclpy.Subscription(self.handle, message_type, topic, qos.get_c_qos_profile(), None)
                self._intake_entities.append((key, handle, message_type))
        with self.context.handle:
            self._intake_guard = _rclpy.GuardCondition(self.context.handle)
        self._intaking = True
        self._intake_thread = threading.Thread(target=self._intake, name="powerline_slam_intake", daemon=True)
        self._intake_thread.start()

    def _stop_intake(self) -> None:
        if self._intake_thread is None:
            return
        self._intaking = False
        with self._intake_guard:
            self._intake_guard.trigger_guard_condition()
        self._intake_thread.join(timeout=10.0)
        self._intake_thread = None
        for _, handle, _ in self._intake_entities:
            handle.destroy_when_not_in_use()
        self._intake_guard.destroy_when_not_in_use()
        self._intake_entities = []
        self._intake_guard = None

    def _intake(self) -> None:
        """Take every available message of every ready subscription per wake-up (raw, per-topic order preserved)."""
        from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
        entities, guard = self._intake_entities, self._intake_guard
        with ExitStack() as stack:
            for _, handle, _ in entities:
                stack.enter_context(handle)
            stack.enter_context(guard)
            stack.enter_context(self.context.handle)
            wait_set = _rclpy.WaitSet(len(entities), 1, 0, 0, 0, 0, self.context.handle)
            try:
                while self._intaking and self.context.ok():
                    wait_set.clear_entities()
                    for _, handle, _ in entities:
                        wait_set.add_subscription(handle)
                    wait_set.add_guard_condition(guard)
                    wait_set.wait(INTAKE_WAIT_NS)
                    ready = wait_set.get_ready_entities("subscription")
                    if not ready:
                        continue
                    for key, handle, message_type in entities:
                        if handle.pointer in ready:
                            while self._intaking:
                                taken = handle.take_message(message_type, True)
                                if taken is None:
                                    break
                                self._enqueue(key, taken[0])
            except Exception as exc:  # inputs would stop silently: exit so supervision respawns a clean node
                if self._intaking and self.context.ok():
                    self.get_logger().fatal(f"powerline_slam intake failed: {type(exc).__name__}: {exc}; exiting")
                    os._exit(EXIT_INTAKE_LOST)

    def _enqueue(self, key: str, raw: bytes) -> None:
        received = time.monotonic()
        overflow = None
        with self._outbox_ready:
            index = self._sequence[key]
            self._sequence[key] += 1
            if self._overload is not None:              # the epoch is dead: counted, never forwarded
                self._overload_counters["messages_dropped_after_overload"] += 1
                return
            if self._rolling:                           # between two epochs: counted per stream, in neither epoch
                self._sequence[key] -= 1
                self._rollover_gap[key] += 1
                return
            self._outbox.append((key, raw, index, received))
            self._outbox_high_water = max(self._outbox_high_water, len(self._outbox))
            self._outbox_ready.notify()
            if self._overload_limits is not None and len(self._outbox) > int(self._overload_limits["max_node_queue"]):
                overflow = len(self._outbox)
        if overflow is not None:                        # the host is not reading: the node itself fails closed
            self._latch_overload(self._node_overload_record("NODE_QUEUE", node_queue=overflow), by_node=True)

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
            try:
                if batch:
                    self._send(("msgs", batch))
                if flush:
                    self._send(("flush",))
            except (OSError, ValueError):               # the pipe went away (the host was stopped)
                return

    def _end_epoch(self) -> None:
        """Stop intake, discard what the host has not processed, release the epoch."""
        self._destroy_subscriptions()
        self._stop_watchdog()
        current = threading.current_thread()
        busy = [t for t in (self._rollover_thread, self._recovery_thread) if t is not None and t.is_alive() and t is not current]
        if busy:
            # a lifecycle transition during a rollover or a recovery: it gives up and nothing of it is reused
            self._rollover_abort = True
            for thread in busy:
                thread.join(timeout=ROLLOVER_ABORT_JOIN_S)
            with self._outbox_ready:
                self._rolling = False
                self._forwarding = False
                self._outbox.clear()
                self._outbox_ready.notify_all()
            if self._forwarder is not None:
                self._forwarder.join(timeout=FORWARDER_STOP_TIMEOUT_S)
                self._forwarder = None
            self._stop_host(unresponsive=True)
            self._rollover_thread = self._recovery_thread = None
            self._last_epoch_end = {"error": "ended during a traversal rollover or a recovery: the host was stopped; a new host "
                                             "starts at the next activation"}
            self.get_logger().warning(f"powerline_slam processing epoch {self._epoch_count} ended: {json.dumps(self._last_epoch_end)}")
            return
        self._recovery_thread = None
        self._rollover_thread = None
        if self._forwarder is None:
            if self._overload_by_node and self._host is not None:
                # failed closed in the middle of a rollover: nothing of that host is reused
                self._stop_host(unresponsive=True)
            return
        with self._outbox_ready:
            self._forwarding = False
            unsent = sum(1 for item in self._outbox if item[0] != "__flush__")
            self._outbox.clear()
            self._outbox_ready.notify_all()
        self._forwarder.join(timeout=FORWARDER_STOP_TIMEOUT_S)
        unresponsive = self._forwarder.is_alive() or self._overload_by_node
        self._forwarder = None
        if unresponsive:
            # the host did not read its pipe (or the node had to fail closed on its own): no estimator state of this
            # host is trusted or reused; the next activation starts a new host process
            self._stop_host(unresponsive=True)
            self._last_epoch_end = {"error": "estimator host unresponsive: stopped; a new host starts at the next activation",
                                    "discarded_unforwarded_messages_at_epoch_end": unsent}
            self.get_logger().warning(
                f"powerline_slam processing epoch {self._epoch_count} ended: {json.dumps(self._last_epoch_end)}")
            return
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
        if self._reset_service is not None:
            self.destroy_service(self._reset_service)
            self._reset_service = None

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
        if self._overload is not None:              # a dead epoch is never finalized
            response.success = False
            response.message = json.dumps(self._overload, default=str)
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
        if node._recovery is not None:
            node._recovery.mark_active(False)       # an orderly end of the process is not a respawn
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
