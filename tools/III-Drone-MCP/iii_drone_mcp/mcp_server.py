from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import pwd
import signal
import sys
import traceback
from typing import Any, Callable

sys.dont_write_bytecode = True

if os.environ.get("III_DRONE_MCP_KEEP_RMW") != "1":
    os.environ["RMW_IMPLEMENTATION"] = os.environ.get("III_DRONE_MCP_RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
    os.environ["FASTDDS_BUILTIN_TRANSPORTS"] = os.environ.get("III_DRONE_MCP_FASTDDS_BUILTIN_TRANSPORTS", "UDPv4")

from iii_drone_mcp.agent_tools import DroneAgentTools, result_to_text


JsonDict = dict[str, Any]


class ToolCallTimeout(TimeoutError):
    pass


@contextmanager
def _tool_call_timeout(timeout_sec: float):
    if os.name != "posix" or timeout_sec <= 0:
        yield
        return

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise ToolCallTimeout(f"MCP tool call exceeded hard timeout of {timeout_sec:.1f}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    signal.signal(signal.SIGALRM, _raise_timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)


def _hard_timeout_for_arguments(arguments: JsonDict) -> float:
    default_timeout = float(os.environ.get("III_DRONE_MCP_TOOL_HARD_TIMEOUT_SEC", "110.0"))
    requested = arguments.get("timeout_sec")
    try:
        requested_timeout = float(requested)
    except (TypeError, ValueError):
        requested_timeout = default_timeout
    if requested_timeout <= 0:
        return min(default_timeout, 30.0)
    return min(default_timeout, max(1.0, requested_timeout + 5.0))


def _reexec_as_runtime_user_if_needed() -> None:
    if os.name != "posix" or os.geteuid() != 0 or os.environ.get("III_DRONE_MCP_ALLOW_ROOT") == "1":
        return

    runtime_user = os.environ.get("III_DRONE_MCP_USER", "iii")
    try:
        runtime_passwd = pwd.getpwnam(runtime_user)
    except KeyError:
        return

    env_args = [
        f"{key}={value}"
        for key, value in os.environ.items()
        if key not in {"HOME", "USER", "LOGNAME"}
    ]
    env_args.extend(
        [
            f"HOME={runtime_passwd.pw_dir}",
            f"USER={runtime_user}",
            f"LOGNAME={runtime_user}",
        ]
    )
    os.execvp(
        "sudo",
        [
            "sudo",
            "-H",
            "-u",
            runtime_user,
            "env",
            *env_args,
            sys.executable,
            sys.argv[0],
            *sys.argv[1:],
        ],
    )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonDict
    handler: Callable[[JsonDict], Any]


def _object_schema(properties: JsonDict, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": True,
    }


class DroneMcpServer:
    """Minimal MCP stdio server for III-Drone agent tooling."""

    def __init__(self, tools: DroneAgentTools):
        self.tools = tools
        self._tool_specs = self._build_tool_specs()

    def serve_stdio(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self._handle_request(request)
            except Exception as exc:  # MCP servers must keep the stdio session alive.
                response = self._error_response(None, -32603, str(exc), traceback.format_exc())
            if response is not None:
                print(json.dumps(response), flush=True)

    def _handle_request(self, request: JsonDict) -> JsonDict | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "iii-drone-mcp", "version": "0.1.0"},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(
                request_id,
                {
                    "tools": [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "inputSchema": spec.input_schema,
                        }
                        for spec in self._tool_specs.values()
                    ]
                },
            )
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            spec = self._tool_specs.get(name)
            if spec is None:
                return self._error_response(request_id, -32602, f"unknown tool: {name}")
            try:
                with _tool_call_timeout(_hard_timeout_for_arguments(arguments)):
                    result = spec.handler(arguments)
                return self._result(
                    request_id,
                    {"content": [{"type": "text", "text": result_to_text(result)}], "isError": not result.success},
                )
            except ToolCallTimeout as exc:
                return self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )
            except Exception as exc:
                return self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": f"{exc}\n{traceback.format_exc()}"}],
                        "isError": True,
                    },
                )
        return self._error_response(request_id, -32601, f"unknown method: {method}")

    def _build_tool_specs(self) -> dict[str, ToolSpec]:
        specs = [
            ToolSpec(
                "operation.fly_to_position",
                "Start the CustomOperation fly_to_position action through the maneuver execution system and return after goal acceptance.",
                _object_schema(
                    {
                        "frame_id": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "yaw": {"type": "number"},
                        "blend_to_next": {"type": "boolean"},
                        "ignore_altitude": {"type": "boolean", "default": False},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["frame_id", "x", "y", "z", "yaw"],
                ),
                lambda args: self.tools.start_operation("fly_to_position", **args),
            ),
            ToolSpec(
                "operation.fly_relative",
                "Start a small CustomOperation fly_to_position command relative to the current world->drone transform.",
                _object_schema(
                    {
                        "frame_id": {"type": "string"},
                        "dx": {"type": "number"},
                        "dy": {"type": "number"},
                        "dz": {"type": "number"},
                        "min_z": {"type": "number"},
                        "dyaw": {"type": "number"},
                        "blend_to_next": {"type": "boolean"},
                        "ignore_altitude": {"type": "boolean", "default": False},
                        "tf_timeout_sec": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.start_operation("fly_relative", **args),
            ),
            ToolSpec(
                "operation.cable_aware_fly_to_position",
                "Start the CustomOperation cable_aware_fly_to_position action through the cable-aware trajectory planner and return after goal acceptance.",
                _object_schema(
                    {
                        "frame_id": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "yaw": {"type": "number"},
                        "ignore_altitude": {"type": "boolean", "default": False},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["frame_id", "x", "y", "z", "yaw"],
                ),
                lambda args: self.tools.start_operation("cable_aware_fly_to_position", **args),
            ),
            ToolSpec(
                "operation.fly_to_fixture",
                "Activate CustomOperation if requested, map a stored simulation fixture pose into the live ROS world frame, and start fly_to_position or cable_aware_fly_to_position nonblocking.",
                _object_schema(
                    {
                        "position_id": {"type": "string"},
                        "operation_name": {
                            "type": "string",
                            "enum": ["auto", "fly_to_position", "cable_aware_fly_to_position"],
                            "default": "auto",
                        },
                        "activate_custom_operation": {"type": "boolean", "default": True},
                        "cancel_existing": {"type": "boolean", "default": True},
                        "clear_queue": {"type": "boolean"},
                        "use_cable_aware_if_overview_present": {"type": "boolean", "default": True},
                        "min_overview_lines": {"type": "integer"},
                        "geometry_path": {"type": "string"},
                        "send_timeout_sec": {"type": "number"},
                        "activation_timeout_sec": {"type": "number"},
                        "activation_postcondition_timeout_sec": {"type": "number"},
                        "activation_stable_sec": {"type": "number"},
                        "activation_repeat_count": {"type": "integer"},
                        "mapping_timeout_sec": {"type": "number"},
                        "gazebo_timeout_sec": {"type": "number"},
                        "tf_timeout_sec": {"type": "number"},
                        "overview_timeout_sec": {"type": "number"},
                        "ignore_altitude": {"type": "boolean", "default": False},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["position_id"],
                ),
                lambda args: self.tools.fly_to_fixture(**args),
            ),
            ToolSpec(
                "operation.resolve_fixture_target",
                "Resolve a stored simulation fixture pose into the live ROS world frame without starting a maneuver.",
                _object_schema(
                    {
                        "position_id": {"type": "string"},
                        "geometry_path": {"type": "string"},
                        "mapping_timeout_sec": {"type": "number"},
                        "gazebo_timeout_sec": {"type": "number"},
                        "tf_timeout_sec": {"type": "number"},
                    },
                    ["position_id"],
                ),
                lambda args: self.tools.resolve_fixture_target(**args),
            ),
            ToolSpec(
                "operation.fly_to_object",
                "Start the CustomOperation fly_to_object action with an exact III Target message shape.",
                _object_schema(
                    {
                        "target": {"type": "object"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["target"],
                ),
                lambda args: self.tools.start_operation("fly_to_object", **args),
            ),
            ToolSpec(
                "operation.cable_landing",
                "Start the CustomOperation cable_landing action.",
                _object_schema(
                    {
                        "target_cable_id": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["target_cable_id"],
                ),
                lambda args: self.tools.start_operation("cable_landing", **args),
            ),
            ToolSpec(
                "operation.cable_takeoff",
                "Start the CustomOperation cable_takeoff action.",
                _object_schema(
                    {
                        "target_cable_id": {"type": "integer"},
                        "target_cable_distance": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["target_cable_id", "target_cable_distance"],
                ),
                lambda args: self.tools.start_operation("cable_takeoff", **args),
            ),
            ToolSpec(
                "operation.hover",
                "Start the CustomOperation hover action.",
                _object_schema(
                    {
                        "duration_s": {"type": "number"},
                        "sustain_duration_s": {"type": "number"},
                        "sustain_action": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["duration_s"],
                ),
                lambda args: self.tools.start_operation("hover", **args),
            ),
            ToolSpec(
                "operation.hover_by_object",
                "Start the CustomOperation hover_by_object action.",
                _object_schema(
                    {
                        "target": {"type": "object"},
                        "duration_s": {"type": "number"},
                        "sustain_action": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["target", "duration_s"],
                ),
                lambda args: self.tools.start_operation("hover_by_object", **args),
            ),
            ToolSpec(
                "operation.hover_on_cable",
                "Start the CustomOperation hover_on_cable action.",
                _object_schema(
                    {
                        "target_cable_id": {"type": "integer"},
                        "target_z_velocity": {"type": "number"},
                        "target_yaw_rate": {"type": "number"},
                        "duration_s": {"type": "number"},
                        "sustain_action": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["target_cable_id", "duration_s"],
                ),
                lambda args: self.tools.start_operation("hover_on_cable", **args),
            ),
            ToolSpec(
                "operation.cancel",
                "Cancel the current CustomOperation action goal.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.cancel_operation(**args),
            ),
            ToolSpec(
                "operation.start",
                "Start a CustomOperation action nonblocking; returns after goal acceptance with a process-local goal_id.",
                _object_schema(
                    {
                        "action": {"type": "string"},
                        "frame_id": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "yaw": {"type": "number"},
                        "dx": {"type": "number"},
                        "dy": {"type": "number"},
                        "dz": {"type": "number"},
                        "min_z": {"type": "number"},
                        "dyaw": {"type": "number"},
                        "duration_s": {"type": "number"},
                        "target": {"type": "object"},
                        "target_cable_id": {"type": "integer"},
                        "target_cable_distance": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "tf_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["action"],
                ),
                lambda args: self.tools.start_operation(
                    str(args["action"]),
                    **{key: value for key, value in args.items() if key != "action"},
                ),
            ),
            ToolSpec(
                "operation.start_raw",
                "Start a raw generic CustomOperation action payload nonblocking; use for low-level diagnostics and parser validation.",
                _object_schema(
                    {
                        "operation": {"type": "string"},
                        "arguments": {"type": "object"},
                        "arguments_json": {"type": "string"},
                        "request_id": {"type": "string"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["operation"],
                ),
                lambda args: self.tools.start_raw_operation(
                    str(args["operation"]),
                    **{key: value for key, value in args.items() if key != "operation"},
                ),
            ),
            ToolSpec(
                "operation.start_fly_relative",
                "Start fly_relative nonblocking; returns a goal_id immediately after the underlying fly_to_position goal is accepted.",
                _object_schema(
                    {
                        "frame_id": {"type": "string"},
                        "dx": {"type": "number"},
                        "dy": {"type": "number"},
                        "dz": {"type": "number"},
                        "min_z": {"type": "number"},
                        "dyaw": {"type": "number"},
                        "tf_timeout_sec": {"type": "number"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.start_operation("fly_relative", **args),
            ),
            ToolSpec(
                "operation.start_fly_to_position",
                "Start fly_to_position nonblocking; returns a goal_id immediately after the goal is accepted.",
                _object_schema(
                    {
                        "frame_id": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "yaw": {"type": "number"},
                        "blend_to_next": {"type": "boolean"},
                        "ignore_altitude": {"type": "boolean", "default": False},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["frame_id", "x", "y", "z", "yaw"],
                ),
                lambda args: self.tools.start_operation("fly_to_position", **args),
            ),
            ToolSpec(
                "operation.start_hover",
                "Start hover nonblocking; returns a goal_id immediately after the goal is accepted.",
                _object_schema(
                    {
                        "duration_s": {"type": "number"},
                        "sustain_duration_s": {"type": "number"},
                        "sustain_action": {"type": "boolean"},
                        "send_timeout_sec": {"type": "number"},
                        "cancel_existing": {"type": "boolean"},
                        "clear_queue": {"type": "boolean"},
                        "clear_queue_timeout_sec": {"type": "number"},
                    },
                    ["duration_s"],
                ),
                lambda args: self.tools.start_operation("hover", **args),
            ),
            ToolSpec(
                "operation.goal_status",
                "Poll a nonblocking operation goal by process-local goal_id.",
                _object_schema({"goal_id": {"type": "string"}}, ["goal_id"]),
                lambda args: self.tools.operation_goal_status(**args),
            ),
            ToolSpec(
                "operation.goal_feedback",
                "Return compact feedback history for a nonblocking operation goal.",
                _object_schema({"goal_id": {"type": "string"}}, ["goal_id"]),
                lambda args: self.tools.operation_goal_feedback(**args),
            ),
            ToolSpec(
                "operation.goal_result",
                "Return result for a nonblocking operation goal when terminal.",
                _object_schema({"goal_id": {"type": "string"}}, ["goal_id"]),
                lambda args: self.tools.operation_goal_result(**args),
            ),
            ToolSpec(
                "operation.wait_goal",
                "Wait for a nonblocking operation goal to reach a terminal state.",
                _object_schema(
                    {
                        "goal_id": {"type": "string"},
                        "max_wait_sec": {"type": "number"},
                        "no_feedback_timeout_sec": {"type": "number"},
                        "allow_no_feedback": {"type": "boolean"},
                    },
                    ["goal_id"],
                ),
                lambda args: self.tools.wait_operation_goal(**args),
            ),
            ToolSpec(
                "operation.cancel_goal",
                "Cancel a nonblocking operation goal by process-local goal_id.",
                _object_schema({"goal_id": {"type": "string"}, "timeout_sec": {"type": "number"}}, ["goal_id"]),
                lambda args: self.tools.cancel_operation_goal(**args),
            ),
            ToolSpec(
                "operation.cancel_all",
                "Cancel all nonblocking operation goals tracked by this MCP process.",
                _object_schema({"timeout_sec": {"type": "number"}, "reason": {"type": "string"}}),
                lambda args: self.tools.cancel_all_operation_goals(**args),
            ),
            ToolSpec(
                "operation.active",
                "Return the active nonblocking operation goal tracked by this MCP process, if any.",
                _object_schema({}),
                lambda args: self.tools.active_operation_goal(),
            ),
            ToolSpec(
                "operation.safety_stop",
                "Cancel tracked operation goals, clear queued maneuvers, command PX4 hold or land, and return final status.",
                _object_schema(
                    {
                        "mode": {"type": "string", "enum": ["hold", "land"]},
                        "disarm_after_land": {"type": "boolean"},
                        "force_clear_queue": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    }
                ),
                lambda args: self.tools.operation_safety_stop(**args),
            ),
            ToolSpec(
                "operation.list_goals",
                "List nonblocking operation goals tracked by the current MCP process.",
                _object_schema({}),
                lambda args: self.tools.list_operation_goals(),
            ),
            ToolSpec(
                "operation.goal_registry_status",
                "Inspect current MCP process-local operation goal registry.",
                _object_schema({}),
                lambda args: self.tools.operation_goal_registry_status(),
            ),
            ToolSpec(
                "operation.clear_completed_goals",
                "Remove terminal nonblocking operation goal records from this MCP process.",
                _object_schema({}),
                lambda args: self.tools.clear_completed_operation_goals(),
            ),
            ToolSpec(
                "operation.prune_goals",
                "Prune terminal nonblocking operation goal records by age and registry size.",
                _object_schema({"retention_sec": {"type": "number"}, "max_retained_goals": {"type": "integer"}}),
                lambda args: self.tools.prune_operation_goals(**args),
            ),
            ToolSpec(
                "operation.discover_active_goals",
                "Report whether operation goals can be recovered after MCP process restart and list relevant ROS action surfaces.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.discover_active_operation_goals(**args),
            ),
            ToolSpec(
                "operation.status",
                "Inspect CustomOperation lifecycle, status topic, and action gateway availability.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.operation_status(**args),
            ),
            ToolSpec(
                "operation.activate",
                "Activate the registered CustomOperation PX4 mode using its runtime mode_id from the status topic.",
                _object_schema(
                    {
                        "timeout_sec": {"type": "number"},
                        "postcondition_timeout_sec": {"type": "number"},
                        "stable_sec": {"type": "number"},
                        "repeat_count": {"type": "integer"},
                        "target_system": {"type": "integer"},
                        "target_component": {"type": "integer"},
                    },
                ),
                lambda args: self.tools.activate_custom_operation(**args),
            ),
            ToolSpec(
                "maneuver.clear_queue",
                "Clear queued maneuvers without cancelling the currently executing maneuver.",
                _object_schema({"reason": {"type": "string"}, "timeout_sec": {"type": "number"}}),
                lambda args: self.tools.clear_maneuver_queue(**args),
            ),
            ToolSpec(
                "mission.executor_action",
                "Send a mission mode executor action request: takeoff, land, arm, or disarm.",
                _object_schema(
                    {
                        "request": {"type": "string", "enum": ["takeoff", "land", "arm", "disarm"]},
                        "takeoff_altitude": {"type": "number"},
                        "force_disarm": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["request"],
                ),
                lambda args: self.tools.mission_executor_action(**args),
            ),
            ToolSpec(
                "mission.activate_mode",
                "Activate a registered mission-owned PX4 mode using its runtime mode_id from the mission mode status topic.",
                _object_schema(
                    {
                        "mode_key": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                        "postcondition_timeout_sec": {"type": "number"},
                        "stable_sec": {"type": "number"},
                        "repeat_count": {"type": "integer"},
                        "target_system": {"type": "integer"},
                        "target_component": {"type": "integer"},
                    },
                ),
                lambda args: self.tools.activate_mission_mode(**args),
            ),
            ToolSpec(
                "mission.status",
                "Read the current mission executor state, active specification, registered modes, readiness, and control owner.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.mission_status(**args),
            ),
            ToolSpec(
                "payload.gripper",
                "Open or close the charger gripper through the existing ROS service.",
                _object_schema({"command": {"type": "string", "enum": ["open", "close"]}, "timeout_sec": {"type": "number"}}, ["command"]),
                lambda args: self.tools.gripper(**args),
            ),
            ToolSpec(
                "perception.pl_mapper",
                "Start, stop, pause, or freeze the powerline mapper.",
                _object_schema(
                    {
                        "command": {"type": "string", "enum": ["start", "stop", "pause", "freeze"]},
                        "reset": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.pl_mapper(**args),
            ),
            ToolSpec(
                "perception.update_powerline_overview",
                "Request a stored powerline overview update.",
                _object_schema(
                    {
                        "timeout_s": {"type": "integer"},
                        "service_timeout_sec": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    }
                ),
                lambda args: self.tools.update_powerline_overview(**args),
            ),
            ToolSpec(
                "perception.get_powerline_overview",
                "Fetch the stored powerline overview and report whether it has at least the requested number of lines.",
                _object_schema(
                    {
                        "min_lines": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "filename": {"type": "string"},
                    },
                ),
                lambda args: self.tools.get_powerline_overview(**args),
            ),
            ToolSpec(
                "perception.store_pylon_overview",
                "Store one pylon XY point in the pylon overview provider. Store exactly two distinct pylons before running inspection_demo.",
                _object_schema(
                    {
                        "pylon_id": {"type": "integer"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "frame_id": {"type": "string", "default": "world"},
                        "timeout_sec": {"type": "number"},
                        "filename": {"type": "string"},
                    },
                    ["pylon_id", "x", "y"],
                ),
                lambda args: self.tools.store_pylon_overview(**args),
            ),
            ToolSpec(
                "perception.get_pylon_overview",
                "Fetch the stored pylon overview and verify it contains the requested number of pylons.",
                _object_schema(
                    {
                        "min_pylons": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "filename": {"type": "string"},
                    },
                ),
                lambda args: self.tools.get_pylon_overview(**args),
            ),
            ToolSpec(
                "perception.clear_pylon_overview",
                "Clear the stored pylon overview.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.clear_pylon_overview(**args),
            ),
            ToolSpec(
                "perception.wait_powerline_lines",
                "Wait until the PL mapper publishes at least a requested number of powerline lines and save the latest message.",
                _object_schema(
                    {
                        "topic": {"type": "string"},
                        "min_lines": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "filename": {"type": "string"},
                    },
                ),
                lambda args: self.tools.wait_powerline_lines(**args),
            ),
            ToolSpec(
                "workflow.start_mission_deploy",
                "Start the nonblocking deployment workflow: takeoff if needed, activate CustomOperation, fly to staging pose, start PL mapper, store overview, optionally fly to a mission-start pose, then activate mission mode.",
                _object_schema(
                    {
                        "workflow_id": {"type": "string"},
                        "artifact_dir": {"type": "string"},
                        "position_id": {"type": "string"},
                        "frame_id": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "yaw": {"type": "number"},
                        "mission_start_position_id": {"type": "string"},
                        "mission_start_frame_id": {"type": "string"},
                        "mission_start_x": {"type": "number"},
                        "mission_start_y": {"type": "number"},
                        "mission_start_z": {"type": "number"},
                        "mission_start_yaw": {"type": "number"},
                        "takeoff_altitude": {"type": "number"},
                        "min_powerline_lines": {"type": "integer"},
                        "powerline_timeout_sec": {"type": "number"},
                        "overview_timeout_s": {"type": "integer"},
                        "overview_service_timeout_sec": {"type": "number"},
                        "overview_query_timeout_sec": {"type": "number"},
                        "overview_store_attempts": {"type": "integer"},
                        "overview_retry_delay_sec": {"type": "number"},
                        "min_pylons": {"type": "integer"},
                        "pylon_overview_timeout_sec": {"type": "number"},
                        "demo_pos_over_corridor_id": {"type": "string"},
                        "demo_pos_pylon_1_id": {"type": "string"},
                        "demo_pos_pylon_2_id": {"type": "string"},
                        "mission_catalog_id": {
                            "type": "string",
                            "description": "Optional installed mission catalog ID to select before activation. If omitted, reach_cable selects reach-charge-leave-experimental and inspection_demo selects inspection-production.",
                        },
                        "use_default_mission_catalog": {
                            "type": "boolean",
                            "description": "Restore the installed catalog default for the active configuration profile before mission activation.",
                        },
                        "require_pylon_overview": {
                            "type": "boolean",
                            "description": "Require stored pylon overview before mission activation. Automatically required for mission_mode=inspection_demo.",
                        },
                        "force_update_overview": {
                            "type": "boolean",
                            "default": True,
                            "description": "Refresh the stored powerline overview before mission activation. Defaults to true.",
                        },
                        "mission_mode": {"type": "string"},
                        "px4_timeout_sec": {"type": "number"},
                        "custom_mode_timeout_sec": {"type": "number"},
                        "fly_send_timeout_sec": {"type": "number"},
                        "fly_wait_timeout_sec": {"type": "number"},
                        "fly_feedback_stale_timeout_sec": {
                            "type": "number",
                            "description": "For blocking deployment workflow waits only: treat a fly action as stale if it stops publishing feedback for this many seconds, then recover only if maneuver execution is idle and the target pose is reached.",
                        },
                        "position_timeout_sec": {"type": "number"},
                        "position_tolerance_m": {"type": "number"},
                        "gazebo_position_tolerance_m": {
                            "type": "number",
                            "description": "Maximum allowed Gazebo ground-truth XY error for simulation fixture pose checks. Defaults to 0.75 m.",
                        },
                        "position_settle_sec": {"type": "number"},
                        "minimum_staging_z": {"type": "number"},
                        "minimum_staging_above_ground": {"type": "number"},
                        "staging_ground_clearance_margin": {"type": "number"},
                        "ground_estimate_timeout_sec": {"type": "number"},
                        "skip_mission_activation": {"type": "boolean"},
                    },
                ),
                lambda args: self.tools.start_mission_deploy_workflow(**args),
            ),
            ToolSpec(
                "workflow.mission_deploy_status",
                "Poll a mission deployment workflow started by workflow.start_mission_deploy.",
                _object_schema(
                    {
                        "workflow_id": {"type": "string"},
                        "status_path": {"type": "string"},
                        "artifact_dir": {"type": "string"},
                        "tail_log_lines": {"type": "integer"},
                    },
                ),
                lambda args: self.tools.mission_deploy_workflow_status(**args),
            ),
            ToolSpec(
                "workflow.cancel_mission_deploy",
                "Terminate a mission deployment workflow process.",
                _object_schema(
                    {
                        "workflow_id": {"type": "string"},
                        "status_path": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.cancel_mission_deploy_workflow(**args),
            ),
            ToolSpec(
                "mission.select_catalog_entry",
                "Select an installed mission catalog entry while the lifecycle node is active and no mission is running. Selection is transactional and uses catalog IDs only.",
                _object_schema(
                    {
                        "catalog_id": {
                            "type": "string",
                            "description": "Installed mission catalog entry ID. Filesystem paths are rejected by the runtime.",
                        },
                        "use_default": {
                            "type": "boolean",
                            "description": "Restore the installed catalog default for the active profile.",
                        },
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.select_mission_catalog_entry(**args),
            ),
            ToolSpec(
                "mission.get_catalog",
                "Return installed mission catalog metadata without exposing filesystem paths.",
                _object_schema(
                    {
                        "include_incompatible": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.get_mission_catalog(**args),
            ),
            ToolSpec(
                "mission.set_bool_service",
                "Call a std_srvs/SetBool service, intended for mission runtime intent services such as trigger_recharge_now, stay_on_cable, and interrupt_recharging_now.",
                _object_schema(
                    {
                        "service_name": {"type": "string"},
                        "value": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["service_name", "value"],
                ),
                lambda args: self.tools.set_bool_service(**args),
            ),
            ToolSpec(
                "runtime.discover_container",
                "Discover the devcontainer associated with a host workspace folder, returning the selected container id/name and docker exec prefix when Docker is available.",
                _object_schema(
                    {
                        "local_folder": {"type": "string"},
                        "workspace_root": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.discover_container(**args),
            ),
            ToolSpec(
                "configuration",
                "Get, save, load, or update configuration server parameter state.",
                _object_schema({"command": {"type": "string"}}),
                lambda args: self.tools.configuration(**args),
            ),
            ToolSpec(
                "px4",
                "Run MAVSDK PX4 commands equivalent to common QGroundControl actions, or get PX4 status.",
                _object_schema(
                    {
                        "command": {
                            "type": "string",
                            "enum": [
                                "arm",
                                "takeoff",
                                "disarm",
                                "arm_direct",
                                "disarm_direct",
                                "land",
                                "hold",
                                "return_to_launch",
                                "set_mode",
                                "set_nav_state",
                                "get_param",
                                "set_param",
                                "status",
                                "health",
                            ],
                        },
                        "mode": {"type": "string"},
                        "nav_state": {"type": "integer"},
                        "param_name": {"type": "string"},
                        "param_value": {"type": "number"},
                        "param_type": {"type": "integer"},
                        "target_system": {"type": "integer"},
                        "target_component": {"type": "integer"},
                        "repeat_count": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "postcondition_timeout_sec": {"type": "number"},
                        "health_stable_sec": {"type": "number"},
                        "stable_sec": {"type": "number"},
                        "min_altitude_m": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.px4(**args),
            ),
            ToolSpec(
                "px4.health",
                "Inspect PX4 preflight, arming, navigation, land-detector, failsafe, and latest command ACK state.",
                _object_schema({"timeout_sec": {"type": "number"}, "stable_sec": {"type": "number"}}),
                lambda args: self.tools.px4_health(**args),
            ),
            ToolSpec(
                "battery.reset",
                "Reset the PX4 SITL battery state without restarting PX4, Gazebo, or the III system.",
                _object_schema(
                    {
                        "remaining_pct": {
                            "type": "number",
                            "description": "Battery percentage to apply; defaults to 100.",
                        },
                        "tolerance_pct": {
                            "type": "number",
                            "description": "Maximum overshoot accepted in the observed battery status.",
                        },
                        "timeout_sec": {"type": "number"},
                    }
                ),
                lambda args: self.tools.battery_reset(**args),
            ),
            ToolSpec(
                "px4.safety",
                "Inspect current PX4 safety state from MAVSDK and ROS topics, including failsafe and recovery indicators.",
                _object_schema({"timeout_sec": {"type": "number"}}),
                lambda args: self.tools.px4_safety(**args),
            ),
            ToolSpec(
                "px4.ulog_events",
                "Extract relevant commander/failsafe/nav/mode/arming event strings from the latest or specified PX4 ULog.",
                _object_schema(
                    {
                        "ulog_path": {"type": "string"},
                        "filename": {"type": "string"},
                        "max_events": {"type": "integer"},
                    }
                ),
                lambda args: self.tools.px4_ulog_events(**args),
            ),
            ToolSpec(
                "logs",
                "Capture supervised tmux panes as diagnostic artifacts without custom shell commands.",
                _object_schema(
                    {
                        "command": {"type": "string", "enum": ["capture"]},
                        "entity_id": {"type": "string"},
                        "history": {"type": "boolean"},
                        "session": {"type": "string"},
                        "window": {"type": "string"},
                        "pane": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "tail_lines": {"type": "integer"},
                        "save": {"type": "boolean"},
                        "filename": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.logs(**args),
            ),
            ToolSpec(
                "safety.critical_nodes",
                "Inspect supervised logs for nonzero exits from critical nodes since an optional UTC timestamp.",
                _object_schema(
                    {
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "since_iso": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    }
                ),
                lambda args: self.tools.critical_node_safety(**args),
            ),
            ToolSpec(
                "system",
                "Run supervised system operations through the III CLI.",
                _object_schema(
                    {
                        "command": {
                            "type": "string",
                            "enum": ["boot", "start", "stop", "restart", "shutdown", "daemon_restart", "status", "service_list", "service_restart"],
                        },
                        "entity_id": {"type": "string"},
                        "include_dependencies": {"type": "boolean"},
                        "cold": {"type": "boolean"},
                        "keep_session": {"type": "boolean"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.system(**args),
            ),
            ToolSpec(
                "simulation",
                "Start, restart, stop, or inspect the PX4/Gazebo/QGroundControl simulation tool session; start/restart can run backend-only.",
                _object_schema(
                    {
                        "command": {"type": "string", "enum": ["start", "restart", "stop", "status"]},
                        "headless": {"type": "boolean"},
                        "wait_ready": {"type": "boolean"},
                        "ready_timeout_sec": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.simulation(**args),
            ),
            ToolSpec(
                "inspect",
                "Inspect ROS graph data and optionally save topic snapshots as artifacts.",
                _object_schema(
                    {
                        "command": {"type": "string"},
                        "topic": {"type": "string"},
                        "save": {"type": "boolean"},
                        "filename": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.inspect(**args),
            ),
            ToolSpec(
                "topic",
                "List ROS topics, inspect topic endpoint info, or record topic data artifacts.",
                _object_schema(
                    {
                        "command": {
                            "type": "string",
                            "enum": ["list", "list_info", "record_seconds", "record_messages"],
                        },
                        "topic": {"type": "string"},
                        "duration_sec": {"type": "number"},
                        "message_count": {"type": "integer"},
                        "count": {"type": "integer"},
                        "timeout_sec": {"type": "number"},
                        "per_topic_timeout_sec": {"type": "number"},
                        "include_hidden": {"type": "boolean"},
                        "include_types": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "filename": {"type": "string"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.topic(**args),
            ),
            ToolSpec(
                "rosbag_record",
                "Start, stop, or query a nonblocking rosbag recording. Defaults to recording all ROS topics under /tmp/iii_drone/rosbags.",
                _object_schema(
                    {
                        "command": {"type": "string", "enum": ["start", "stop", "status"]},
                        "recording_id": {"type": "string"},
                        "output_dir": {"type": "string"},
                        "all_topics": {"type": "boolean"},
                        "topics": {"type": "array", "items": {"type": "string"}},
                        "include_hidden_topics": {"type": "boolean"},
                        "startup_grace_sec": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.rosbag_record(**args),
            ),
            ToolSpec(
                "gazebo",
                "Inspect Gazebo topics, control an external Gazebo camera, and save rendered external simulation snapshots.",
                _object_schema(
                    {
                        "command": {
                            "type": "string",
                            "enum": ["topics", "services", "topic_once", "set_camera_pose", "image_snapshot", "ros_image_snapshot"],
                        },
                        "world": {"type": "string"},
                        "model_name": {"type": "string"},
                        "topic": {"type": "string"},
                        "filename": {"type": "string"},
                        "service": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "target_x": {"type": "number"},
                        "target_y": {"type": "number"},
                        "target_z": {"type": "number"},
                        "qx": {"type": "number"},
                        "qy": {"type": "number"},
                        "qz": {"type": "number"},
                        "qw": {"type": "number"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "horizontal_fov": {"type": "number"},
                        "update_rate": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["command"],
                ),
                lambda args: self.tools.gazebo(**args),
            ),
            ToolSpec(
                "sim.geometry_state",
                "Return compact simulation geometry plus current world-frame drone relationship to conductors, pylons, and corridor.",
                _object_schema(
                    {
                        "geometry_path": {"type": "string"},
                        "pose": {"type": "object"},
                        "tf_timeout_sec": {"type": "number"},
                        "corridor_margin_m": {"type": "number"},
                        "include_samples": {"type": "boolean"},
                    },
                ),
                lambda args: self.tools.sim_observation("geometry_state", **args),
            ),
            ToolSpec(
                "sim.record_drone_position",
                "Record the current ROS world->drone pose into the persistent simulation geometry fixture, by default under mission_start_positions.",
                _object_schema(
                    {
                        "position_id": {"type": "string"},
                        "label": {"type": "string"},
                        "section": {
                            "type": "string",
                            "enum": ["mission_start_positions", "drone_positions", "demo_overview_positions"],
                        },
                        "category": {"type": "string"},
                        "expected": {"type": "object"},
                        "intended_use": {"type": "array", "items": {"type": "string"}},
                        "note": {"type": "string"},
                        "geometry_path": {"type": "string"},
                        "tf_timeout_sec": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["position_id"],
                ),
                lambda args: self.tools.sim_observation("record_drone_position", **args),
            ),
            ToolSpec(
                "sim.visibility_state",
                "Estimate conductor visibility from current or provided world-frame drone pose.",
                _object_schema(
                    {
                        "geometry_path": {"type": "string"},
                        "pose": {"type": "object"},
                        "tf_timeout_sec": {"type": "number"},
                        "max_range_m": {"type": "number"},
                        "horizontal_fov_rad": {"type": "number"},
                        "upward_cone_rad": {"type": "number"},
                    },
                ),
                lambda args: self.tools.sim_observation("visibility_state", **args),
            ),
            ToolSpec(
                "sim.trajectory_state",
                "Return recent world-frame path samples, maneuver queue state, reference mode, and setpoint echo.",
                _object_schema(
                    {
                        "timeout_sec": {"type": "number"},
                        "sample_duration_sec": {"type": "number"},
                        "sample_period_sec": {"type": "number"},
                        "max_samples": {"type": "integer"},
                    },
                ),
                lambda args: self.tools.sim_observation("trajectory_state", **args),
            ),
            ToolSpec(
                "sim.render_snapshot",
                "Capture a rendered Gazebo PNG from a named diagnostic view preset or explicit custom pose.",
                _object_schema(
                    {
                        "view": {
                            "type": "string",
                            "enum": ["custom", "follow_drone", "topdown", "corridor", "target", "perception_fov"],
                        },
                        "geometry_path": {"type": "string"},
                        "world": {"type": "string"},
                        "filename": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "target_x": {"type": "number"},
                        "target_y": {"type": "number"},
                        "target_z": {"type": "number"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "horizontal_fov": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.sim_observation("render_snapshot", **args),
            ),
            ToolSpec(
                "sim.render_snapshot_set",
                "Capture a diagnostic bundle of rendered Gazebo PNG views.",
                _object_schema(
                    {
                        "views": {"type": "array", "items": {"type": "string"}},
                        "geometry_path": {"type": "string"},
                        "world": {"type": "string"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "horizontal_fov": {"type": "number"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.sim_observation("render_snapshot_set", **args),
            ),
            ToolSpec(
                "sim.plot_state",
                "Generate compact geometry/path diagnostic plots for simulation inspection.",
                _object_schema(
                    {
                        "geometry_path": {"type": "string"},
                        "path_samples": {"type": "array", "items": {"type": "object"}},
                        "sample_duration_sec": {"type": "number"},
                        "sample_period_sec": {"type": "number"},
                        "prefix": {"type": "string"},
                    },
                ),
                lambda args: self.tools.sim_observation("plot_state", **args),
            ),
            ToolSpec(
                "sim.observe_window",
                "Record a simulation observation window with pose samples, optional rendered snapshots, plots, and verdict JSON.",
                _object_schema(
                    {
                        "geometry_path": {"type": "string"},
                        "duration_sec": {"type": "number"},
                        "sample_period_sec": {"type": "number"},
                        "path_samples": {"type": "array", "items": {"type": "object"}},
                        "capture_snapshots": {"type": "boolean"},
                        "expected_corridor": {"type": "boolean"},
                        "min_conductor_clearance_m": {"type": "number"},
                        "min_sample_count": {"type": "integer"},
                        "expected_dx": {"type": "number"},
                        "expected_dy": {"type": "number"},
                        "expected_dz": {"type": "number"},
                        "expected_displacement_tolerance_m": {"type": "number"},
                        "min_distance_traveled_m": {"type": "number"},
                        "max_distance_traveled_m": {"type": "number"},
                        "min_target_progress_m": {"type": "number"},
                        "hover_max_drift_m": {"type": "number"},
                        "expected_mission_mode": {"type": "string"},
                        "expected_mission_success": {"type": "boolean"},
                        "fail_on_mission_failure": {"type": "boolean"},
                        "max_returned_samples": {"type": "integer"},
                        "prefix": {"type": "string"},
                        "filename": {"type": "string"},
                        "world": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                    },
                ),
                lambda args: self.tools.sim_observation("observe_window", **args),
            ),
            ToolSpec(
                "sim.observe_active_goal",
                "Observe pose, plots, and goal state timeline for a process-local nonblocking operation goal until terminal or max duration.",
                _object_schema(
                    {
                        "goal_id": {"type": "string"},
                        "geometry_path": {"type": "string"},
                        "max_duration_sec": {"type": "number"},
                        "sample_period_sec": {"type": "number"},
                        "capture_snapshots": {"type": "boolean"},
                        "expected_corridor": {"type": "boolean"},
                        "min_conductor_clearance_m": {"type": "number"},
                        "min_sample_count": {"type": "integer"},
                        "expected_dx": {"type": "number"},
                        "expected_dy": {"type": "number"},
                        "expected_dz": {"type": "number"},
                        "expected_displacement_tolerance_m": {"type": "number"},
                        "min_distance_traveled_m": {"type": "number"},
                        "max_distance_traveled_m": {"type": "number"},
                        "min_target_progress_m": {"type": "number"},
                        "max_target_regression_m": {"type": "number"},
                        "hover_max_drift_m": {"type": "number"},
                        "require_terminal": {"type": "boolean"},
                        "max_returned_samples": {"type": "integer"},
                        "max_returned_goal_samples": {"type": "integer"},
                        "prefix": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                    ["goal_id"],
                ),
                lambda args: self.tools.sim_observation("observe_active_goal", **args),
            ),
            ToolSpec(
                "sim.observation_timeline",
                "Record a compact timeline of pose, operation state, PX4 status, setpoints, and perception publish state.",
                _object_schema(
                    {
                        "duration_sec": {"type": "number"},
                        "sample_period_sec": {"type": "number"},
                        "warmup_sec": {"type": "number"},
                        "max_returned_samples": {"type": "integer"},
                        "filename": {"type": "string"},
                    },
                ),
                lambda args: self.tools.sim_observation("observation_timeline", **args),
            ),
            ToolSpec(
                "sim.perception_verdict",
                "Compare expected visible conductors from simulation geometry with current PL mapper powerline detections.",
                _object_schema(
                    {
                        "geometry_path": {"type": "string"},
                        "topic": {"type": "string"},
                        "timeout_sec": {"type": "number"},
                        "tf_timeout_sec": {"type": "number"},
                        "filename": {"type": "string"},
                    },
                ),
                lambda args: self.tools.sim_observation("perception_verdict", **args),
            ),
        ]
        return {spec.name: spec for spec in specs}

    @staticmethod
    def _result(request_id: Any, result: JsonDict) -> JsonDict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error_response(request_id: Any, code: int, message: str, data: Any = None) -> JsonDict:
        error: JsonDict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


def main() -> None:
    _reexec_as_runtime_user_if_needed()

    parser = argparse.ArgumentParser(description="III-Drone MCP stdio server")
    parser.add_argument("--artifact-dir", default="/tmp/iii_drone/iii_drone_agent")
    parser.add_argument("--px4-system-address", default="udp://:14540")
    args = parser.parse_args()

    tools = DroneAgentTools(
        artifact_dir=args.artifact_dir,
        px4_system_address=args.px4_system_address,
    )
    try:
        DroneMcpServer(tools).serve_stdio()
    finally:
        tools.close()


if __name__ == "__main__":
    main()
