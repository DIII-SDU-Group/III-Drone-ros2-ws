import sys
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import patch

from iii_drone_mcp.agent_tools import DroneAgentTools, ToolResult


def _tools_with_px4_result(land_success: bool):
    tools = object.__new__(DroneAgentTools)
    commands = []
    tools.cancel_all_operation_goals = MethodType(
        lambda _self, **_kwargs: ToolResult(True, {}, "cancelled"), tools
    )
    tools._wait_no_active_operation_goal = MethodType(lambda _self, **_kwargs: True, tools)
    tools.clear_maneuver_queue = MethodType(
        lambda _self, **_kwargs: ToolResult(True, {}, "cleared"), tools
    )
    tools.active_operation_goal = MethodType(
        lambda _self: ToolResult(True, {"active": False}, "idle"), tools
    )
    tools.px4_safety = MethodType(
        lambda _self, **_kwargs: ToolResult(True, {"derived": {"armed": False, "in_air": False}}, "safe"),
        tools,
    )

    def px4_result(_self, command, **_kwargs):
        commands.append(command)
        if command == "land_direct":
            return ToolResult(land_success, {}, "landed" if land_success else "still airborne")
        return ToolResult(True, {}, command)

    tools._px4_tool_result_or_error = MethodType(px4_result, tools)
    return tools, commands


def test_safety_stop_uses_ros_direct_land_before_disarm() -> None:
    tools, commands = _tools_with_px4_result(True)

    result = tools.operation_safety_stop(mode="land", disarm_after_land=True)

    assert result.success is True
    assert commands == ["set_nav_state", "land_direct", "disarm_direct", "status"]


def test_safety_stop_never_disarms_without_confirmed_touchdown() -> None:
    tools, commands = _tools_with_px4_result(False)

    result = tools.operation_safety_stop(mode="land", disarm_after_land=True)

    assert result.success is False
    assert commands == ["set_nav_state", "land_direct", "status"]
    disarm_event = next(event for event in result.data["events"] if event["step"] == "px4_disarm")
    assert disarm_event["data"]["skipped"] is True


def test_px4_safety_ros_truth_overrides_stale_mavsdk_booleans() -> None:
    tools = object.__new__(DroneAgentTools)
    tools.px4 = MethodType(
        lambda _self, _command, **_kwargs: ToolResult(
            True,
            {"armed": False, "in_air": False, "flight_mode": "HOLD"},
            "stale MAVSDK status",
        ),
        tools,
    )

    class VehicleStatus:
        ARMING_STATE_ARMED = 2

    class VehicleLandDetected:
        pass

    class FailsafeFlags:
        pass

    status = SimpleNamespace(arming_state=2, nav_state=27, failsafe=False)
    land = SimpleNamespace(landed=False)
    samples = {
        VehicleStatus: status,
        VehicleLandDetected: land,
        FailsafeFlags: SimpleNamespace(),
    }
    tools._take_message = MethodType(
        lambda _self, _topic, msg_type, _timeout, required=False: samples[msg_type],
        tools,
    )
    tools._message_to_plain_dict = MethodType(
        lambda _self, message: vars(message) if message is not None else None,
        tools,
    )
    px4_msgs = ModuleType("px4_msgs")
    px4_msgs_msg = ModuleType("px4_msgs.msg")
    px4_msgs_msg.VehicleStatus = VehicleStatus
    px4_msgs_msg.VehicleLandDetected = VehicleLandDetected
    px4_msgs_msg.FailsafeFlags = FailsafeFlags

    with patch.dict(sys.modules, {"px4_msgs": px4_msgs, "px4_msgs.msg": px4_msgs_msg}):
        result = tools.px4_safety(timeout_sec=1.0)

    assert result.success is True
    assert result.data["derived"]["armed"] is True
    assert result.data["derived"]["in_air"] is True
    assert result.data["derived"]["verdict_source"] == "ros"
