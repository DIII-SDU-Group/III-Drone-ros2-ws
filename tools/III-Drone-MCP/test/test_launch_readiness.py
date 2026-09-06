from dataclasses import dataclass
import math
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy

from iii_drone_mcp.agent_tools import DroneAgentTools, ToolResult
from iii_drone_mcp.mcp_server import DroneMcpServer
from iii_drone_mcp.mission_scenario_suite import mission_attempt_completed


def test_custom_operation_status_uses_retained_best_effort_qos():
    tools = DroneAgentTools.__new__(DroneAgentTools)
    observed = {}

    def take_message(topic, message_type, timeout_sec, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(data='{"mode_id": 27}')

    tools._take_message = take_message
    with patch("iii_drone_mcp.agent_tools.rclpy.ok", return_value=True):
        result = tools._wait_custom_operation_status(timeout_sec=0.1, auto_recover=False)

    assert result.success is True
    assert observed["qos_profile"].durability == DurabilityPolicy.TRANSIENT_LOCAL
    assert observed["qos_profile"].reliability == ReliabilityPolicy.BEST_EFFORT


def test_simulation_status_flags_keep_transport_separate_from_backend_processes():
    flags = DroneAgentTools._simulation_status_flags(
        "\n".join(
            [
                "tmux_session: running",
                "simulation_process_groups: 123 456",
                "gazebo_transport: unavailable",
                "px4_instance_state: lock_or_socket_present",
            ]
        )
    )

    assert flags["session_running"] is True
    assert flags["simulation_processes_running"] is True
    assert flags["px4_instance_present"] is True
    assert flags["gazebo_transport_available"] is False
    assert flags["backend_processes_ready"] is True


def test_simulation_readiness_uses_px4_when_transport_probe_is_unavailable():
    @dataclass
    class Telemetry:
        armed: bool = False
        flight_mode: str = "HOLD"
        in_air: bool = False

    tools = DroneAgentTools.__new__(DroneAgentTools)
    tools._px4_system_address = "udpin://0.0.0.0:14540"
    tools.simulation = lambda *args, **kwargs: ToolResult(
        True,
        {
            "stdout": "\n".join(
                [
                    "tmux_session: running",
                    "simulation_process_groups: 123 456",
                    "gazebo_transport: unavailable",
                    "px4_instance_state: lock_or_socket_present",
                ]
            )
        },
    )

    with (
        patch("iii_drone_mcp.agent_tools.Px4CommandClient"),
        patch.object(
            DroneAgentTools,
            "_px4_connect_and_snapshot",
            new=AsyncMock(return_value=Telemetry()),
        ),
    ):
        result = tools._wait_for_simulation_ready(ToolResult(True, {}), timeout_sec=1.0)

    assert result.success is True
    assert result.data["simulation_status_flags"]["gazebo_transport_available"] is False
    assert result.data["readiness_warnings"]


def test_system_status_booted_rejects_preboot_status():
    result = ToolResult(
        True,
        {"stdout": "Booted: False\nProfile: None\n\nManaged nodes:\n"},
        "",
    )

    assert DroneAgentTools._system_status_booted(result) is False


def test_system_status_booted_accepts_active_profile():
    result = ToolResult(
        True,
        {"stdout": "Booted: True\nProfile: sim\n\nManaged nodes:\n  active: mission_executor\n"},
        "",
    )

    assert DroneAgentTools._system_status_booted(result) is True


def test_terminal_reach_charge_leave_requires_inactive_mission():
    assert mission_attempt_completed(
        saw_reach_active=True,
        saw_leave_success=True,
        mission_status={"mission_active": False},
    )

    assert not mission_attempt_completed(
        saw_reach_active=True,
        saw_leave_success=True,
        mission_status={"mission_active": True},
    )


def test_battery_reset_toggles_px4_reset_token_and_verifies_percentage():
    tools = DroneAgentTools.__new__(DroneAgentTools)
    calls = []
    reset_token = 0
    acknowledgement_token = 0

    def px4(command, **kwargs):
        nonlocal reset_token, acknowledgement_token
        calls.append((command, kwargs))
        if command == "get_param" and kwargs["param_name"] == "SIM_BAT_MIN_PCT":
            return ToolResult(True, {"param_value": 20.0})
        if command == "get_param" and kwargs["param_name"] == "SIM_BAT_RESET":
            return ToolResult(True, {"param_value": float(reset_token)})
        if command == "get_param" and kwargs["param_name"] == "SIM_BAT_RST_ACK":
            return ToolResult(True, {"param_value": float(acknowledgement_token)})
        if command == "set_param" and kwargs["param_name"] == "SIM_BAT_RESET":
            reset_token = int(kwargs["param_value"])
            acknowledgement_token = reset_token
        return ToolResult(True, {"param_value": kwargs["param_value"]})

    samples = iter([SimpleNamespace(remaining=0.42), SimpleNamespace(remaining=0.999)])
    tools.px4 = px4
    tools._take_message = lambda *_args, **_kwargs: next(samples)
    tools._message_to_nested_dict = lambda message: vars(message)

    px4_msgs = ModuleType("px4_msgs")
    px4_msgs_msg = ModuleType("px4_msgs.msg")
    px4_msgs_msg.BatteryStatus = object
    px4_msgs.msg = px4_msgs_msg
    with (
        patch.dict(sys.modules, {"px4_msgs": px4_msgs, "px4_msgs.msg": px4_msgs_msg}),
        patch("iii_drone_mcp.agent_tools.rclpy.ok", return_value=True),
    ):
        result = tools.battery_reset(remaining_pct=100.0, timeout_sec=2.0)

    assert result.success is True
    assert result.data["observed_remaining_pct"] == 99.9
    assert result.data["acknowledgement_token"] == 1
    assert result.data["cleanup_acknowledgement_token"] == 0
    assert ("set_param", {
        "param_name": "SIM_BAT_INIT_PCT",
        "param_value": 100.0,
        "param_type": 9,
        "timeout_sec": 2.0,
    }) in calls
    assert ("set_param", {
        "param_name": "SIM_BAT_RESET",
        "param_value": 0,
        "param_type": 6,
        "timeout_sec": 2.0,
    }) in calls
    assert ("set_param", {
        "param_name": "SIM_BAT_RESET",
        "param_value": 1,
        "param_type": 6,
        "timeout_sec": 2.0,
    }) in calls


def test_battery_reset_tool_is_registered():
    server = DroneMcpServer(SimpleNamespace())

    assert "battery.reset" in server._tool_specs
    assert server._tool_specs["battery.reset"].input_schema["properties"]["remaining_pct"]["type"] == "number"


def test_fixture_pose_application_is_sim_only(monkeypatch):
    tools = DroneAgentTools.__new__(DroneAgentTools)
    monkeypatch.delenv("SIMULATION", raising=False)

    result = tools.apply_fixture_pose("pos_over_corridor")

    assert result.success is False
    assert "only in simulation" in result.message


def test_fixture_pose_application_uses_stored_gazebo_truth(monkeypatch):
    tools = DroneAgentTools.__new__(DroneAgentTools)
    monkeypatch.setenv("SIMULATION", "true")
    tools._find_fixture_position = lambda *_args, **_kwargs: {
        "_fixture_path": "/fixture.json",
        "_fixture_section": "drone_positions",
        "recorded_from": {"gazebo_world": "test_world", "gazebo_model": "test_drone"},
    }
    tools._fixture_gazebo_pose = lambda _position: {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.6}

    class Pose:
        def __init__(self):
            self.name = ""
            self.position = SimpleNamespace(x=0.0, y=0.0, z=0.0)
            self.orientation = SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0)

    class Boolean:
        pass

    requests = []

    class Node:
        def request(self, service, request, request_type, response_type, timeout_ms):
            requests.append((service, request, request_type, response_type, timeout_ms))
            return True, SimpleNamespace(data=True)

    gz = ModuleType("gz")
    gz_msgs = ModuleType("gz.msgs10")
    gz_boolean = ModuleType("gz.msgs10.boolean_pb2")
    gz_pose = ModuleType("gz.msgs10.pose_pb2")
    gz_transport = ModuleType("gz.transport13")
    gz_boolean.Boolean = Boolean
    gz_pose.Pose = Pose
    gz_transport.Node = Node
    gz.msgs10 = gz_msgs

    with patch.dict(
        sys.modules,
        {
            "gz": gz,
            "gz.msgs10": gz_msgs,
            "gz.msgs10.boolean_pb2": gz_boolean,
            "gz.msgs10.pose_pb2": gz_pose,
            "gz.transport13": gz_transport,
        },
    ):
        result = tools.apply_fixture_pose("pos_over_corridor", timeout_sec=4.0)

    assert result.success is True
    service, request, request_type, response_type, timeout_ms = requests[0]
    assert service == "/world/test_world/set_pose"
    assert request.name == "test_drone"
    assert (request.position.x, request.position.y, request.position.z) == (1.0, 2.0, 3.0)
    assert request.orientation.z == pytest.approx(math.sin(0.3))
    assert request.orientation.w == pytest.approx(math.cos(0.3))
    assert request_type is Pose
    assert response_type is Boolean
    assert timeout_ms == 4000
