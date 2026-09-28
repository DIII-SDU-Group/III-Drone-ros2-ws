#!/usr/bin/env python3
"""Drive a canonical split-host HIL inspection/recharge cycle.

This runner deliberately keeps one ROS process alive while it owns a
CustomOperation maneuver.  Short-lived CLI clients cancel their action when
they exit, which is unsuitable for staging the vehicle at the inspection
entry pose.  It is development/HIL-only: PX4 and Gazebo remain workstation
owned and the ROS graph remains Pi owned.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
import signal
import threading
from pathlib import Path
import sys
import time
from typing import NamedTuple

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from std_srvs.srv import SetBool
from std_msgs.msg import Float32, String

from px4_msgs.msg import (
    BatteryStatus,
    OffboardControlMode,
    TrajectorySetpoint,
    VehicleCommand,
    VehicleCommandAck,
    VehicleLandDetected,
    VehicleLocalPosition,
    VehicleStatus,
)
from iii_drone_interfaces.msg import ChargerStatus, GripperStatus, StringStamped
from iii_drone_interfaces.srv import GripperCommand

# The Pi developer image keeps the MCP helpers as workspace source rather than
# an installed wheel.  Resolve that source path from this script so the
# canonical HIL runner works from a clean SSH shell as well as from the MCP
# development environment.
_workspace_root = Path(__file__).resolve().parents[2]
_mcp_source = _workspace_root / "tools" / "III-Drone-MCP"
if _mcp_source.is_dir():
    sys.path.insert(0, str(_mcp_source))

from iii_drone_mcp.agent_tools import DroneAgentTools
from iii_drone_mcp.px4_command_client import Px4CommandClient

sys.path.insert(0, str(_workspace_root / "tools" / "III-Drone-CLI"))
from iii.runtime_api_client import RuntimeApiClient


_PX4_TARGETS = {
    "hil": {"system_id": 8, "default_endpoint": "udpin://0.0.0.0:14544"},
    "sim": {"system_id": 1, "default_endpoint": "udpin://0.0.0.0:14540"},
}
_RUNTIME_API_REQUEST_TIMEOUT_SEC = 15.0


def _call_runtime_api(driver, operation, *, timeout_sec: float = _RUNTIME_API_REQUEST_TIMEOUT_SEC):
    """Keep lightweight test doubles compatible while real Drivers spin ROS."""
    helper = getattr(driver, "_runtime_api_call", None)
    return helper(operation, timeout_sec=timeout_sec) if helper else operation()


class InspectionTransitionDecision(NamedTuple):
    """Evidence-backed result of leaving Inspection Demo for Reach Cable."""

    transition: str
    inspection_status: dict[str, object]
    reach_status: dict[str, object]
    mission_state: dict[str, object]
    battery_evidence: dict[str, object]


def select_px4_target(
    identity: dict[str, object],
    api_endpoint: str | None,
    configured_endpoint: str | None = None,
    system_id: int | None = None,
) -> tuple[str, int]:
    """Validate the sourced Runtime API target and return its command endpoint."""
    profile = identity.get("profile")
    target = _PX4_TARGETS.get(profile) if isinstance(profile, str) else None
    if target is None:
        raise RuntimeError(f"Unsupported Runtime API profile for flight driver: {profile!r}")
    expected_system_id = int(target["system_id"])
    if system_id is not None and system_id != expected_system_id:
        raise RuntimeError(
            f"Runtime API profile {profile!r} requires PX4 system {expected_system_id}, "
            f"received {system_id}"
        )
    expected_endpoint = (
        configured_endpoint
        if configured_endpoint is not None
        else str(target["default_endpoint"])
    )
    if api_endpoint != expected_endpoint:
        raise RuntimeError(
            f"Runtime API profile {profile!r} command endpoint mismatch: "
            f"expected {expected_endpoint}, received {api_endpoint}"
        )
    return expected_endpoint, expected_system_id


class Driver(Node):
    def __init__(self) -> None:
        # PX4 SITL and Gazebo publish a simulation clock across the HIL link.
        # Setpoint timestamps must use that same clock; a default rclpy node
        # otherwise stamps them with workstation wall time.
        super().__init__(
            "hil_inspection_cycle_driver",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.vehicle: VehicleStatus | None = None
        self.land_detected: VehicleLandDetected | None = None
        self._vehicle_receipt_at: str | None = None
        self._vehicle_receipt_monotonic: float | None = None
        self._vehicle_message_timestamp_us: int | None = None
        self._land_detected_receipt_at: str | None = None
        self._land_detected_receipt_monotonic: float | None = None
        self._land_detected_message_timestamp_us: int | None = None
        self.local_position: VehicleLocalPosition | None = None
        self.gripper_status: GripperStatus | None = None
        self.charging_samples: dict[str, tuple[float, object]] = {}
        self.battery_status_sample: tuple[float, BatteryStatus] | None = None
        self.custom_operation_mode_id: int | None = None
        self._custom_operation_status_stamp: tuple[int, int] = (-1, -1)
        self._custom_operation_status_floor: tuple[int, int] = (-1, -1)
        self.command_acks: list[VehicleCommandAck] = []
        self.runtime_client = RuntimeApiClient.from_env()
        self.runtime_client.timeout_seconds = min(
            self.runtime_client.timeout_seconds, _RUNTIME_API_REQUEST_TIMEOUT_SEC
        )
        self.px4_system_address: str | None = None
        self.native_command_receipts: list[dict[str, object]] = []
        self.native_state_receipts: list[dict[str, object]] = []
        self.mode_status: dict[str, dict[str, object]] = {}
        self.mode_status_receipts: list[dict[str, object]] = []
        self.registration_refresh_trace: list[dict[str, object]] = []
        self._mode_status_floor: dict[str, tuple[int, int]] = {}
        self.publisher = self.create_publisher(
            VehicleCommand, "/fmu/in/vehicle_command", 10
        )
        self.offboard_mode_publisher = self.create_publisher(
            OffboardControlMode, "/fmu/in/offboard_control_mode", 10
        )
        self.trajectory_publisher = self.create_publisher(
            TrajectorySetpoint, "/fmu/in/trajectory_setpoint", 10
        )
        self.create_subscription(
            VehicleStatus,
            "/fmu/out/vehicle_status_v1",
            self._on_vehicle,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            VehicleCommandAck,
            "/fmu/out/vehicle_command_ack",
            self._on_command_ack,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            VehicleLandDetected,
            "/fmu/out/vehicle_land_detected",
            self._on_land_detected,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            VehicleLocalPosition,
            "/fmu/out/vehicle_local_position",
            self._on_local_position,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            BatteryStatus,
            "/fmu/out/battery_status",
            self._on_battery_status,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            GripperStatus,
            "/payload/charger_gripper/gripper_status",
            self._on_gripper_status,
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
                # The simulated and physical payload controllers publish this
                # rapidly-updated state with sensor-style best-effort QoS.
                # Requesting reliable delivery prevents DDS matching entirely.
                reliability=ReliabilityPolicy.BEST_EFFORT,
            ),
        )
        for topic, message_type, field in (
            ("charger_status", ChargerStatus, "charger_status"),
            ("charging_power", Float32, "data"),
            ("sim_state", String, "data"),
        ):
            self.create_subscription(
                message_type, f"/payload/charger_gripper/{topic}",
                lambda message, key=topic, attr=field: self.charging_samples.update(
                    {key: (time.monotonic(), getattr(message, attr))}
                ),
                qos_profile_sensor_data,
            )
        mode_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        for name in ("custom_operation", "inspection_demo", "reach_cable", "cable_charging", "leave_cable"):
            self.create_subscription(
                StringStamped,
                f"/mission/modes/{name}/status",
                lambda message, mode=name: self._on_mode(mode, message),
                mode_qos,
            )
        custom_mode_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            StringStamped,
            "/mission/custom_operation/status",
            self._on_custom_operation_status,
            custom_mode_qos,
        )

    def _on_vehicle(self, message: VehicleStatus) -> None:
        self.vehicle = message
        self._vehicle_receipt_at = datetime.now(timezone.utc).isoformat()
        self._vehicle_receipt_monotonic = time.monotonic()
        self._vehicle_message_timestamp_us = int(getattr(message, "timestamp", 0))

    def _on_command_ack(self, message: VehicleCommandAck) -> None:
        self.command_acks.append(message)

    def _on_land_detected(self, message: VehicleLandDetected) -> None:
        self.land_detected = message
        self._land_detected_receipt_at = datetime.now(timezone.utc).isoformat()
        self._land_detected_receipt_monotonic = time.monotonic()
        self._land_detected_message_timestamp_us = int(getattr(message, "timestamp", 0))

    def _on_local_position(self, message: VehicleLocalPosition) -> None:
        self.local_position = message

    def _on_battery_status(self, message: BatteryStatus) -> None:
        self.battery_status_sample = (time.monotonic(), message)

    def _on_gripper_status(self, message: GripperStatus) -> None:
        self.gripper_status = message

    def _on_mode(self, name: str, message: StringStamped) -> None:
        receipt = {
            "event": "mode_status_subscriber_receipt",
            "mode": name,
            "receipt_at": datetime.now(timezone.utc).isoformat(),
            "stamp": (int(message.stamp.sec), int(message.stamp.nanosec)),
            "data": message.data,
        }
        self.mode_status_receipts.append(receipt)
        try:
            value = json.loads(message.data)
            # StringStamped carries the authoritative publication timestamp
            # outside the JSON payload.  Retain it so a fresh wait cannot
            # mistake a latched terminal result from the preceding mode
            # generation for the current activation.
            if isinstance(value, dict):
                value["_stamp"] = (int(message.stamp.sec), int(message.stamp.nanosec))
            self.mode_status[name] = value
        except json.JSONDecodeError:
            self.get_logger().warning(f"invalid {name} mode status: {message.data!r}")

    def _on_custom_operation_status(self, message: StringStamped) -> None:
        self.mode_status_receipts.append({
            "event": "mode_status_subscriber_receipt",
            "mode": "custom_operation",
            "receipt_at": datetime.now(timezone.utc).isoformat(),
            "stamp": (int(message.stamp.sec), int(message.stamp.nanosec)),
            "data": message.data,
        })
        try:
            value = json.loads(message.data)
        except json.JSONDecodeError:
            self.get_logger().warning(
                f"invalid custom_operation status: {message.data!r}"
            )
            return
        if isinstance(value, dict):
            stamp = (int(message.stamp.sec), int(message.stamp.nanosec))
            if stamp <= self._custom_operation_status_floor:
                return
            self._custom_operation_status_stamp = stamp
            mode_id = value.get("mode_id")
            if isinstance(mode_id, int) and 23 <= mode_id <= 30:
                self.custom_operation_mode_id = mode_id

    def clear_mode_status(self, *names: str) -> None:
        """Discard terminal status from a preceding mission generation."""
        for name in names:
            previous = self.mode_status.get(name)
            if isinstance(previous, dict):
                stamp = previous.get("_stamp")
                if isinstance(stamp, (tuple, list)) and len(stamp) == 2:
                    self._mode_status_floor[name] = (int(stamp[0]), int(stamp[1]))
            self.mode_status.pop(name, None)

    def ensure_armed_handoff(self, timeout_sec: float = 20.0) -> None:
        """Require the ingress maneuver to preserve its airborne arm state."""
        vehicle = self.wait_vehicle(timeout_sec=timeout_sec)
        if vehicle.failsafe:
            raise RuntimeError("PX4 is in failsafe at mission-mode handoff")
        if vehicle.arming_state != VehicleStatus.ARMING_STATE_ARMED:
            raise RuntimeError("PX4 disarmed during ingress; refusing to re-arm at mission handoff")

    def wait_vehicle(self, timeout_sec: float = 10.0) -> VehicleStatus:
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if self.vehicle is not None:
                return self.vehicle
        self.get_logger().error(
            "VehicleStatus wait expired: "
            f"timeout_sec={timeout_sec:.3f} cached={self.vehicle is not None} "
            f"rclpy_ok={rclpy.ok()}"
        )
        raise TimeoutError("no PX4 VehicleStatus received")

    def select_nav_state(self, nav_state: int, repeat_count: int = 10) -> None:
        vehicle = self.wait_vehicle()
        if not 23 <= nav_state <= 30:
            raise ValueError(f"PX4 external mode must be in [23, 30], got {nav_state}")
        # Do not report an acknowledgement from the preceding Offboard/arm
        # prelude as the acknowledgement for this mode transition.
        self.command_acks.clear()
        for _ in range(repeat_count):
            command = VehicleCommand()
            command.timestamp = vehicle.timestamp
            # PX4's externally registered modes are selected through the
            # canonical custom-mode command: AUTO main mode plus the live
            # EXTERNALn submode assigned by the registration stream.
            command.command = VehicleCommand.VEHICLE_CMD_DO_SET_MODE
            command.param1 = 1.0  # MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            command.param2 = 4.0  # PX4_CUSTOM_MAIN_MODE_AUTO
            command.param3 = float(11 + nav_state - 23)
            command.target_system = vehicle.system_id
            command.target_component = vehicle.component_id
            # Match the canonical ROS/PX4 command identity used by the
            # mission modes.  PX4 may silently ignore a ROS command marked as
            # coming from MAVLink component 0.
            command.source_system = 1
            command.source_component = 1
            command.from_external = True
            self.publisher.publish(command)
            rclpy.spin_once(self, timeout_sec=0.05)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
        matching_acks = [
            ack for ack in self.command_acks
            if ack.command == VehicleCommand.VEHICLE_CMD_DO_SET_MODE
        ]
        if matching_acks:
            ack = matching_acks[-1]
            self.get_logger().info(
                f"external-mode command ack: result={ack.result} result_param2={ack.result_param2}"
            )
            if ack.result != VehicleCommandAck.VEHICLE_CMD_RESULT_ACCEPTED:
                current = self.vehicle
                raise RuntimeError(
                    f"PX4 rejected external mode {nav_state}: result={ack.result}; "
                    f"arming_state={getattr(current, 'arming_state', None)} "
                    f"nav_state={getattr(current, 'nav_state', None)} "
                    f"failsafe={getattr(current, 'failsafe', None)}"
                )

    def _publish_command(
        self,
        command_id: int,
        *,
        param1: float = 0.0,
        param2: float = 0.0,
        param3: float = 0.0,
        param7: float = 0.0,
        repeat_count: int = 5,
    ) -> None:
        vehicle = self.wait_vehicle()
        command = VehicleCommand()
        command.timestamp = vehicle.timestamp
        command.command = command_id
        command.param1 = float(param1)
        command.param2 = float(param2)
        command.param3 = float(param3)
        command.param7 = float(param7)
        command.target_system = vehicle.system_id
        command.target_component = vehicle.component_id
        command.source_system = 1
        command.source_component = 1
        command.from_external = True
        for _ in range(repeat_count):
            command.timestamp = self.vehicle.timestamp if self.vehicle is not None else vehicle.timestamp
            self.publisher.publish(command)
            rclpy.spin_once(self, timeout_sec=0.1)

    def _runtime_api_call(self, operation, *, timeout_sec: float = _RUNTIME_API_REQUEST_TIMEOUT_SEC):
        """Run one bounded Runtime API request while keeping ROS callbacks moving."""
        client = getattr(self, "runtime_client", None)
        previous_timeout = getattr(client, "timeout_seconds", None)
        request_timeout = min(_RUNTIME_API_REQUEST_TIMEOUT_SEC, max(0.05, timeout_sec))
        if previous_timeout is not None:
            client.timeout_seconds = min(previous_timeout, request_timeout)
        done = threading.Event()
        outcome: list[tuple[bool, object]] = []

        def request() -> None:
            try:
                outcome.append((True, operation()))
            except BaseException as exc:
                outcome.append((False, exc))
            finally:
                done.set()

        worker = threading.Thread(target=request, name="hil-runtime-api-request", daemon=True)
        worker.start()
        try:
            while not done.is_set():
                if rclpy.ok():
                    rclpy.spin_once(self, timeout_sec=0.05)
                else:
                    done.wait(0.05)
        finally:
            # urlopen is capped on this client, so join even on ROS shutdown or
            # KeyboardInterrupt; no request thread may outlive the caller.
            worker.join()
            if previous_timeout is not None:
                client.timeout_seconds = previous_timeout
        succeeded, value = outcome[0]
        if not succeeded:
            raise value
        return value

    def native_flight_command(self, command_id: str, parameters: dict | None = None) -> None:
        """Use the same acknowledged, gated command path as native GC/CLI."""
        response = _call_runtime_api(self,
            lambda: self.runtime_client.command(command_id, parameters or {})
        )
        self.native_command_receipts.append({"command_id": command_id, "response": response})
        if response.get("accepted") is not True:
            raise RuntimeError(f"Native {command_id} rejected: {json.dumps(response, default=str)}")

    def wait_native_command_result(self, request_id: str, timeout_sec: float = 15.0) -> dict:
        """Require a terminal result for this request before dependent work."""
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            events = _call_runtime_api(self,
                lambda: self.runtime_client._request("GET", "/events/recent"),
                timeout_sec=deadline - time.monotonic(),
            )
            for event in events:
                if event.get("category") != "command_result" or event.get("request_id") != request_id:
                    continue
                result = event.get("details") or {}
                status = result.get("status")
                if status == "succeeded":
                    self.native_state_receipts.append({"request_id": request_id, "terminal_result": result})
                    return result
                if status in {"failed", "rejected", "cancelled"}:
                    raise RuntimeError(f"Native request {request_id} ended {status}: {result}")
            rclpy.spin_once(self, timeout_sec=0.2)
        raise TimeoutError(f"Native request {request_id} has no successful terminal result")

    def wait_native_mission_phase(self, mode_key: str, timeout_sec: float = 15.0) -> dict:
        """Wait for the native intent gate's independently received phase."""
        deadline = time.monotonic() + timeout_sec
        state: dict = {}
        while rclpy.ok() and time.monotonic() < deadline:
            state = _call_runtime_api(self,
                lambda: self.runtime_client._request("GET", "/mission/status"),
                timeout_sec=deadline - time.monotonic(),
            )
            active = [mode for mode in state.get("modes", []) if mode.get("active")]
            if (
                state.get("freshness") == "fresh"
                and state.get("source_availability") == "available"
                and len(active) == 1
                and active[0].get("mode_key") == mode_key
                and active[0].get("freshness") == "fresh"
            ):
                self.native_state_receipts.append({"expected_mission_phase": mode_key, "state": state})
                return state
            rclpy.spin_once(self, timeout_sec=0.2)
        raise TimeoutError(f"Native mission phase did not converge to {mode_key}: {state}")

    def read_fresh_native_mission_phase(self) -> tuple[str, dict]:
        """Read exactly one fresh active mission phase from the Runtime API."""
        state = _call_runtime_api(self,
            lambda: self.runtime_client._request("GET", "/mission/status")
        )
        active = [mode for mode in state.get("modes", []) if mode.get("active")]
        if (
            state.get("freshness") != "fresh"
            or state.get("source_availability") != "available"
            or len(active) != 1
            or active[0].get("freshness") != "fresh"
        ):
            raise RuntimeError(f"Cannot verify active mission phase from fresh Runtime API state: {state}")
        mode_key = str(active[0].get("mode_key", ""))
        if not mode_key:
            raise RuntimeError(f"Fresh Runtime API mission phase has no mode key: {state}")
        self.native_state_receipts.append({
            "expected_mission_phase": mode_key,
            "state": state,
            "observation": "current_active_mission_phase",
        })
        return mode_key, state

    @staticmethod
    def _mode_stamp(value: dict) -> tuple[int, int] | None:
        stamp = value.get("_stamp")
        if isinstance(stamp, (tuple, list)) and len(stamp) == 2:
            return int(stamp[0]), int(stamp[1])
        return None

    def wait_for_fresh_leave_cable_status(self, timeout_sec: float = 15.0) -> dict:
        """Accept a fresh Leave activation or its successful terminal result."""
        deadline = time.monotonic() + timeout_sec
        floor = self._mode_status_floor.get("leave_cable", (-1, -1))
        fresh_active_receipt = False
        while rclpy.ok() and time.monotonic() < deadline:
            for receipt in self.mode_status_receipts:
                if receipt.get("mode") != "leave_cable":
                    continue
                stamp = receipt.get("stamp")
                if not isinstance(stamp, (tuple, list)) or len(stamp) != 2:
                    continue
                if (int(stamp[0]), int(stamp[1])) <= floor:
                    continue
                try:
                    data = json.loads(str(receipt.get("data", "")))
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("active"):
                    fresh_active_receipt = True

            value = self.mode_status.get("leave_cable")
            if isinstance(value, dict):
                stamp = Driver._mode_stamp(value)
                if stamp is not None and stamp > floor:
                    if value.get("tree_finished") and not value.get("tree_success"):
                        raise RuntimeError(f"leave_cable failed during automatic transition: {value}")
                    if value.get("active"):
                        return value
                    if value.get("tree_finished") and value.get("tree_success") and fresh_active_receipt:
                        return value
            rclpy.spin_once(self, timeout_sec=0.2)
        raise TimeoutError(
            "no fresh Leave Cable activation or successful terminal status was observed: "
            f"{self.mode_status.get('leave_cable')}"
        )

    def wait_leave_cable_succeeded(self, timeout_sec: float = 600.0) -> dict:
        """Require fresh Leave-running evidence before a later terminal success."""
        deadline = time.monotonic() + timeout_sec
        floor = self._mode_status_floor.get("leave_cable", (-1, -1))
        running_stamp: tuple[int, int] | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            for receipt in self.mode_status_receipts:
                if receipt.get("mode") != "leave_cable":
                    continue
                stamp = receipt.get("stamp")
                if not isinstance(stamp, (tuple, list)) or len(stamp) != 2:
                    continue
                if (int(stamp[0]), int(stamp[1])) <= floor:
                    continue
                try:
                    data = json.loads(str(receipt.get("data", "")))
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                receipt_stamp = (int(stamp[0]), int(stamp[1]))
                if (
                    data.get("active")
                    and data.get("tree_running")
                    and not data.get("tree_finished")
                ):
                    if running_stamp is None or receipt_stamp > running_stamp:
                        running_stamp = receipt_stamp
                    continue
                if data.get("tree_finished") and running_stamp is not None:
                    if not data.get("tree_success"):
                        raise RuntimeError(f"leave_cable failed: {data}")
                    if receipt_stamp > running_stamp:
                        return {**data, "_stamp": receipt_stamp}

            value = self.mode_status.get("leave_cable")
            if isinstance(value, dict):
                stamp = Driver._mode_stamp(value)
                if stamp is not None and stamp > floor:
                    if (
                        value.get("active")
                        and value.get("tree_running")
                        and not value.get("tree_finished")
                    ):
                        if running_stamp is None or stamp > running_stamp:
                            running_stamp = stamp
                    if value.get("tree_finished") and running_stamp is not None:
                        if not value.get("tree_success"):
                            raise RuntimeError(f"leave_cable failed: {value}")
                        if stamp > running_stamp:
                            return value
            rclpy.spin_once(self, timeout_sec=0.2)
        raise TimeoutError(
            "Leave Cable did not publish a fresh successful terminal status: "
            f"{self.mode_status.get('leave_cable')}"
        )

    @staticmethod
    def _is_active_leave_phase_rejection(response: dict) -> bool:
        rejection = response.get("rejection")
        if not isinstance(rejection, dict):
            return False
        return (
            response.get("accepted") is False
            and rejection.get("code") == "forbidden"
            and rejection.get("message")
            == "runtime intent service is not valid for active mode 'leave_cable'"
        )

    def advance_after_charging(
        self,
        *,
        full_charge_evidence: dict[str, object] | None = None,
        full_charge_timeout_sec: float = 3.0,
        automatic_only: bool = False,
        transition_timeout_sec: float = 30.0,
    ) -> dict[str, object]:
        """Request Leave only while charging; accept only verified auto-Leave races."""
        phase, state = self.read_fresh_native_mission_phase()
        if automatic_only:
            deadline = time.monotonic() + transition_timeout_sec
            while phase == "cable_charging" and rclpy.ok():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Full charge did not trigger automatic Leave within the deadline")
                status = self.mode_status.get("cable_charging", {})
                if status.get("tree_finished") and not status.get("tree_success"):
                    raise RuntimeError(f"Cable Charging failed before automatic Leave: {status}")
                rclpy.spin_once(self, timeout_sec=0.2)
                phase, state = self.read_fresh_native_mission_phase()
            if phase == "cable_charging":
                raise RuntimeError("ROS stopped before automatic Leave")
        if phase in {"leave_cable", "inspection_demo"}:
            evidence = full_charge_evidence or self.wait_until_fully_charged(
                full_charge_timeout_sec, allow_leave_cable=True
            )
            leave_status = self.wait_for_fresh_leave_cable_status()
            return {
                "transition": "automatic",
                "mission_state": state,
                "leave_status": leave_status,
                "full_charge_evidence": evidence,
            }
        if phase != "cable_charging":
            raise RuntimeError(f"Cannot advance after charging from unexpected mission phase {phase!r}: {state}")

        receipt_count = len(self.native_command_receipts)
        try:
            self.native_flight_command("mission.leave_cable_now")
        except RuntimeError:
            command_receipts = self.native_command_receipts[receipt_count:]
            current_receipt = next(
                (
                    receipt for receipt in reversed(command_receipts)
                    if receipt.get("command_id") == "mission.leave_cable_now"
                ),
                None,
            )
            response = current_receipt.get("response", {}) if current_receipt else {}
            if not self._is_active_leave_phase_rejection(response):
                raise
            phase, state = self.read_fresh_native_mission_phase()
            if phase != "leave_cable":
                raise RuntimeError(
                    "Leave command was forbidden because Leave Cable was active, "
                    f"but fresh Runtime API phase is {phase!r}: {state}"
                )
            evidence = full_charge_evidence or self.wait_until_fully_charged(
                full_charge_timeout_sec, allow_leave_cable=True
            )
            leave_status = self.wait_for_fresh_leave_cable_status()
            return {
                "transition": "automatic",
                "mission_state": state,
                "leave_status": leave_status,
                "full_charge_evidence": evidence,
            }
        return {"transition": "commanded", "mission_state": state}

    def wait_native_vehicle_state(self, expected: dict[str, object], timeout_sec: float = 15.0) -> dict:
        """Wait for the state used by the next native command's safety gate.

        Our ROS subscriber can observe a transition before the Runtime API's
        subscriber. An ACK or our local ROS observation alone does not make
        the following command ready at that separate consumer.
        """
        deadline = time.monotonic() + timeout_sec
        state: dict = {}
        while rclpy.ok() and time.monotonic() < deadline:
            state = _call_runtime_api(self,
                self.runtime_client.vehicle_status,
                timeout_sec=deadline - time.monotonic(),
            )
            fields = state.get("telemetry_fields") or {}
            if (
                state.get("freshness") == "fresh"
                and state.get("latest", {}).get("dangerous_commands_allowed") is True
                and all(
                    state.get(name) == value
                    and (fields.get(name) or {}).get("value") == value
                    and (fields.get(name) or {}).get("freshness") == "fresh"
                    and (fields.get(name) or {}).get("source_availability") == "available"
                    and (fields.get(name) or {}).get("disagreement") is not True
                    for name, value in expected.items()
                )
            ):
                self.native_state_receipts.append({"expected": expected, "state": state})
                return state
            rclpy.spin_once(self, timeout_sec=0.2)
        raise TimeoutError(f"Native vehicle state did not converge to {expected}: {state}")

    def wait_native_mission_owner_cleared(self, timeout_sec: float = 15.0) -> dict[str, object]:
        """Require fresh Runtime proof that Hold owns control and no mission mode runs."""
        deadline = time.monotonic() + timeout_sec
        latest: dict[str, object] = {}
        while rclpy.ok() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            control = _call_runtime_api(self,
                lambda: self.runtime_client._request("GET", "/control/status"),
                timeout_sec=remaining,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            mission = _call_runtime_api(self,
                lambda: self.runtime_client._request("GET", "/mission/status"),
                timeout_sec=remaining,
            )
            modes = mission.get("modes") or []
            latest = {"control": control, "mission": mission}
            control_latest = control.get("latest") or {}
            interruption = control_latest.get("hold_interruption") or {}
            transition = control_latest.get("transition") or {}
            hold_proven = (
                interruption.get("command_id") == "px4.hold"
                and interruption.get("completed") is True
            ) or (
                transition.get("command_id") == "px4.hold"
                and transition.get("status") == "terminated"
                and "hold confirmed" in str(transition.get("message", "")).lower()
            )
            if (
                control.get("freshness") == "fresh"
                and control.get("source_availability") == "available"
                and control.get("owner") == "px4_hold"
                and control.get("active_setpoint_owner") == "px4"
                and hold_proven
                and mission.get("freshness") == "fresh"
                and mission.get("source_availability") == "available"
                and mission.get("latest", {}).get("mission_active") is False
                and all(mode.get("active") is not True for mode in modes)
            ):
                evidence = {
                    "expected_control_owner": "px4_hold",
                    "control": control,
                    "mission": mission,
                    "hold_interruption": interruption,
                    "transition": transition,
                }
                self.native_state_receipts.append(evidence)
                return evidence
        raise TimeoutError(
            "Runtime API did not prove fresh PX4 Hold ownership and cleared mission modes: "
            f"{latest}"
        )

    def wait_native_disarmed(self, timeout_sec: float = 15.0) -> dict[str, object]:
        """Require fresh fused Runtime telemetry showing disarmed."""
        deadline = time.monotonic() + timeout_sec
        state: dict[str, object] = {}
        while rclpy.ok() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            state = _call_runtime_api(self,
                self.runtime_client.vehicle_status,
                timeout_sec=remaining,
            )
            fields = state.get("telemetry_fields") or {}
            armed = fields.get("armed") or {}
            if (
                state.get("freshness") == "fresh"
                and state.get("armed") is False
                and armed.get("value") is False
                and armed.get("freshness") == "fresh"
                and armed.get("source_availability") == "available"
                and armed.get("disagreement") is not True
            ):
                self.native_state_receipts.append({"expected": {"armed": False}, "state": state})
                return state
        raise TimeoutError(f"Runtime API did not report fresh disarmed state: {state}")

    def activate_inspection_from_ingress(self, inspection_mode_id: int) -> None:
        """Release Custom Operation into Hold before native mission activation.

        The production inspection preflight requires manual/Hold control.
        Completing a maneuver leaves Custom Operation active, so its terminal
        result alone does not satisfy that ownership handoff.
        """
        self.native_flight_command("px4.hold")
        self.wait_nav_state(VehicleStatus.NAVIGATION_STATE_AUTO_LOITER)
        self.wait_native_vehicle_state({"armed": True, "in_air": True, "nav_state": "hold"})
        self.native_flight_command("mission.activate", {"mode_key": "inspection_demo"})
        self.wait_nav_state(inspection_mode_id)

    def prepare_airborne(
        self,
        tools: DroneAgentTools,
        altitude_m: float = 2.0,
        timeout_sec: float = 90.0,
    ) -> None:
        """Put the selected SIM/HIL target into airborne hold via Runtime API."""
        identity = _call_runtime_api(self, self.runtime_client.identity)
        vehicle = self.wait_vehicle()
        state = _call_runtime_api(self, self.runtime_client.vehicle_status)
        endpoint = state.get("latest", {}).get("command_transport", {}).get("endpoint")
        selected_endpoint, _ = select_px4_target(
            identity,
            endpoint,
            os.environ.get("III_PX4_SYSTEM_ADDRESS"),
            int(vehicle.system_id),
        )
        self.px4_system_address = selected_endpoint
        if vehicle.arming_state == VehicleStatus.ARMING_STATE_DISARMED:
            self.native_flight_command("px4.arm")
            arm_deadline = time.monotonic() + 15.0
            while rclpy.ok() and time.monotonic() < arm_deadline:
                vehicle = self.wait_vehicle(timeout_sec=min(1.0, arm_deadline - time.monotonic()))
                if vehicle.arming_state == VehicleStatus.ARMING_STATE_ARMED:
                    break
            else:
                raise TimeoutError(f"Native arm was acknowledged but not observed: {self.status_snapshot()}")

        self.wait_native_vehicle_state({"armed": True})
        self.native_flight_command("px4.takeoff", {"altitude_m": float(altitude_m)})
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            vehicle = self.wait_vehicle(timeout_sec=min(1.0, deadline - time.monotonic()))
            if vehicle.failsafe:
                raise RuntimeError(f"PX4 entered failsafe during native takeoff: {self.status_snapshot()}")
            airborne = self.land_detected is not None and not self.land_detected.landed
            if (
                vehicle.arming_state == VehicleStatus.ARMING_STATE_ARMED
                and vehicle.nav_state == VehicleStatus.NAVIGATION_STATE_AUTO_LOITER
                and airborne
                and self.local_position is not None
            ):
                self.wait_native_vehicle_state({"armed": True, "in_air": True, "nav_state": "hold"})
                return
        raise TimeoutError(f"Native takeoff did not reach airborne hold: {self.status_snapshot()}")

    def send_mavsdk_command(
        self,
        command: int,
        vehicle: VehicleStatus,
        params: tuple[float, float, float, float, float, float, float],
    ) -> None:
        """Send one addressed MAVLink command through the selected Pi endpoint."""
        if self.px4_system_address is None:
            raise RuntimeError("PX4 command endpoint has not passed Runtime API preflight")
        async def send() -> None:
            client = Px4CommandClient(
                self.px4_system_address
            )
            try:
                await client.connect()
                await client._send_targeted_command_long(
                    command=int(command),
                    target_system=int(vehicle.system_id),
                    target_component=int(vehicle.component_id),
                    params=params,
                    source_system=255,
                    source_component=190,
                )
            finally:
                await client.close_async()

        asyncio.run(asyncio.wait_for(send(), timeout=15.0))

    def wait_local_position(self, timeout_sec: float = 10.0) -> VehicleLocalPosition:
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if self.local_position is not None and self.local_position.xy_valid and self.local_position.z_valid:
                return self.local_position
        raise TimeoutError("no valid PX4 local position received")

    def _publish_offboard_setpoint(self, target: list[float], yaw: float) -> None:
        timestamp = int(self.get_clock().now().nanoseconds / 1000)
        mode = OffboardControlMode()
        mode.timestamp = timestamp
        mode.position = True
        self.offboard_mode_publisher.publish(mode)
        setpoint = TrajectorySetpoint()
        setpoint.timestamp = timestamp
        setpoint.position = target
        setpoint.velocity = [math.nan, math.nan, math.nan]
        setpoint.acceleration = [math.nan, math.nan, math.nan]
        setpoint.jerk = [math.nan, math.nan, math.nan]
        setpoint.yaw = yaw
        setpoint.yawspeed = math.nan
        self.trajectory_publisher.publish(setpoint)

    def wait_nav_state(self, nav_state: int, timeout_sec: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_sec
        last_state: int | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            vehicle = self.wait_vehicle(timeout_sec=min(1.0, deadline - time.monotonic()))
            if vehicle.nav_state == nav_state:
                return
            if vehicle.nav_state != last_state:
                last_state = int(vehicle.nav_state)
                self.get_logger().warning(
                    f"waiting for PX4 nav state {nav_state}; "
                    f"current={vehicle.nav_state} "
                    f"user_intention={vehicle.nav_state_user_intention} "
                    f"arming={vehicle.arming_state} failsafe={vehicle.failsafe} "
                    f"valid_mask={vehicle.valid_nav_states_mask} "
                    f"can_set_mask={vehicle.can_set_nav_states_mask}"
                )
        vehicle = self.vehicle
        details = ""
        if vehicle is not None:
            details = (
                f" current={vehicle.nav_state} user_intention={vehicle.nav_state_user_intention}"
                f" arming={vehicle.arming_state} failsafe={vehicle.failsafe}"
                f" valid_mask={vehicle.valid_nav_states_mask}"
                f" can_set_mask={vehicle.can_set_nav_states_mask}"
            )
        raise TimeoutError(f"PX4 did not enter nav state {nav_state}.{details}")

    def ensure_external_modes_registered(
        self,
        tools: DroneAgentTools,
        timeout_sec: float = 120.0,
    ) -> dict[str, int]:
        """Verify the mode IDs belong to the current live PX4 session.

        PX4 SITL can be recreated while the Pi ROS graph remains healthy.  In
        that case every ModeBase process still advertises its old external-mode
        ID, while the new PX4 instance has no corresponding registrations.  A
        failed preflight command is too late: stale IDs can also collide with a
        different freshly registered mode.  Restart the complete application
        runtime once, then require every canonical mode to be present in PX4's
        live ``valid_nav_states_mask`` before the mission prelude begins.
        PX4 intentionally leaves external modes out of
        ``can_set_nav_states_mask`` while disarmed; that mask is a
        commandability gate, not a registration/identity probe.
        """
        mode_names = (
            "custom_operation",
            "inspection_demo",
            "reach_cable",
            "cable_charging",
            "leave_cable",
        )

        for refresh_attempt in range(2):
            deadline = time.monotonic() + timeout_sec
            while rclpy.ok() and time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=0.2)
                vehicle = self.vehicle
                if vehicle is None:
                    continue

                mode_ids: dict[str, int] = {}
                if self.custom_operation_mode_id is not None:
                    mode_ids["custom_operation"] = self.custom_operation_mode_id
                for name in mode_names[1:]:
                    value = self.mode_status.get(name)
                    mode_id = value.get("mode_id") if isinstance(value, dict) else None
                    if isinstance(mode_id, int) and 23 <= mode_id <= 30:
                        mode_ids[name] = mode_id

                if set(mode_ids) != set(mode_names):
                    continue
                if len(set(mode_ids.values())) != len(mode_names):
                    continue
                valid_mask = int(getattr(vehicle, "valid_nav_states_mask", 0))
                can_set_mask = int(getattr(vehicle, "can_set_nav_states_mask", 0))
                self.registration_refresh_trace.append({
                    "event": "registration_refresh_observation",
                    "refresh_attempt": refresh_attempt,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "mode_ids": dict(mode_ids),
                    "valid_nav_states_mask": valid_mask,
                    "can_set_nav_states_mask": can_set_mask,
                    "nav_state": int(vehicle.nav_state),
                    "vehicle_timestamp": int(getattr(vehicle, "timestamp", 0)),
                    "mode_status_stamps": {
                        name: value.get("_stamp")
                        for name, value in self.mode_status.items()
                        if isinstance(value, dict) and "_stamp" in value
                    },
                    "custom_operation_mode_id": self.custom_operation_mode_id,
                })
                # PX4 marks every EXTERNAL1..8 slot valid so it can display a
                # label even when no live ROS registration owns that slot.
                # Only can_set_nav_states_mask proves that the current PX4
                # instance accepted the registration request.  Checking the
                # valid mask alone can therefore accept a transient-local
                # registration reply from a predecessor PX4 instance.
                if all(
                    valid_mask & (1 << mode_id)
                    and can_set_mask & (1 << mode_id)
                    for mode_id in mode_ids.values()
                ):
                    self.get_logger().info(
                        "live PX4 external modes ready: "
                        + ", ".join(f"{name}={mode_ids[name]}" for name in mode_names)
                    )
                    return mode_ids

            if refresh_attempt == 1:
                break

            self.get_logger().warning(
                "PX4 external-mode registration is stale or incomplete; "
                "restarting the Pi application runtime once before HIL preflight."
            )
            self.mode_status.clear()
            self.custom_operation_mode_id = None
            self._custom_operation_status_floor = self._custom_operation_status_stamp
            self.registration_refresh_trace.append({
                "event": "registration_refresh_restart_requested",
                "refresh_attempt": refresh_attempt,
                "time": datetime.now(timezone.utc).isoformat(),
                "mode_status_floor": dict(self._mode_status_floor),
                "custom_operation_status_floor": self._custom_operation_status_floor,
            })
            refresh = tools.system(
                "restart",
                cold=False,
                timeout_sec=180.0,
            )
            if not refresh.success:
                raise RuntimeError(
                    "failed to refresh Pi runtime external-mode registrations: "
                    + refresh.message
                )

        raise TimeoutError(
            "canonical PX4 external modes did not become settable in the live "
            "session before HIL preflight"
        )

    def call_intent(self, name: str) -> None:
        client = self.create_client(SetBool, name)
        if not client.wait_for_service(timeout_sec=10.0):
            raise TimeoutError(f"intent service unavailable: {name}")
        future = client.call_async(SetBool.Request(data=True))
        deadline = time.monotonic() + 10.0
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if future.done():
                response = future.result()
                if not response.success:
                    raise RuntimeError(f"intent rejected: {name}: {response.message}")
                return
        raise TimeoutError(f"intent service timed out: {name}")

    def status_snapshot(self) -> dict[str, object]:
        vehicle = self.vehicle
        local = self.local_position
        land = self.land_detected
        return {
            "arming_state": None if vehicle is None else int(vehicle.arming_state),
            "nav_state": None if vehicle is None else int(vehicle.nav_state),
            "failsafe": None if vehicle is None else bool(vehicle.failsafe),
            "landed": None if land is None else bool(land.landed),
            "x": None if local is None else float(local.x),
            "y": None if local is None else float(local.y),
            "z": None if local is None else float(local.z),
        }

    def cleanup_safety_evidence(
        self,
        cleanup_started_monotonic: float,
        *,
        freshness_sec: float = 5.0,
    ) -> dict[str, object]:
        """Return independent, timestamped post-cleanup safety evidence.

        The driver keeps topic caches for ordinary control waits, but a
        cleanup success event must not be justified by a pre-cleanup cached
        ``VehicleStatus``.  Land detection and arming state are evaluated
        independently and both samples must be fresh and received after the
        cleanup phase began.
        """
        now = time.monotonic()

        def sample_evidence(
            *,
            receipt_at: str | None,
            receipt_monotonic: float | None,
            message_timestamp_us: int | None,
            values: dict[str, object],
        ) -> dict[str, object]:
            if receipt_monotonic is None:
                return {
                    "available": False,
                    "message_timestamp_us": message_timestamp_us,
                    "local_receipt_at": receipt_at,
                    "age_sec": None,
                    "fresh": False,
                    "post_cleanup": False,
                    **values,
                }
            age_sec = max(0.0, now - receipt_monotonic)
            return {
                "available": True,
                "message_timestamp_us": message_timestamp_us,
                "local_receipt_at": receipt_at,
                "age_sec": age_sec,
                "fresh": age_sec <= freshness_sec,
                "post_cleanup": receipt_monotonic >= cleanup_started_monotonic,
                **values,
            }

        vehicle = self.vehicle
        land_detected = self.land_detected
        vehicle_evidence = sample_evidence(
            receipt_at=self._vehicle_receipt_at,
            receipt_monotonic=self._vehicle_receipt_monotonic,
            message_timestamp_us=self._vehicle_message_timestamp_us,
            values={
                "arming_state": None if vehicle is None else int(vehicle.arming_state),
                "failsafe": None if vehicle is None else bool(vehicle.failsafe),
            },
        )
        land_evidence = sample_evidence(
            receipt_at=self._land_detected_receipt_at,
            receipt_monotonic=self._land_detected_receipt_monotonic,
            message_timestamp_us=self._land_detected_message_timestamp_us,
            values={
                "landed": None if land_detected is None else bool(land_detected.landed),
            },
        )
        safe = bool(
            vehicle_evidence["available"]
            and vehicle_evidence["fresh"]
            and vehicle_evidence["post_cleanup"]
            and land_evidence["available"]
            and land_evidence["fresh"]
            and land_evidence["post_cleanup"]
            and vehicle_evidence["arming_state"] == VehicleStatus.ARMING_STATE_DISARMED
            and land_evidence["landed"] is True
        )
        return {
            "safe_landed_disarmed": safe,
            "freshness_window_sec": freshness_sec,
            "vehicle_status": vehicle_evidence,
            "land_detected": land_evidence,
        }

    def wait_for_fresh_cleanup_safe_landed_disarmed(
        self,
        cleanup_started_monotonic: float,
        *,
        timeout_sec: float = 10.0,
        freshness_sec: float = 5.0,
    ) -> dict[str, object]:
        """Wait for fresh landed and disarmed samples from the cleanup phase."""
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            evidence = self.cleanup_safety_evidence(
                cleanup_started_monotonic,
                freshness_sec=freshness_sec,
            )
            if evidence["safe_landed_disarmed"]:
                return evidence
        evidence = self.cleanup_safety_evidence(
            cleanup_started_monotonic,
            freshness_sec=freshness_sec,
        )
        raise TimeoutError(
            "no fresh post-cleanup landed/disarmed evidence: "
            + json.dumps(evidence, default=str)
        )

    def open_gripper_and_wait(self, timeout_sec: float = 10.0) -> None:
        """Restore the free-flight payload state before a new ingress.

        A preceding interrupted Reach Cable attempt can leave the simulated
        gripper closed.  That state is deliberately classified as on-cable by
        the maneuver layer and must not leak into the next inspection run.
        """
        client = self.create_client(
            GripperCommand, "/payload/charger_gripper/gripper_command"
        )
        if not client.wait_for_service(timeout_sec=timeout_sec):
            raise TimeoutError("gripper command service unavailable")
        future = client.call_async(
            GripperCommand.Request(
                gripper_command=GripperCommand.Request.GRIPPER_COMMAND_OPEN
            )
        )
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if future.done():
                response = future.result()
                if response.gripper_command_response != GripperCommand.Response.GRIPPER_COMMAND_RESPONSE_SUCCESS:
                    raise RuntimeError(
                        "gripper open command failed: "
                        f"response={response.gripper_command_response}"
                    )
                break
        else:
            raise TimeoutError("gripper open command timed out")
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if (
                self.gripper_status is not None
                and self.gripper_status.gripper_status
                == GripperStatus.GRIPPER_STATUS_OPEN
            ):
                return
        raise TimeoutError("gripper did not report open after open command")

    def land_and_wait(
        self,
        tools: DroneAgentTools,
        timeout_sec: float = 180.0,
    ) -> None:
        """Interrupt autonomous ownership with Hold, then prove a safe landing."""
        deadline = time.monotonic() + timeout_sec
        self.native_flight_command("px4.hold")
        self.wait_native_mission_owner_cleared(
            timeout_sec=max(0.05, deadline - time.monotonic())
        )
        vehicle = self.wait_vehicle()
        if vehicle.arming_state != VehicleStatus.ARMING_STATE_DISARMED:
            state = _call_runtime_api(self, self.runtime_client.vehicle_status)
            air_field = (state.get("telemetry_fields") or {}).get("in_air") or {}
            confirmed_grounded = (
                state.get("freshness") == "fresh"
                and state.get("in_air") is False
                and air_field.get("value") is False
                and air_field.get("freshness") == "fresh"
                and air_field.get("source_availability") == "available"
                and air_field.get("disagreement") is not True
            )
            # Hold must be visible to the native command gate before Land.
            # A failed takeoff can leave an armed vehicle on the ground. The
            # native API has no disarm command, and SITL disables preflight
            # auto-disarm; report this instead of waiting for impossible flight.
            if confirmed_grounded:
                raise RuntimeError(
                    "Cleanup stopped autonomous ownership, but PX4 remains armed on the ground; "
                    "the native API cannot disarm this state. Stop the virtual stack."
                )
            else:
                self.wait_native_vehicle_state(
                    {"armed": True, "in_air": True},
                    timeout_sec=max(0.05, deadline - time.monotonic()),
                )
                self.native_flight_command("px4.land")
        while rclpy.ok() and time.monotonic() < deadline:
            self.wait_vehicle(timeout_sec=min(1.0, max(0.1, deadline - time.monotonic())))
            if (
                self.land_detected is not None and self.land_detected.landed
                and self.vehicle is not None
                and self.vehicle.arming_state == VehicleStatus.ARMING_STATE_DISARMED
            ):
                self.wait_native_disarmed(
                    timeout_sec=max(0.05, deadline - time.monotonic())
                )
                return
        raise TimeoutError("PX4 did not land and auto-disarm after native Hold/Land cleanup")

    def wait_mode(
        self, name: str, predicate, timeout_sec: float = 600.0,
        *, failed_predecessor: str | None = None,
    ) -> dict[str, object]:
        """Wait for a fresh mode lifecycle, never a stale terminal status.

        Mode status is published periodically and can retain the terminal
        result from the preceding mission generation while the next mode is
        being activated.  Treating that latched result as a failure makes the
        driver stop even though the mission continues normally.  Require an
        observed active state before accepting either a terminal success or a
        terminal failure for this wait.
        """
        deadline = time.monotonic() + timeout_sec
        saw_update = False
        saw_active = False
        status_floor = self._mode_status_floor.get(name, (-1, -1))
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            # The caller has already observed this predecessor active in the
            # current cycle. Its failure prevents the successor from ever
            # activating; land through finally instead of hovering until the
            # successor's ten-minute timeout.
            predecessor = self.mode_status.get(failed_predecessor) if failed_predecessor else None
            if predecessor and predecessor.get("tree_finished") and not predecessor.get("tree_success"):
                raise RuntimeError(f"{failed_predecessor} failed before {name}: {predecessor}")
            value = self.mode_status.get(name)
            if value is None:
                continue
            stamp = value.get("_stamp")
            if isinstance(stamp, (tuple, list)) and len(stamp) == 2:
                if (int(stamp[0]), int(stamp[1])) <= status_floor:
                    continue
            saw_update = True
            if value.get("active"):
                saw_active = True
                self._mode_status_floor.pop(name, None)
            if saw_active and predicate(value):
                return value
            if saw_active and value.get("tree_finished") and not value.get("tree_success"):
                raise RuntimeError(f"{name} failed: {value}")
        raise TimeoutError(f"timed out waiting for {name} status")

    def wait_mode_id(self, name: str, timeout_sec: float = 20.0) -> int:
        """Read the PX4 external-mode ID assigned by the live mode status.

        Registration order is not a contract: restarting the Pi/runtime can
        assign a different EXTERNALn slot. Selecting a stale numeric ID is
        rejected by PX4 even though the mission mode itself is healthy.
        """
        deadline = time.monotonic() + timeout_sec
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if name == "custom_operation" and self.custom_operation_mode_id is not None:
                return self.custom_operation_mode_id
            value = self.mode_status.get(name)
            if isinstance(value, dict):
                mode_id = value.get("mode_id")
                if isinstance(mode_id, int) and 23 <= mode_id <= 30:
                    return mode_id
        raise TimeoutError(f"mode {name} did not publish a valid PX4 mode_id")

    def wait_charging_evidence(self, timeout_sec: float = 30.0) -> dict[str, object]:
        """Prove fresh simulated latch and charging power before requesting leave."""
        started = time.monotonic()
        while rclpy.ok() and time.monotonic() - started < timeout_sec:
            rclpy.spin_once(self, timeout_sec=0.2)
            now = time.monotonic()
            if not all(
                key in self.charging_samples
                and self.charging_samples[key][0] >= started
                and now - self.charging_samples[key][0] <= 2.0
                for key in ("charger_status", "charging_power", "sim_state")
            ):
                continue
            status = self.charging_samples["charger_status"][1]
            power = float(self.charging_samples["charging_power"][1])
            sim_state = str(self.charging_samples["sim_state"][1])
            if status == ChargerStatus.CHARGER_STATUS_CHARGING and math.isfinite(power) and power > 0 and "latched=1" in sim_state:
                return {"charger_status": status, "charging_power_w": power, "sim_state": sim_state,
                        "receipt_at": datetime.now(timezone.utc).isoformat()}
        raise TimeoutError("no fresh latched payload and positive charging-power evidence")

    def dwell_mode(
        self,
        name: str,
        duration_sec: float,
        *,
        require_charging_evidence: bool = False,
        charging_invalid_grace_sec: float = 2.0,
    ) -> list[dict[str, object]]:
        """Dwell in a live phase, recording short fresh charging-state gaps."""
        if duration_sec < 0:
            raise ValueError("dwell duration must be non-negative")
        if charging_invalid_grace_sec < 0:
            raise ValueError("charging invalid grace must be non-negative")
        if duration_sec == 0:
            return []
        deadline = time.monotonic() + duration_sec
        invalid_since: float | None = None
        invalid_first: dict[str, object] | None = None
        invalid_last: dict[str, object] | None = None
        invalid_spans: list[dict[str, object]] = []
        while rclpy.ok():
            before_spin = time.monotonic()
            if before_spin >= deadline and invalid_since is None:
                break
            remaining = max(0.01, deadline - before_spin)
            rclpy.spin_once(self, timeout_sec=min(0.2, remaining))
            value = self.mode_status.get(name)
            if not isinstance(value, dict):
                raise RuntimeError(f"{name} status disappeared during dwell")
            if value.get("tree_finished") and not value.get("tree_success"):
                raise RuntimeError(f"{name} failed during dwell: {value}")
            if not value.get("active") or not value.get("tree_running"):
                raise RuntimeError(f"{name} left its running phase during dwell: {value}")
            if require_charging_evidence:
                now = time.monotonic()
                samples = self.charging_samples
                ages = {
                    key: None if key not in samples else round(now - samples[key][0], 3)
                    for key in ("charger_status", "charging_power", "sim_state")
                }
                battery = self.battery_status_sample
                battery_age = None if battery is None else round(now - battery[0], 3)
                status = samples.get("charger_status", (None, None))[1]
                power_value = samples.get("charging_power", (None, None))[1]
                sim_state_value = samples.get("sim_state", (None, None))[1]
                power = None if power_value is None else float(power_value)
                sim_state = "" if sim_state_value is None else str(sim_state_value)
                latched = "latched=1" in sim_state
                snapshot: dict[str, object] = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "charger_status": status,
                    "charging_power_w": power,
                    "latched": latched,
                    "sim_state": sim_state,
                    "battery_remaining": (
                        None if battery is None else float(battery[1].remaining)
                    ),
                    "battery_voltage_v": (
                        None if battery is None else float(battery[1].voltage_v)
                    ),
                    "source_ages_sec": {**ages, "battery_status": battery_age},
                }
                stale = any(
                    age is None or age < 0 or age > 2.0
                    for age in ages.values()
                )
                if stale:
                    raise RuntimeError(
                        "charging telemetry became stale during dwell: "
                        f"diagnostic={snapshot}"
                    )
                charging = (
                    status == ChargerStatus.CHARGER_STATUS_CHARGING
                    and power is not None
                    and math.isfinite(power)
                    and power > 0
                    and latched
                )
                fully_charged = (
                    status == ChargerStatus.CHARGER_STATUS_FULLY_CHARGED
                    and power is not None
                    and math.isfinite(power)
                    and power >= 0
                    and latched
                )
                if not charging and not fully_charged:
                    reasons = []
                    if not latched:
                        reasons.append("unlatched")
                    if status not in (
                        ChargerStatus.CHARGER_STATUS_CHARGING,
                        ChargerStatus.CHARGER_STATUS_FULLY_CHARGED,
                    ):
                        reasons.append("unsupported_charger_status")
                    elif not math.isfinite(power) or (
                        status == ChargerStatus.CHARGER_STATUS_CHARGING and power <= 0
                    ) or (
                        status == ChargerStatus.CHARGER_STATUS_FULLY_CHARGED and power < 0
                    ):
                        reasons.append("invalid_charging_power")
                    snapshot["invalid_reasons"] = reasons
                    if invalid_since is None:
                        invalid_since = now
                        invalid_first = snapshot
                    invalid_last = snapshot
                    invalid_duration = now - invalid_since
                    if invalid_duration > charging_invalid_grace_sec:
                        raise RuntimeError(
                            "charging evidence remained invalid beyond grace during dwell: "
                            f"invalid_duration_sec={invalid_duration:.3f} "
                            f"first={invalid_first} last={invalid_last}"
                        )
                    continue
                if invalid_since is not None:
                    invalid_spans.append({
                        "started_at": invalid_first["timestamp"] if invalid_first else None,
                        "ended_at": snapshot["timestamp"],
                        "duration_sec": round(now - invalid_since, 3),
                        "first": invalid_first,
                        "last_invalid": invalid_last,
                        "recovered": snapshot,
                    })
                    invalid_since = None
                    invalid_first = None
                    invalid_last = None
                if now >= deadline:
                    return invalid_spans
        if not rclpy.ok():
            raise RuntimeError(f"ROS stopped during {name} dwell")
        return invalid_spans

    @staticmethod
    def _receipt_status(receipt: dict[str, object]) -> tuple[tuple[int, int], dict[str, object]] | None:
        stamp = receipt.get("stamp")
        if not isinstance(stamp, (tuple, list)) or len(stamp) != 2:
            return None
        try:
            value = json.loads(str(receipt.get("data", "")))
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        return (int(stamp[0]), int(stamp[1])), value

    def _inspection_auto_transition_evidence(
        self,
        running_stamp: tuple[int, int],
        receipt_index: int,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        """Require new Inspection success followed by new active Reach status."""
        successes: list[tuple[tuple[int, int], dict[str, object]]] = []
        reaches: list[tuple[tuple[int, int], dict[str, object]]] = []
        reach_floor = self._mode_status_floor.get("reach_cable", (-1, -1))
        for receipt in self.mode_status_receipts[receipt_index:]:
            parsed = Driver._receipt_status(receipt)
            if parsed is None:
                continue
            stamp, value = parsed
            if stamp <= running_stamp:
                continue
            if receipt.get("mode") == "inspection_demo":
                if value.get("tree_finished") and not value.get("tree_success"):
                    raise RuntimeError(f"inspection_demo failed during transition: {value}")
                if value.get("tree_finished") and value.get("tree_success"):
                    successes.append((stamp, value))
            elif (
                receipt.get("mode") == "reach_cable"
                and stamp > reach_floor
                and value.get("active")
            ):
                reaches.append((stamp, value))
        for success_stamp, success_status in sorted(successes, key=lambda item: item[0]):
            for reach_stamp, reach_status in reaches:
                if reach_stamp > success_stamp:
                    return success_status, reach_status
        return None

    def _inspection_success_since(
        self,
        running_stamp: tuple[int, int],
        receipt_index: int,
    ) -> dict[str, object] | None:
        for receipt in self.mode_status_receipts[receipt_index:]:
            if receipt.get("mode") != "inspection_demo":
                continue
            parsed = Driver._receipt_status(receipt)
            if parsed is None:
                continue
            stamp, value = parsed
            if stamp <= running_stamp:
                continue
            if value.get("tree_finished") and not value.get("tree_success"):
                raise RuntimeError(f"inspection_demo failed during transition: {value}")
            if value.get("tree_finished") and value.get("tree_success"):
                return value
        return None

    def _inspection_battery_evidence(self) -> dict[str, object]:
        sample = self.battery_status_sample
        now = time.monotonic()
        age = None if sample is None else now - sample[0]
        voltage = None if sample is None else float(sample[1].voltage_v)
        remaining = None if sample is None else float(sample[1].remaining)
        fresh = age is not None and 0 <= age <= 2.0
        return {
            "battery_voltage_v": voltage if fresh else None,
            "battery_remaining": remaining if fresh else None,
            "battery_age_sec": None if age is None else round(age, 3),
            "battery_fresh": fresh,
        }

    def wait_for_inspection_auto_transition(
        self,
        running_status: dict[str, object],
        *,
        receipt_index: int,
        timeout_sec: float,
    ) -> InspectionTransitionDecision | None:
        running_stamp = Driver._mode_stamp(running_status)
        floor = self._mode_status_floor.get("inspection_demo", (-1, -1))
        if (
            not running_status.get("active")
            or not running_status.get("tree_running")
            or running_stamp is None
            or running_stamp <= floor
        ):
            raise RuntimeError(f"Inspection Demo lacks fresh current-generation running status: {running_status}")
        deadline = time.monotonic() + timeout_sec
        last_api_error: str | None = None
        last_api_phase: str | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            evidence = self._inspection_auto_transition_evidence(running_stamp, receipt_index)
            if evidence is not None:
                inspection_status, reach_status = evidence
                try:
                    mission_key, mission_state = self.read_fresh_native_mission_phase()
                    last_api_error = None
                    last_api_phase = mission_key
                except RuntimeError as exc:
                    message = str(exc)
                    if not (
                        message.startswith("Cannot verify active mission phase from fresh Runtime API state:")
                        or message.startswith("Fresh Runtime API mission phase has no mode key:")
                    ):
                        raise
                    last_api_error = message
                    mission_key = ""
                    mission_state = {}
                if mission_key == "reach_cable":
                    return InspectionTransitionDecision(
                        "automatic", inspection_status, reach_status, mission_state,
                        self._inspection_battery_evidence(),
                    )
                if mission_key and mission_key != "inspection_demo":
                    raise RuntimeError(
                        "fresh Inspection success and Reach activation disagree with Runtime API: "
                        f"active_phase={mission_key} mission_state={mission_state}"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            rclpy.spin_once(self, timeout_sec=min(0.2, remaining))
        if timeout_sec > 0.2 and (last_api_error or last_api_phase == "inspection_demo"):
            evidence = self._inspection_auto_transition_evidence(running_stamp, receipt_index)
            if evidence is not None:
                detail = last_api_error or f"Runtime API remained at active phase {last_api_phase!r}"
                raise TimeoutError(
                    "ROS receipts prove successful Inspection then active Reach, but the fresh "
                    f"Runtime API phase did not converge to Reach within {timeout_sec:.3f}s: {detail}"
                )
        return None

    def advance_inspection_to_reach(
        self,
        running_status: dict[str, object],
        dwell_sec: float,
        *,
        timeout_sec: float = 600.0,
        stop_at: float | None = None,
        automatic_only: bool = False,
    ) -> InspectionTransitionDecision | None:
        """Honor canonical auto-recharge or issue manual recharge after dwell."""
        if dwell_sec < 0:
            raise ValueError("inspection dwell must be non-negative")
        running_stamp = Driver._mode_stamp(running_status)
        if running_stamp is None:
            raise RuntimeError(f"Inspection Demo running status has no source stamp: {running_status}")
        # Stamp floors isolate this generation while scanning all receipts
        # also catches a transition delivered between wait_mode() and here.
        receipt_index = 0
        deadline = time.monotonic() + dwell_sec
        while rclpy.ok() and time.monotonic() < deadline and (stop_at is None or time.monotonic() < stop_at):
            limit = min(deadline, stop_at) if stop_at is not None else deadline
            rclpy.spin_once(self, timeout_sec=min(0.2, max(0.01, limit - time.monotonic())))
            automatic = self.wait_for_inspection_auto_transition(
                running_status, receipt_index=receipt_index, timeout_sec=0.001
            )
            if automatic is not None:
                return automatic
            if self._inspection_success_since(running_stamp, receipt_index) is not None:
                automatic = self.wait_for_inspection_auto_transition(
                    running_status, receipt_index=receipt_index, timeout_sec=10.0
                )
                if automatic is None:
                    raise TimeoutError(
                        "Inspection Demo succeeded but fresh Reach activation/Runtime API phase "
                        "did not converge within 10 seconds"
                    )
                return automatic
            current = self.mode_status.get("inspection_demo")
            if not isinstance(current, dict) or not current.get("active") or not current.get("tree_running"):
                raise RuntimeError(f"inspection_demo left its running phase without proven auto-recharge: {current}")
        automatic = self.wait_for_inspection_auto_transition(
            running_status, receipt_index=receipt_index, timeout_sec=0.001
        )
        if automatic is not None:
            return automatic
        if self._inspection_success_since(running_stamp, receipt_index) is not None:
            automatic = self.wait_for_inspection_auto_transition(
                running_status, receipt_index=receipt_index, timeout_sec=10.0
            )
            if automatic is None:
                raise TimeoutError(
                    "Inspection Demo succeeded but fresh Reach activation/Runtime API phase "
                    "did not converge within 10 seconds"
                )
            return automatic
        if stop_at is not None and time.monotonic() >= stop_at:
            return None
        if automatic_only:
            raise TimeoutError(
                f"Inspection did not trigger automatic recharge within {dwell_sec:.1f}s"
            )
        mission_key, mission_state = self.read_fresh_native_mission_phase()
        if mission_key != "inspection_demo":
            # Only fresh current-generation Inspection success followed by
            # Reach activation can explain an automatic phase advance.
            automatic = self.wait_for_inspection_auto_transition(
                running_status, receipt_index=receipt_index, timeout_sec=min(3.0, timeout_sec)
            )
            if automatic is not None:
                return automatic
            raise RuntimeError(
                f"unexpected mission phase before manual recharge: {mission_key}; {mission_state}"
            )
        command_receipt_index = receipt_index
        try:
            self.native_flight_command("mission.recharge_now")
        except RuntimeError:
            automatic = self.wait_for_inspection_auto_transition(
                running_status,
                receipt_index=command_receipt_index,
                timeout_sec=min(10.0, timeout_sec),
            )
            if automatic is not None:
                return automatic
            raise
        reach_status = self.wait_mode("reach_cable", lambda value: bool(value.get("active")), timeout_sec=timeout_sec)
        mission_state = self.wait_native_mission_phase("reach_cable", timeout_sec=timeout_sec)
        return InspectionTransitionDecision(
            "manual", running_status, reach_status, mission_state,
            self._inspection_battery_evidence(),
        )

    def monitor_inspection_until(
        self,
        running_status: dict[str, object],
        deadline: float,
    ) -> InspectionTransitionDecision | None:
        """Wait through the duration guard, observing canonical auto-recharge."""
        receipt_index = 0
        while rclpy.ok() and time.monotonic() < deadline:
            decision = self.wait_for_inspection_auto_transition(
                running_status,
                receipt_index=receipt_index,
                timeout_sec=min(0.2, max(0.01, deadline - time.monotonic())),
            )
            if decision is not None:
                return decision
            running_stamp = Driver._mode_stamp(running_status)
            if running_stamp is None:
                raise RuntimeError(f"Inspection Demo running status has no source stamp: {running_status}")
            if self._inspection_success_since(running_stamp, receipt_index) is not None:
                decision = self.wait_for_inspection_auto_transition(
                    running_status, receipt_index=receipt_index, timeout_sec=10.0
                )
                if decision is None:
                    raise TimeoutError(
                        "Inspection Demo succeeded in duration guard but Reach activation/Runtime API "
                        "did not converge within 10 seconds"
                    )
                return decision
            current = self.mode_status.get("inspection_demo")
            if isinstance(current, dict) and current.get("tree_finished") and not current.get("tree_success"):
                raise RuntimeError(f"inspection_demo failed during duration guard: {current}")
            if not (
                isinstance(current, dict)
                and current.get("active")
                and current.get("tree_running")
            ):
                raise RuntimeError(
                    f"inspection_demo left its running phase without proven auto-recharge: {current}"
                )
            if time.monotonic() >= deadline:
                return None
            rclpy.spin_once(self, timeout_sec=min(0.2, max(0.01, deadline - time.monotonic())))
        return None

    def wait_until_fully_charged(
        self,
        timeout_sec: float,
        *,
        allow_leave_cable: bool = False,
    ) -> dict[str, object]:
        """Require fresh full-charge, latch, and PX4 battery evidence."""
        deadline = time.monotonic() + timeout_sec
        entry_deadline = min(deadline, time.monotonic() + 10.0)
        entry_converged = False
        latest: dict[str, object] = {}
        while rclpy.ok() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            rclpy.spin_once(self, timeout_sec=min(0.2, max(0.01, remaining)))
            mode = self.mode_status.get("cable_charging")
            if not isinstance(mode, dict):
                raise RuntimeError("Cable Charging status disappeared while waiting for full charge")
            if mode.get("tree_finished") and not mode.get("tree_success"):
                raise RuntimeError(f"Cable Charging failed while waiting for full charge: {mode}")

            now = time.monotonic()
            samples = self.charging_samples
            ages = {
                key: None if key not in samples else round(now - samples[key][0], 3)
                for key in ("charger_status", "charging_power", "sim_state")
            }
            if not all(age is not None and age <= 2.0 for age in ages.values()):
                raise RuntimeError(f"charging telemetry became stale before full charge: ages_sec={ages}")
            charger_status = samples["charger_status"][1]
            power = float(samples["charging_power"][1])
            sim_state = str(samples["sim_state"][1])
            if "latched=1" not in sim_state:
                raise RuntimeError(
                    "charger latch was lost before full charge: "
                    f"charger_status={charger_status} power_w={power} sim_state={sim_state}"
                )
            charging = (
                charger_status == ChargerStatus.CHARGER_STATUS_CHARGING
                and math.isfinite(power)
                and power > 0
            )
            fully_charged = (
                charger_status == ChargerStatus.CHARGER_STATUS_FULLY_CHARGED
                and math.isfinite(power)
                and power >= 0
            )
            if not charging and not fully_charged:
                raise RuntimeError(
                    "charger stopped in an unsupported state before full charge: "
                    f"charger_status={charger_status} power_w={power} sim_state={sim_state}"
                )

            battery = self.battery_status_sample
            battery_age = None if battery is None else round(now - battery[0], 3)
            latest = {
                "charger_status": charger_status,
                "charging_power_w": power,
                "sim_state": sim_state,
                "battery_remaining": None if battery is None else float(battery[1].remaining),
                "battery_voltage_v": None if battery is None else float(battery[1].voltage_v),
                "battery_age_sec": battery_age,
            }
            cable_running = bool(mode.get("active") and mode.get("tree_running"))
            cable_completed = bool(mode.get("tree_finished") and mode.get("tree_success"))
            if not fully_charged and cable_running and entry_converged:
                continue
            phase, phase_state = self.read_fresh_native_mission_phase()
            if (
                not entry_converged
                and mode.get("active")
                and (not mode.get("tree_finished") or cable_completed)
            ):
                # Activation, tree startup, and Runtime API delivery are separate
                # callbacks. A just-activated charging mode can still expose the
                # predecessor's successful terminal flags. Treat those flags as
                # pending only during the initial bounded Reach -> Charging
                # handoff; convergence still requires current running status and
                # the fresh Runtime API phase. Telemetry and failure checks above
                # remain strict, and success flags never prove full charge.
                if phase == "cable_charging" and cable_running:
                    entry_converged = True
                elif phase in ("reach_cable", "cable_charging"):
                    if time.monotonic() >= entry_deadline:
                        raise TimeoutError(
                            "Cable Charging entry did not converge within 10 seconds: "
                            f"phase={phase!r} cable_status={mode}"
                        )
                    continue
            if phase == "cable_charging":
                if not cable_running:
                    if cable_completed:
                        # The success callback and Runtime API phase can arrive
                        # in either order. Keep waiting for fresh Leave evidence.
                        continue
                    raise RuntimeError(
                        f"Cable Charging left its running phase before full charge: {mode}"
                    )
            elif phase == "leave_cable" and allow_leave_cable:
                if not fully_charged:
                    continue
            elif phase == "inspection_demo" and allow_leave_cable:
                if not fully_charged:
                    continue
                try:
                    self.wait_for_fresh_leave_cable_status(
                        timeout_sec=min(0.2, max(0.01, deadline - time.monotonic()))
                    )
                except TimeoutError:
                    continue
            else:
                raise RuntimeError(
                    "Unexpected mission phase while verifying full charge: "
                    f"phase={phase!r} state={phase_state} cable_status={mode}"
                )
            if (
                fully_charged
                and battery is not None
                and battery_age is not None
                and battery_age <= 2.0
                and math.isfinite(float(battery[1].remaining))
                and float(battery[1].remaining) >= 0.98
            ):
                latest["mission_phase"] = phase
                return latest
        raise TimeoutError(
            f"charger did not reach fresh fully-charged state within {timeout_sec:.1f}s: {latest}"
        )


def initialize_driver_ros() -> None:
    # The default rclpy SIGINT handler shuts down its context before finally
    # can command landing or receive fresh telemetry. Keep Python's normal
    # KeyboardInterrupt behavior and let tools.close() shut ROS down afterwards.
    from rclpy.signals import SignalHandlerOptions
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if not rclpy.ok():
        rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument(
        "--stop-after-cable-charging",
        action="store_true",
        help=(
            "Stop after Reach Cable succeeds and Cable Charging becomes active; "
            "the existing cleanup path still lands and disarms the vehicle."
        ),
    )
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument(
        "--automatic-cycles", action="store_true",
        help="Require battery-triggered recharge and automatic Leave after full charge; never command either transition.",
    )
    parser.add_argument(
        "--automatic-recharge-timeout-sec", type=float, default=600.0,
        help="Maximum healthy Inspection wait for automatic recharge in automatic-cycle mode.",
    )
    parser.add_argument(
        "--inspection-dwell-sec",
        type=float,
        default=0.0,
        help="Minimum time to remain in a healthy Inspection phase before recharge.",
    )
    parser.add_argument(
        "--charging-dwell-sec",
        type=float,
        default=0.0,
        help="Minimum time to remain in Cable Charging with fresh latched charge evidence.",
    )
    parser.add_argument(
        "--charge-until-full-timeout-sec",
        type=float,
        default=0.0,
        help="Wait up to this long for latched FULLY_CHARGED and fresh PX4 battery evidence.",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=0.0,
        help="Continue canonical cycles until this wall-clock duration, then land safely.",
    )
    parser.add_argument(
        "--deadline-drain-guard-sec",
        type=float,
        default=180.0,
        help=(
            "When a duration run has this much time remaining, finish the current "
            "inspection phase and wait for the exact deadline instead of starting "
            "another recharge cycle."
        ),
    )
    parser.add_argument(
        "--cleanup-freshness-sec",
        type=float,
        default=5.0,
        help="Maximum local receipt age for each post-cleanup PX4 safety sample.",
    )
    parser.add_argument(
        "--custom-mode-id",
        type=int,
        default=(
            int(os.environ["III_HIL_CUSTOM_OPERATION_NAV_STATE"])
            if os.environ.get("III_HIL_CUSTOM_OPERATION_NAV_STATE")
            else None
        ),
        help=(
            "PX4 nav-state assigned to Custom Operation; when omitted, discover "
            "the live registration from /mission/custom_operation/status."
        ),
    )
    parser.add_argument(
        "--inspection-mode-id",
        type=int,
        default=None,
        help=(
            "PX4 nav-state assigned to Inspection Demo; when omitted, discover "
            "the live registration from /mission/modes/inspection_demo/status."
        ),
    )
    args = parser.parse_args()
    if args.inspection_dwell_sec < 0:
        parser.error("--inspection-dwell-sec must be non-negative")
    if args.charging_dwell_sec < 0:
        parser.error("--charging-dwell-sec must be non-negative")
    if args.charge_until_full_timeout_sec < 0:
        parser.error("--charge-until-full-timeout-sec must be non-negative")
    if args.automatic_recharge_timeout_sec <= 0:
        parser.error("--automatic-recharge-timeout-sec must be positive")
    if args.automatic_cycles and args.charge_until_full_timeout_sec <= 0:
        parser.error("--automatic-cycles requires --charge-until-full-timeout-sec")
    if args.automatic_cycles and args.charging_dwell_sec != 0:
        parser.error("--automatic-cycles requires zero --charging-dwell-sec; full charge owns the transition")
    if args.duration_sec < 0:
        parser.error("--duration-sec must be non-negative")
    if args.deadline_drain_guard_sec < 0:
        parser.error("--deadline-drain-guard-sec must be non-negative")
    if args.cleanup_freshness_sec <= 0:
        parser.error("--cleanup-freshness-sec must be positive")
    args.artifact_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the target before starting ROS or creating flight tools.
    runtime_client = RuntimeApiClient.from_env()
    runtime_client.timeout_seconds = min(
        runtime_client.timeout_seconds, _RUNTIME_API_REQUEST_TIMEOUT_SEC
    )
    identity = runtime_client.identity()
    api_state = runtime_client.vehicle_status()
    api_endpoint = api_state.get("latest", {}).get("command_transport", {}).get("endpoint")
    selected_endpoint, _ = select_px4_target(
        identity, api_endpoint, os.environ.get("III_PX4_SYSTEM_ADDRESS")
    )
    initialize_driver_ros()
    tools = DroneAgentTools(
        node_name="hil_inspection_ingress",
        artifact_dir=args.artifact_dir,
        px4_system_address=selected_endpoint,
    )
    driver = Driver()
    driver.px4_system_address = selected_endpoint
    events: list[dict[str, object]] = []
    events_path = args.artifact_dir.joinpath("driver_events.json")

    def record(event: dict[str, object]) -> None:
        events.append(event)
        events_path.write_text(
            json.dumps(events, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (args.artifact_dir / "native_command_receipts.json").write_text(
            json.dumps(driver.native_command_receipts, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (args.artifact_dir / "native_state_receipts.json").write_text(
            json.dumps(driver.native_state_receipts, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (args.artifact_dir / "mode_status_subscriber_receipts.json").write_text(
            json.dumps(driver.mode_status_receipts, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        (args.artifact_dir / "registration_refresh_trace.json").write_text(
            json.dumps(driver.registration_refresh_trace, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    try:
        registered_modes = driver.ensure_external_modes_registered(tools)
        record({"event": "external_modes_ready", "mode_ids": registered_modes})
        driver.open_gripper_and_wait()
        record({"event": "gripper_open_preflight"})
        driver.prepare_airborne(tools)
        record({"event": "airborne_hold_preflight", "status": driver.status_snapshot()})
        # Exercise the same acknowledged activation gate as native GC/CLI.
        # Retain the independently discovered ROS mode IDs as postconditions.
        custom_mode_id = args.custom_mode_id
        if custom_mode_id is None:
            custom_mode_id = registered_modes["custom_operation"]
        elif custom_mode_id != registered_modes["custom_operation"]:
            raise RuntimeError(
                f"requested CustomOperation mode {custom_mode_id} is stale; "
                f"live PX4 registration is {registered_modes['custom_operation']}"
            )
        driver.wait_native_vehicle_state({"armed": True, "in_air": True})
        driver.native_flight_command("custom_operation.activate")
        driver.wait_nav_state(custom_mode_id)
        activation_request_id = driver.native_command_receipts[-1]["response"]["request_id"]
        driver.wait_native_command_result(activation_request_id)
        record({"event": "custom_operation_active", "mode_id": custom_mode_id, "status": driver.status_snapshot()})

        started = tools.fly_to_fixture(
            "low_entry_side",
            activate_custom_operation=False,
            # Pi PX4 odometry owns dynamic world->drone TF in HIL; workstation
            # ground-truth publishes cable and sensor statics only.
            use_cable_aware_if_overview_present=True,
            send_timeout_sec=15.0,
            mapping_timeout_sec=30.0,
            gazebo_timeout_sec=8.0,
            tf_timeout_sec=5.0,
        )
        if not started.success:
            raise RuntimeError(started.message)
        record({"event": "ingress_started", "data": started.data, "status": driver.status_snapshot()})
        result = tools.wait_operation_goal(
            started.data["goal_id"],
            max_wait_sec=90.0,
            no_feedback_timeout_sec=30.0,
            allow_no_feedback=False,
        )
        record({"event": "ingress_result", "success": result.success,
                "message": result.message, "data": result.data,
                "status": driver.status_snapshot()})
        if not result.success or result.data.get("state") != "succeeded":
            raise RuntimeError(f"ingress did not succeed: {result.message}; {result.data}")
        record({"event": "ingress_succeeded", "data": result.data, "status": driver.status_snapshot()})

        # Preserve the custom-operation setpoint/frame through the handoff.
        # Unexpected disarm is a failed ingress, so stop instead of re-arming.
        driver.ensure_armed_handoff()
        record({"event": "airborne_handoff_preserved", "status": driver.status_snapshot()})

        if args.stage_only:
            return 0

        driver.clear_mode_status("inspection_demo", "reach_cable", "cable_charging", "leave_cable")
        inspection_mode_id = args.inspection_mode_id
        if inspection_mode_id is None:
            inspection_mode_id = registered_modes["inspection_demo"]
        elif inspection_mode_id != registered_modes["inspection_demo"]:
            raise RuntimeError(
                f"requested Inspection Demo mode {inspection_mode_id} is stale; "
                f"live PX4 registration is {registered_modes['inspection_demo']}"
            )
        driver.activate_inspection_from_ingress(inspection_mode_id)
        # Do not enqueue the first recharge intent in the tiny PX4 mode
        # handoff window between onActivate() and the behavior-tree worker
        # becoming live.  The mode status publisher announces `active` before
        # StartExecution() has finished; accepting an intent in that window
        # makes repeated HIL cycles race tree construction and teardown.
        inspection_status = driver.wait_mode(
            "inspection_demo",
            lambda value: bool(value.get("active")) and bool(value.get("tree_running")),
            timeout_sec=20.0,
        )
        time.sleep(1.0)
        record({"event": "inspection_active"})
        deadline = time.monotonic() + args.duration_sec if args.duration_sec else None

        # Explicit developer intents exercise every canonical transition.  The
        # status/evidence observer, not this driver, decides whether each mode
        # actually completed successfully.
        cycle = 0
        while deadline is None or time.monotonic() < deadline:
            if deadline is None and cycle >= args.cycles:
                break
            transition = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if remaining <= args.deadline_drain_guard_sec:
                    record({
                        "event": "inspection_duration_drain_started",
                        "remaining_sec": max(0.0, remaining),
                    })
                    transition = driver.monitor_inspection_until(inspection_status, deadline)
                    if transition is None:
                        break
            if transition is None:
                transition = driver.advance_inspection_to_reach(
                    inspection_status,
                    args.automatic_recharge_timeout_sec if args.automatic_cycles else args.inspection_dwell_sec,
                    stop_at=deadline,
                    automatic_only=args.automatic_cycles,
                )
                if transition is None:
                    break
            cycle += 1
            record({
                "event": (
                    "inspection_auto_recharge_observed"
                    if transition.transition == "automatic"
                    else "inspection_recharge_commanded"
                ),
                "cycle": cycle,
                **transition._asdict(),
            })
            record({"event": "reach_cable_active", "cycle": cycle, "status": transition.reach_status})
            driver.wait_mode(
                "cable_charging", lambda value: bool(value.get("active")),
                failed_predecessor="reach_cable",
            )
            record({"event": "cable_charging_active", "cycle": cycle})
            record({"event": "charging_power_verified", "cycle": cycle,
                    "evidence": driver.wait_charging_evidence()})
            charging_dwell_diagnostics = driver.dwell_mode(
                "cable_charging",
                args.charging_dwell_sec,
                require_charging_evidence=True,
            )
            if charging_dwell_diagnostics:
                record({
                    "event": "charging_dwell_transient_invalid_recovered",
                    "cycle": cycle,
                    "invalid_spans": charging_dwell_diagnostics,
                })
            full_evidence = None
            if args.charge_until_full_timeout_sec > 0:
                full_evidence = driver.wait_until_fully_charged(
                    args.charge_until_full_timeout_sec,
                    allow_leave_cable=True,
                )
                record({
                    "event": "charging_full_verified",
                    "cycle": cycle,
                    "evidence": full_evidence,
                })
            if args.stop_after_cable_charging:
                return 0
            # Full charging can advance the canonical next_mode before the
            # optional manual intent reaches Runtime API. The advance helper
            # accepts that only with fresh full-charge and Leave evidence.
            leave_transition = driver.advance_after_charging(
                full_charge_evidence=full_evidence,
                automatic_only=args.automatic_cycles,
            )
            if leave_transition["transition"] == "automatic":
                record({
                    "event": "leave_cable_auto_transition",
                    "cycle": cycle,
                    "mission_state": leave_transition["mission_state"],
                    "full_charge_evidence": leave_transition["full_charge_evidence"],
                })
            else:
                record({
                    "event": "leave_cable_commanded",
                    "cycle": cycle,
                    "mission_state": leave_transition["mission_state"],
                })
            leave_status = driver.wait_leave_cable_succeeded()
            record({"event": "leave_cable_active", "cycle": cycle})
            record({"event": "leave_cable_succeeded", "cycle": cycle, "status": leave_status})
            driver.clear_mode_status("inspection_demo", "reach_cable", "cable_charging", "leave_cable")
            inspection_status = driver.wait_mode(
                "inspection_demo",
                lambda value: bool(value.get("active")) and bool(value.get("tree_running")),
            )
            time.sleep(1.0)
            record({"event": "inspection_resumed", "cycle": cycle})
        if deadline is not None:
            cleanup_started_monotonic = time.monotonic()
            driver.land_and_wait(tools)
            cleanup_evidence = driver.wait_for_fresh_cleanup_safe_landed_disarmed(
                cleanup_started_monotonic,
                freshness_sec=args.cleanup_freshness_sec,
            )
            record({
                "event": "final_safe_landed_disarmed",
                "cleanup_safety_evidence": cleanup_evidence,
            })
        return 0
    finally:
        if not any(item.get("event") == "final_safe_landed_disarmed" for item in events):
            cleanup_started_monotonic = time.monotonic()
            try:
                driver.land_and_wait(tools)
                cleanup_evidence = driver.wait_for_fresh_cleanup_safe_landed_disarmed(
                    cleanup_started_monotonic,
                    freshness_sec=args.cleanup_freshness_sec,
                )
                record({
                    "event": "cleanup_safe_landed_disarmed",
                    "cleanup_safety_evidence": cleanup_evidence,
                })
            except Exception as exc:
                record({"event": "cleanup_land_failed", "error": str(exc)})
        events_path.write_text(
            json.dumps(events, indent=2, default=str) + "\n", encoding="utf-8"
        )
        driver.destroy_node()
        tools.close()


if __name__ == "__main__":
    raise SystemExit(main())
