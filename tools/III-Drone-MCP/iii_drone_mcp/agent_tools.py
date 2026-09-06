from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Optional, Sequence
import uuid

import yaml

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Transform

from iii_drone_interfaces.action import ModeExecutorAction
from iii_drone_interfaces.msg import Maneuver, ManeuverQueue
from iii_drone_interfaces.msg import MissionModeStatus
from iii_drone_interfaces.msg import PLMapperCommand as PLMapperCommandMsg
from iii_drone_interfaces.msg import Powerline
from iii_drone_interfaces.msg import StringStamped
from iii_drone_interfaces.msg import Target
from iii_drone_interfaces.srv import (
    ClearManeuverQueue,
    ClearPylonOverview,
    GetCurrentParameterFile,
    GetDeclaredParameters,
    GetParameterFiles,
    GetParameterYaml,
    GetPowerlineOverview,
    GetPylonOverview,
    GripperCommand,
    GetMissionCatalog,
    LoadParameters,
    PLMapperCommand,
    SaveParameters,
    SelectMissionCatalogEntry,
    SetParameterFromGC,
    StorePylonOverview,
    UpdatePowerlineOverview,
)
from iii_drone_mission.operations_client import OperationsClient, OperationResult
from iii_drone_mcp.px4_command_client import Px4CommandClient
from iii_drone_mcp.simulation_observation import (
    all_conductor_samples,
    compact_conductors,
    conductor_height_range,
    conductor_samples,
    corridor_membership,
    corridor_model,
    decimate,
    distance,
    image_metadata,
    load_geometry,
    nearest_conductor,
    point_from_any,
    visibility_state,
    write_json,
)
from iii_drone_mcp.fixture_ids import canonical_fixture_id, normalize_fixture_id
from iii_drone_mcp.simulation_frames import (
    GAZEBO_TO_ROS_POSITION_YAW_RAD,
    rotate_gazebo_xy_delta_to_ros,
)
from lifecycle_msgs.msg import State as LifecycleState
from lifecycle_msgs.srv import GetState as LifecycleGetState
from std_srvs.srv import SetBool


DEFAULT_GEOMETRY_PATH = Path(__file__).resolve().parents[1] / "config" / "hca_full_pylon_setup_geometry.json"


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: Any = None
    message: str = ""


def _mission_mode_topic_key(mode_key: str) -> str:
    stripped = str(mode_key or "").strip()
    if not stripped:
        return "reach_cable"
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", stripped)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)
    snake = re.sub(r"[^0-9A-Za-z]+", "_", snake)
    snake = re.sub(r"_+", "_", snake).strip("_").lower()
    return snake or "reach_cable"


@dataclass
class OperationGoalRecord:
    goal_id: str
    mcp_session_id: str
    action_name: str
    state: str
    started_at: float
    accepted_at: float | None
    completed_at: float | None
    cancel_requested_at: float | None
    cancelled_at: float | None
    failed_at: float | None
    target_summary: dict[str, Any]
    reference_frame: str | None
    client: ActionClient
    goal_handle: Any
    result_future: Any
    feedback_count: int = 0
    last_feedback: Any = None
    last_feedback_at: float | None = None
    feedback_history: Any = None
    result: Any = None
    error: str | None = None
    ros_goal_id: str | None = None


class DroneAgentTools:
    """Shared implementation behind MCP and other agent-facing entry points."""

    def __init__(
        self,
        *,
        node_name: str = "iii_drone_agent_tools",
        artifact_dir: str | Path = "/tmp/iii_drone/iii_drone_agent",
        px4_system_address: str = "udpin://0.0.0.0:14540",
    ):
        if not rclpy.ok():
            rclpy.init(args=None)
        if node_name == "iii_drone_agent_tools":
            node_name = f"{node_name}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.node = Node(node_name, start_parameter_services=False)
        self.artifact_dir = self._resolve_artifact_dir(Path(artifact_dir))
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.operations = OperationsClient(self.node)
        self._px4_system_address = px4_system_address
        self._workspace_root = Path(os.environ.get("WORKSPACE_DIR", "/home/iii/ws"))
        self._iii_cli_path = self._resolve_iii_cli()
        self._mcp_session_id = str(uuid.uuid4())
        self._goal_registry_started_at = time.time()
        self._operation_goals: dict[str, OperationGoalRecord] = {}
        self._max_goal_feedback_messages = 50
        self._terminal_goal_retention_sec = 600.0
        self._max_retained_operation_goals = 100
        self._topic_cache: dict[str, dict[str, Any]] = {}
        self._topic_cache_subscriptions: dict[str, Any] = {}
        self._workflow_runs: dict[str, dict[str, Any]] = {}

        self._mode_executor = ActionClient(self.node, ModeExecutorAction, "/mission/mode_executor/action")
        self._gripper = self.node.create_client(GripperCommand, "/payload/charger_gripper/gripper_command")
        self._pl_mapper = self.node.create_client(PLMapperCommand, "/perception/pl_mapper/pl_mapper_command")
        self._update_powerline = self.node.create_client(
            UpdatePowerlineOverview,
            "/mission/powerline_overview_provider/update_powerline_overview",
        )
        self._get_powerline = self.node.create_client(
            GetPowerlineOverview,
            "/mission/powerline_overview_provider/get_powerline_overview",
        )
        self._store_pylon = self.node.create_client(
            StorePylonOverview,
            "/mission/pylon_overview_provider/store_pylon_overview",
        )
        self._get_pylon = self.node.create_client(
            GetPylonOverview,
            "/mission/pylon_overview_provider/get_pylon_overview",
        )
        self._clear_pylon = self.node.create_client(
            ClearPylonOverview,
            "/mission/pylon_overview_provider/clear_pylon_overview",
        )
        self._clear_queue = self.node.create_client(
            ClearManeuverQueue,
            "/control/maneuver_controller/clear_maneuver_queue",
        )
        self._maneuver_controller_get_state = self.node.create_client(
            LifecycleGetState,
            "/control/maneuver_controller/maneuver_controller/get_state",
        )
        self._get_parameter_yaml = self.node.create_client(
            GetParameterYaml,
            "/configuration/configuration_server/get_parameter_yaml",
        )
        self._get_declared_parameters = self.node.create_client(
            GetDeclaredParameters,
            "/configuration/configuration_server/get_declared_parameters",
        )
        self._save_parameters = self.node.create_client(
            SaveParameters,
            "/configuration/configuration_server/save_parameters",
        )
        self._get_parameter_files = self.node.create_client(
            GetParameterFiles,
            "/configuration/configuration_server/get_parameter_files",
        )
        self._load_parameters = self.node.create_client(
            LoadParameters,
            "/configuration/configuration_server/load_parameters",
        )
        self._set_parameter = self.node.create_client(
            SetParameterFromGC,
            "/configuration/configuration_server/set_parameter_from_gc",
        )
        self._get_current_parameter_file = self.node.create_client(
            GetCurrentParameterFile,
            "/configuration/configuration_server/get_current_parameter_file",
        )
        self._get_mission_catalog = self.node.create_client(
            GetMissionCatalog,
            "/mission/mission_executor/get_mission_catalog",
        )
        self._select_mission_catalog_entry = self.node.create_client(
            SelectMissionCatalogEntry,
            "/mission/mission_executor/select_mission_catalog_entry",
        )

    def close(self) -> None:
        if rclpy.ok():
            try:
                self.cancel_all_operation_goals(timeout_sec=2.0, reason="mcp process closing")
            except Exception as exc:
                self.node.get_logger().warn(f"failed to cancel active MCP operation goals during shutdown: {exc}")
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    def operation(self, name: str, **kwargs: Any) -> ToolResult:
        if name == "fly_relative":
            pose = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 5.0)))
            target_z = pose["z"] + float(kwargs.get("dz", 0.0))
            if "min_z" in kwargs:
                target_z = max(target_z, float(kwargs["min_z"]))
            return self.operation(
                "fly_to_position",
                frame_id=str(kwargs.get("frame_id", "world")),
                x=pose["x"] + float(kwargs.get("dx", 0.0)),
                y=pose["y"] + float(kwargs.get("dy", 0.0)),
                z=target_z,
                yaw=pose["yaw"] + float(kwargs.get("dyaw", 0.0)),
                ignore_altitude=bool(kwargs.get("ignore_altitude", False)),
                timeout_sec=kwargs.get("timeout_sec"),
            )
        method = getattr(self.operations, name)
        if name in {"fly_to_object", "hover_by_object"}:
            kwargs["target"] = self._target_from_dict(kwargs["target"])
        self._wait_operation_idle(timeout_sec=float(kwargs.pop("idle_timeout_sec", 3.0)))
        result: OperationResult = method(**kwargs)
        if not result.accepted and "goal rejected" in result.message:
            self._wait_operation_idle(timeout_sec=float(kwargs.pop("retry_idle_timeout_sec", 5.0)))
            result = method(**kwargs)
        if result.success:
            self._wait_operation_idle(timeout_sec=float(kwargs.pop("post_idle_timeout_sec", 5.0)))
        return ToolResult(result.success, asdict(result), result.message)

    def start_operation(self, name: str, **kwargs: Any) -> ToolResult:
        if name == "cable_aware_fly_to_position" and bool(kwargs.get("validate_sim_powerline_overview", True)):
            validation = self.validate_stored_powerline_overview_against_sim_geometry(
                geometry_path=str(kwargs.get("geometry_path", "")),
                max_line_error_m=float(kwargs.get("max_sim_powerline_overview_line_error_m", 1.5)),
                timeout_sec=float(kwargs.get("overview_timeout_sec", 2.0)),
            )
            if not validation.success:
                return validation
        action_name, client, goal, target_summary, reference_frame = self._operation_start_request(name, kwargs)
        return self._start_operation_goal(
            action_name=action_name,
            client=client,
            goal=goal,
            target_summary=target_summary,
            reference_frame=reference_frame,
            kwargs=kwargs,
        )

    def fly_to_fixture(
        self,
        position_id: str,
        *,
        operation_name: str = "auto",
        activate_custom_operation: bool = True,
        cancel_existing: bool = True,
        clear_queue: bool = False,
        use_cable_aware_if_overview_present: bool = True,
        min_overview_lines: int = 1,
        geometry_path: str = "",
        send_timeout_sec: float = 10.0,
        activation_timeout_sec: float = 5.0,
        activation_postcondition_timeout_sec: float = 10.0,
        activation_stable_sec: float = 0.5,
        mapping_timeout_sec: float = 30.0,
        gazebo_timeout_sec: float = 5.0,
        tf_timeout_sec: float = 2.0,
        **kwargs: Any,
    ) -> ToolResult:
        target = self.resolve_fixture_target(
            position_id=position_id,
            geometry_path=geometry_path,
            mapping_timeout_sec=mapping_timeout_sec,
            gazebo_timeout_sec=gazebo_timeout_sec,
            tf_timeout_sec=tf_timeout_sec,
        )
        if not target.success:
            return target

        selected_operation = str(operation_name or "auto")
        overview: dict[str, Any] | None = None
        invalid_overview_result: ToolResult | None = None
        if selected_operation == "auto":
            selected_operation = "fly_to_position"
            if bool(use_cable_aware_if_overview_present):
                try:
                    overview_result = self.get_powerline_overview(
                        min_lines=int(min_overview_lines),
                        timeout_sec=float(kwargs.get("overview_timeout_sec", 2.0)),
                        filename=f"stored_powerline_overview_for_{self._normalize_fixture_id(position_id)}.json",
                    )
                    if overview_result.success and bool(kwargs.get("validate_sim_powerline_overview", True)):
                        validation = self.validate_stored_powerline_overview_against_sim_geometry(
                            geometry_path=geometry_path,
                            max_line_error_m=float(kwargs.get("max_sim_powerline_overview_line_error_m", 1.5)),
                            timeout_sec=float(kwargs.get("overview_timeout_sec", 2.0)),
                        )
                        if not validation.success:
                            invalid_overview_result = validation
                            overview_result = validation
                    overview = {
                        "success": overview_result.success,
                        "message": overview_result.message,
                        "line_count": (overview_result.data or {}).get("line_count")
                        if isinstance(overview_result.data, dict)
                        else None,
                    }
                    if overview_result.success:
                        selected_operation = "cable_aware_fly_to_position"
                except Exception as exc:
                    overview = {"success": False, "message": f"overview check failed: {exc}", "line_count": None}

        if invalid_overview_result is not None:
            payload = dict(target.data or {})
            payload["overview_validation"] = invalid_overview_result.data
            payload["overview"] = overview
            return ToolResult(False, payload, invalid_overview_result.message)

        if selected_operation not in {"fly_to_position", "cable_aware_fly_to_position"}:
            return ToolResult(
                False,
                {"position_id": position_id, "operation_name": selected_operation},
                "operation_name must be auto, fly_to_position, or cable_aware_fly_to_position",
            )

        activation: ToolResult | None = None
        if bool(activate_custom_operation):
            activation = self.activate_custom_operation(
                timeout_sec=float(activation_timeout_sec),
                postcondition_timeout_sec=float(activation_postcondition_timeout_sec),
                stable_sec=float(activation_stable_sec),
                repeat_count=int(kwargs.get("activation_repeat_count", 5)),
            )
            if not activation.success:
                payload = dict(target.data or {})
                payload["activation"] = activation.data
                return ToolResult(False, payload, f"CustomOperation activation failed: {activation.message}")

        target_data = dict(target.data or {})
        result = self.start_operation(
            selected_operation,
            frame_id=str(target_data["frame_id"]),
            x=float(target_data["x"]),
            y=float(target_data["y"]),
            z=float(target_data["z"]),
            yaw=float(target_data["yaw"]),
            ignore_altitude=bool(kwargs.get("ignore_altitude", False)),
            send_timeout_sec=float(send_timeout_sec),
            cancel_existing=bool(cancel_existing),
            clear_queue=bool(clear_queue),
            clear_queue_timeout_sec=float(kwargs.get("clear_queue_timeout_sec", 10.0)),
        )
        data = dict(result.data or {})
        data["fixture"] = target_data
        data["selected_operation"] = selected_operation
        if overview is not None:
            data["overview_selection"] = overview
        if activation is not None:
            data["activation"] = activation.data
        return ToolResult(result.success, data, f"{selected_operation} fixture goal accepted" if result.success else result.message)

    def resolve_fixture_target(
        self,
        position_id: str,
        *,
        geometry_path: str = "",
        mapping_timeout_sec: float = 30.0,
        gazebo_timeout_sec: float = 5.0,
        tf_timeout_sec: float = 2.0,
    ) -> ToolResult:
        position = self._find_fixture_position(position_id, geometry_path=geometry_path)
        if position is None:
            return ToolResult(False, {"position_id": position_id, "geometry_path": geometry_path or str(DEFAULT_GEOMETRY_PATH)}, "fixture position not found")

        pose = dict(position.get("pose") or {})
        gazebo_pose = self._fixture_gazebo_pose(position)
        if gazebo_pose is not None:
            try:
                mapped = self._map_gazebo_fixture_to_live_ros_world(
                    gazebo_pose,
                    mapping_timeout_sec=float(mapping_timeout_sec),
                    gazebo_timeout_sec=float(gazebo_timeout_sec),
                    tf_timeout_sec=float(tf_timeout_sec),
                )
                mapped.update(
                    {
                        "position_id": position_id,
                        "normalized_position_id": self._normalize_fixture_id(position_id),
                        "frame_id": position.get("frame_id") or "world",
                        "fixture_label": position.get("label"),
                        "fixture_section": position.get("_fixture_section"),
                        "target_source": "gazebo_ground_truth_mapped_to_live_ros_world",
                    }
                )
                return ToolResult(True, mapped, f"resolved fixture {position_id} from Gazebo ground truth")
            except Exception as exc:
                if not {"x", "y", "z", "yaw"}.issubset(pose.keys()):
                    return ToolResult(
                        False,
                        {"position_id": position_id, "gazebo_ground_truth_pose": gazebo_pose, "error": str(exc)},
                        "failed to map Gazebo ground-truth fixture into live ROS world",
                    )

        if {"x", "y", "z", "yaw"}.issubset(pose.keys()):
            return ToolResult(
                True,
                {
                    "position_id": position_id,
                    "normalized_position_id": self._normalize_fixture_id(position_id),
                    "frame_id": position.get("frame_id") or "world",
                    "x": float(pose["x"]),
                    "y": float(pose["y"]),
                    "z": float(pose["z"]),
                    "yaw": float(pose["yaw"]),
                    "fixture_label": position.get("label"),
                    "fixture_section": position.get("_fixture_section"),
                    "target_source": "stored_ros_world_pose",
                },
                f"resolved fixture {position_id} from stored ROS pose",
            )
        return ToolResult(False, {"position_id": position_id, "position": position}, "fixture position is missing pose fields")

    def start_raw_operation(
        self,
        operation: str,
        arguments: Optional[dict[str, Any]] = None,
        arguments_json: Optional[str] = None,
        request_id: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        if arguments is not None and arguments_json is not None:
            raise ValueError("provide either arguments or arguments_json, not both")
        if arguments_json is None:
            arguments_json = json.dumps(arguments or {}, sort_keys=True)
        try:
            target_summary = json.loads(arguments_json)
            if not isinstance(target_summary, dict):
                target_summary = {"arguments_json": arguments_json}
        except json.JSONDecodeError:
            target_summary = {"arguments_json": arguments_json}
        goal = self.operations.build_raw_goal(str(operation), str(arguments_json), request_id=str(request_id))
        return self._start_operation_goal(
            action_name=str(operation),
            client=self.operations._run_operation,
            goal=goal,
            target_summary=target_summary,
            reference_frame=target_summary.get("frame_id") if isinstance(target_summary, dict) else None,
            kwargs=kwargs,
        )

    def _start_operation_goal(
        self,
        *,
        action_name: str,
        client: ActionClient,
        goal: Any,
        target_summary: dict[str, Any],
        reference_frame: str | None,
        kwargs: dict[str, Any],
    ) -> ToolResult:
        self._expire_stale_operation_records()
        if bool(kwargs.get("require_maneuver_controller_ready", True)):
            ready = self.wait_maneuver_controller_ready(
                timeout_sec=float(kwargs.get("maneuver_ready_timeout_sec", 20.0)),
                auto_recover=bool(kwargs.get("auto_recover_maneuver_controller", True)),
            )
            if not ready.success:
                return ToolResult(
                    False,
                    {
                        "error": "maneuver_controller_not_ready",
                        "readiness": ready.data,
                        "target_summary": target_summary,
                        "reference_frame": reference_frame,
                    },
                    ready.message,
                )

        active = self._active_operation_record()
        if active is not None and not bool(kwargs.get("cancel_existing", False)):
            return ToolResult(
                False,
                {
                    "error": "operation_already_active",
                    "active_goal": self._operation_goal_snapshot(active),
                    "operation_active": self.active_operation_goal().data,
                    "suggested_next_tools": ["operation.goal_status", "operation.cancel_goal", "operation.wait_goal"],
                },
                "another operation goal is active",
            )
        if active is not None and bool(kwargs.get("cancel_existing", False)):
            self.cancel_operation_goal(active.goal_id, timeout_sec=float(kwargs.get("cancel_timeout_sec", 3.0)))
            self._expire_stale_operation_records(force=True)
            active = self._active_operation_record()
            if active is not None:
                return ToolResult(
                    False,
                    {
                        "error": "operation_cancel_did_not_settle",
                        "active_goal": self._operation_goal_snapshot(active),
                        "operation_active": self.active_operation_goal().data,
                        "suggested_next_tools": ["operation.safety_stop", "operation.cancel_all", "maneuver.clear_queue"],
                    },
                    "existing operation did not settle after cancellation",
                )
        if bool(kwargs.get("clear_queue", False)):
            self.clear_maneuver_queue(reason=f"operation.start {action_name}", timeout_sec=float(kwargs.get("clear_queue_timeout_sec", 10.0)))

        send_timeout_sec = float(kwargs.get("send_timeout_sec", kwargs.get("timeout_sec", 10.0)))
        if send_timeout_sec <= 0.0:
            send_timeout_sec = 10.0
        if not client.wait_for_server(timeout_sec=send_timeout_sec):
            return ToolResult(
                False,
                self._operation_rejected_payload(action_name, target_summary, reference_frame, "action_server_unavailable"),
                f"{action_name} action server unavailable",
            )

        goal_id = str(uuid.uuid4())
        feedback_history: deque[Any] = deque(maxlen=int(kwargs.get("max_feedback_messages", self._max_goal_feedback_messages)))

        def on_feedback(feedback_msg: Any) -> None:
            record = self._operation_goals.get(goal_id)
            if record is None:
                return
            record.feedback_count += 1
            record.last_feedback_at = time.time()
            record.last_feedback = self._message_to_nested_dict(getattr(feedback_msg, "feedback", feedback_msg))
            feedback_history.append({"t": record.last_feedback_at, "feedback": record.last_feedback})

        send_future = client.send_goal_async(goal, feedback_callback=on_feedback)
        rclpy.spin_until_future_complete(self.node, send_future, timeout_sec=send_timeout_sec)
        if not send_future.done():
            return ToolResult(
                False,
                self._operation_rejected_payload(action_name, target_summary, reference_frame, "goal_send_timeout"),
                f"timed out sending {action_name} goal",
            )

        goal_handle = send_future.result()
        if not goal_handle or not goal_handle.accepted:
            return ToolResult(
                False,
                self._operation_rejected_payload(action_name, target_summary, reference_frame, "goal_rejected"),
                f"{action_name} goal rejected",
            )

        result_future = goal_handle.get_result_async()
        now = time.time()
        record = OperationGoalRecord(
            goal_id=goal_id,
            mcp_session_id=self._mcp_session_id,
            action_name=action_name,
            state="active",
            started_at=now,
            accepted_at=now,
            completed_at=None,
            cancel_requested_at=None,
            cancelled_at=None,
            failed_at=None,
            target_summary=target_summary,
            reference_frame=reference_frame,
            client=client,
            goal_handle=goal_handle,
            result_future=result_future,
            feedback_history=feedback_history,
            ros_goal_id=self._ros_goal_id(goal_handle),
        )
        self._operation_goals[goal_id] = record
        self._poll_operation_goal(record)
        return ToolResult(True, self._operation_goal_snapshot(record), f"{action_name} goal accepted")

    def operation_goal_status(self, goal_id: str) -> ToolResult:
        record = self._operation_goals.get(goal_id)
        if record is None:
            return self._unknown_operation_goal(goal_id)
        self._poll_operation_goal(record)
        return ToolResult(True, self._operation_goal_snapshot(record), record.state)

    def operation_goal_feedback(self, goal_id: str) -> ToolResult:
        record = self._operation_goals.get(goal_id)
        if record is None:
            return self._unknown_operation_goal(goal_id)
        self._poll_operation_goal(record)
        return ToolResult(
            True,
            {
                "goal": self._operation_goal_snapshot(record),
                "feedback_history": list(record.feedback_history or []),
            },
            "goal feedback",
        )

    def operation_goal_result(self, goal_id: str) -> ToolResult:
        record = self._operation_goals.get(goal_id)
        if record is None:
            return self._unknown_operation_goal(goal_id)
        self._poll_operation_goal(record)
        success = record.state in {"succeeded", "failed", "cancelled"}
        return ToolResult(success, self._operation_goal_snapshot(record), record.state)

    def wait_operation_goal(
        self,
        goal_id: str,
        max_wait_sec: Optional[float] = None,
        no_feedback_timeout_sec: Optional[float] = None,
        allow_no_feedback: bool = True,
    ) -> ToolResult:
        record = self._operation_goals.get(goal_id)
        if record is None:
            return self._unknown_operation_goal(goal_id)
        started = time.monotonic()
        while rclpy.ok():
            self._poll_operation_goal(record)
            if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
                return ToolResult(record.state == "succeeded", self._operation_goal_snapshot(record), record.state)
            now = time.monotonic()
            if max_wait_sec is not None and now - started >= float(max_wait_sec):
                return ToolResult(False, self._operation_goal_snapshot(record), "goal still active after wait timeout")
            if (
                no_feedback_timeout_sec is not None
                and not allow_no_feedback
                and time.time() - (record.last_feedback_at or record.accepted_at or record.started_at) > float(no_feedback_timeout_sec)
            ):
                return ToolResult(False, self._operation_goal_snapshot(record), "goal feedback stale")
            rclpy.spin_once(self.node, timeout_sec=0.1)
        return ToolResult(False, self._operation_goal_snapshot(record), "rclpy shutdown before goal completed")

    def cancel_operation_goal(self, goal_id: str, timeout_sec: Optional[float] = 3.0) -> ToolResult:
        record = self._operation_goals.get(goal_id)
        if record is None:
            return self._unknown_operation_goal(goal_id)
        self._poll_operation_goal(record)
        if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
            return ToolResult(True, self._operation_goal_snapshot(record), f"goal already terminal: {record.state}")
        record.cancel_requested_at = time.time()
        record.state = "cancel_requested"
        cancel_future = record.goal_handle.cancel_goal_async()
        rclpy.spin_until_future_complete(self.node, cancel_future, timeout_sec=timeout_sec)
        self._poll_operation_goal(record)
        self._expire_stale_operation_records(force=True)
        return ToolResult(cancel_future.done(), self._operation_goal_snapshot(record), "cancel requested")

    def cancel_all_operation_goals(self, timeout_sec: Optional[float] = 3.0, reason: str = "cancel all requested") -> ToolResult:
        cancelled: list[dict[str, Any]] = []
        for record in list(self._operation_goals.values()):
            self._poll_operation_goal(record)
            if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
                continue
            result = self.cancel_operation_goal(record.goal_id, timeout_sec=timeout_sec)
            snapshot = dict(result.data or {})
            snapshot["cancel_reason"] = reason
            cancelled.append(snapshot)
        return ToolResult(
            True,
            {
                "mcp_session_id": self._mcp_session_id,
                "cancelled_count": len(cancelled),
                "cancelled_goals": cancelled,
                "reason": reason,
            },
            "cancelled active operation goals",
        )

    def active_operation_goal(self) -> ToolResult:
        active = self._active_operation_record()
        queue = self._take_message("/control/maneuver_controller/maneuver_queue", ManeuverQueue, 0.5, required=False)
        return ToolResult(
            True,
            {
                "mcp_session_id": self._mcp_session_id,
                "active": active is not None,
                "goal": None if active is None else self._operation_goal_snapshot(active),
                "maneuver_queue": None if queue is None else self._message_to_nested_dict(queue),
            },
            "active operation goal" if active is not None else "no active operation goal",
        )

    def operation_safety_stop(
        self,
        mode: str = "hold",
        disarm_after_land: bool = False,
        force_clear_queue: bool = True,
        timeout_sec: float = 30.0,
    ) -> ToolResult:
        if mode not in {"hold", "land"}:
            raise ValueError("operation.safety_stop mode must be 'hold' or 'land'")
        events: list[dict[str, Any]] = []
        started_at = time.time()

        cancel_result = self.cancel_all_operation_goals(timeout_sec=min(5.0, timeout_sec), reason="operation.safety_stop")
        events.append({"step": "cancel_all", "success": cancel_result.success, "message": cancel_result.message, "data": cancel_result.data})
        cancel_settled = self._wait_no_active_operation_goal(timeout_sec=min(10.0, timeout_sec))
        events.append({"step": "wait_cancel_settled", "success": cancel_settled, "message": "operation cancellation settled" if cancel_settled else "operation cancellation still pending", "data": self.active_operation_goal().data})

        if force_clear_queue:
            try:
                clear_result = self.clear_maneuver_queue(reason="operation.safety_stop", timeout_sec=min(3.0, timeout_sec))
            except Exception as exc:
                clear_result = ToolResult(False, {"error": str(exc)}, "queue clear failed")
            events.append({"step": "clear_queue", "success": clear_result.success, "message": clear_result.message, "data": clear_result.data})

        px4_result = self._px4_tool_result_or_error(
            mode,
            timeout_sec=timeout_sec,
            postcondition_timeout_sec=timeout_sec,
        )
        events.append({"step": f"px4_{mode}", "success": px4_result.success, "message": px4_result.message, "data": px4_result.data})

        if mode == "land" and disarm_after_land:
            disarm_result = self._px4_tool_result_or_error(
                "disarm",
                timeout_sec=timeout_sec,
                postcondition_timeout_sec=min(10.0, timeout_sec),
            )
            events.append({"step": "px4_disarm", "success": disarm_result.success, "message": disarm_result.message, "data": disarm_result.data})

        final_px4 = self._px4_tool_result_or_error("status", timeout_sec=min(10.0, timeout_sec))
        events.append({"step": "px4_status", "success": final_px4.success, "message": final_px4.message, "data": final_px4.data})
        final_safety = self.px4_safety(timeout_sec=min(3.0, timeout_sec))
        events.append({"step": "px4_safety", "success": final_safety.success, "message": final_safety.message, "data": final_safety.data})
        final_operation = self.active_operation_goal()
        events.append({"step": "operation_active", "success": final_operation.success, "message": final_operation.message, "data": final_operation.data})

        success = all(event["success"] for event in events if event["step"] not in {"operation_active", "px4_status"})
        return ToolResult(
            success,
            {
                "started_at": started_at,
                "completed_at": time.time(),
                "mode": mode,
                "disarm_after_land": disarm_after_land,
                "events": events,
                "final_px4_status": final_px4.data,
                "final_px4_safety": final_safety.data,
                "final_operation_status": final_operation.data,
            },
            "safety stop complete" if success else "safety stop completed with errors",
        )

    def _px4_tool_result_or_error(self, command: str, **kwargs: Any) -> ToolResult:
        try:
            return self.px4(command, **kwargs)
        except Exception as exc:
            return ToolResult(
                False,
                {"command": command, "error": repr(exc), "kwargs": kwargs},
                f"PX4 {command} command raised before returning a tool result",
            )

    def _wait_no_active_operation_goal(self, timeout_sec: float = 5.0) -> bool:
        deadline = time.monotonic() + min(timeout_sec, 2.0)
        while rclpy.ok() and time.monotonic() < deadline:
            if self._active_operation_record() is None:
                return True
            rclpy.spin_once(self.node, timeout_sec=0.1)
        return self._active_operation_record() is None

    def list_operation_goals(self) -> ToolResult:
        self.prune_operation_goals()
        for record in list(self._operation_goals.values()):
            self._poll_operation_goal(record)
        return ToolResult(
            True,
            {
                "mcp_session_id": self._mcp_session_id,
                "goal_registry_started_at": self._goal_registry_started_at,
                "goals": [self._operation_goal_snapshot(record) for record in self._operation_goals.values()],
            },
            "operation goals",
        )

    def operation_goal_registry_status(self) -> ToolResult:
        prune_result = self.prune_operation_goals()
        for record in list(self._operation_goals.values()):
            self._poll_operation_goal(record)
        terminal = sum(1 for record in self._operation_goals.values() if record.state in {"succeeded", "failed", "cancelled", "rejected"})
        active = len(self._operation_goals) - terminal
        return ToolResult(
            True,
            {
                "mcp_session_id": self._mcp_session_id,
                "goal_registry_started_at": self._goal_registry_started_at,
                "goal_count": len(self._operation_goals),
                "active_goal_count": active,
                "terminal_goal_count": terminal,
                "last_prune": prune_result.data,
                "terminal_goal_retention_sec": self._terminal_goal_retention_sec,
                "max_retained_operation_goals": self._max_retained_operation_goals,
                "persistence": "process-local",
                "recoverable_after_process_restart": False,
                "unknown_goal_error": "unknown_goal_id",
            },
            "operation goal registry status",
        )

    def clear_completed_operation_goals(self) -> ToolResult:
        removed = []
        for goal_id, record in list(self._operation_goals.items()):
            self._poll_operation_goal(record)
            if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
                removed.append(self._operation_goal_snapshot(record))
                del self._operation_goals[goal_id]
        return ToolResult(True, {"removed_count": len(removed), "removed_goals": removed}, "completed operation goals cleared")

    def prune_operation_goals(
        self,
        retention_sec: Optional[float] = None,
        max_retained_goals: Optional[int] = None,
    ) -> ToolResult:
        retention = self._terminal_goal_retention_sec if retention_sec is None else float(retention_sec)
        max_retained = self._max_retained_operation_goals if max_retained_goals is None else int(max_retained_goals)
        now = time.time()
        removed: list[dict[str, Any]] = []
        terminal_records = []
        for goal_id, record in list(self._operation_goals.items()):
            self._poll_operation_goal(record)
            if record.state not in {"succeeded", "failed", "cancelled", "rejected"}:
                continue
            terminal_at = record.completed_at or record.cancelled_at or record.failed_at or record.accepted_at or record.started_at
            terminal_records.append((terminal_at, goal_id, record))
            if now - terminal_at > retention:
                removed.append(self._operation_goal_snapshot(record))
                del self._operation_goals[goal_id]
        terminal_records.sort(key=lambda item: item[0])
        while len(self._operation_goals) > max_retained and terminal_records:
            _, goal_id, record = terminal_records.pop(0)
            if goal_id in self._operation_goals:
                removed.append(self._operation_goal_snapshot(record))
                del self._operation_goals[goal_id]
        return ToolResult(
            True,
            {
                "removed_count": len(removed),
                "retention_sec": retention,
                "max_retained_goals": max_retained,
                "remaining_goal_count": len(self._operation_goals),
            },
            "operation goals pruned",
        )

    def discover_active_operation_goals(self, timeout_sec: float = 5.0) -> ToolResult:
        """Best-effort diagnostics for goals not known to this MCP process.

        ROS 2 action status topics expose status arrays, but they do not provide
        enough typed goal metadata to reconstruct this process-local registry.
        This tool reports the action/status surface so the agent can diagnose
        likely stale operations and then use safety recovery instead of assuming
        the handle can be recovered.
        """
        actions = self._run_tool_command(["ros2", "action", "list", "-t"], timeout_sec=timeout_sec, check=False)
        topics = self._run_tool_command(["ros2", "topic", "list"], timeout_sec=timeout_sec, check=False)
        actions_stdout = "" if actions.data is None else str(actions.data.get("stdout", ""))
        topics_stdout = "" if topics.data is None else str(topics.data.get("stdout", ""))
        action_lines = [
            line.strip()
            for line in actions_stdout.splitlines()
            if "/mission/custom_operation/" in line or "/control/maneuver_controller/" in line
        ]
        status_topics = [
            line.strip()
            for line in topics_stdout.splitlines()
            if (
                "/mission/custom_operation/" in line
                or "/control/maneuver_controller/" in line
            )
            and line.strip().endswith("/_action/status")
        ]
        return ToolResult(
            True,
            {
                "mcp_session_id": self._mcp_session_id,
                "goal_registry_started_at": self._goal_registry_started_at,
                "persistence": "process-local",
                "recoverable": False,
                "reason": "ROS action status does not include enough typed operation goal metadata to reconstruct MCP goal records after process restart.",
                "known_process_local_goals": [self._operation_goal_snapshot(record) for record in self._operation_goals.values()],
                "operation_action_servers": action_lines,
                "action_status_topics": status_topics,
                "suggested_recovery_tools": ["operation.cancel_all", "operation.safety_stop", "maneuver.clear_queue", "px4"],
            },
            "active goals are not recoverable across MCP process restarts",
        )

    def _operation_start_request(
        self,
        name: str,
        kwargs: dict[str, Any],
    ) -> tuple[str, ActionClient, Any, dict[str, Any], str | None]:
        if name == "fly_relative":
            pose = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 5.0)))
            dx = float(kwargs.get("dx", 0.0))
            dy = float(kwargs.get("dy", 0.0))
            dz = float(kwargs.get("dz", 0.0))
            target_z = pose["z"] + float(kwargs.get("dz", 0.0))
            if "min_z" in kwargs:
                target_z = max(target_z, float(kwargs["min_z"]))
            requested_displacement_m = math.sqrt(dx * dx + dy * dy + dz * dz)
            target_displacement_m = math.sqrt(
                dx * dx
                + dy * dy
                + (target_z - pose["z"]) * (target_z - pose["z"])
            )
            kwargs = {
                "frame_id": str(kwargs.get("frame_id", "world")),
                "x": pose["x"] + dx,
                "y": pose["y"] + dy,
                "z": target_z,
                "yaw": pose["yaw"] + float(kwargs.get("dyaw", 0.0)),
                "blend_to_next": bool(kwargs.get("blend_to_next", False)),
                "ignore_altitude": bool(kwargs.get("ignore_altitude", False)),
                "_relative_start": pose,
                "_relative_requested_displacement_m": requested_displacement_m,
                "_relative_target_displacement_m": target_displacement_m,
            }
            name = "fly_to_position"

        if name == "fly_to_position":
            arguments = {
                "frame_id": str(kwargs.get("frame_id", "world")),
                "x": float(kwargs["x"]),
                "y": float(kwargs["y"]),
                "z": float(kwargs["z"]),
                "yaw": float(kwargs["yaw"]),
                "blend_to_next": bool(kwargs.get("blend_to_next", False)),
                "ignore_altitude": bool(kwargs.get("ignore_altitude", False)),
            }
            return (
                "fly_to_position",
                self.operations._run_operation,
                self.operations.build_goal("fly_to_position", arguments),
                self._operation_target_summary(
                    {"x": arguments["x"], "y": arguments["y"], "z": arguments["z"], "yaw": arguments["yaw"]},
                    kwargs,
                ),
                arguments["frame_id"],
            )
        if name == "cable_aware_fly_to_position":
            arguments = {
                "frame_id": str(kwargs.get("frame_id", "world")),
                "x": float(kwargs["x"]),
                "y": float(kwargs["y"]),
                "z": float(kwargs["z"]),
                "yaw": float(kwargs["yaw"]),
                "ignore_altitude": bool(kwargs.get("ignore_altitude", False)),
            }
            return (
                "cable_aware_fly_to_position",
                self.operations._run_operation,
                self.operations.build_goal("cable_aware_fly_to_position", arguments),
                {"x": arguments["x"], "y": arguments["y"], "z": arguments["z"], "yaw": arguments["yaw"]},
                arguments["frame_id"],
            )
        if name == "hover":
            arguments = {
                "duration_s": float(kwargs["duration_s"]),
                "sustain_duration_s": float(kwargs.get("sustain_duration_s", 0.0)),
                "sustain_action": bool(kwargs.get("sustain_action", False)),
            }
            return (
                "hover",
                self.operations._run_operation,
                self.operations.build_goal("hover", arguments),
                arguments,
                None,
            )
        if name == "fly_to_object":
            arguments = self.operations._target_arguments(kwargs["target"])
            return (
                "fly_to_object",
                self.operations._run_operation,
                self.operations.build_goal("fly_to_object", arguments),
                {"target": kwargs["target"]},
                None,
            )
        if name == "hover_by_object":
            arguments = self.operations._target_arguments(kwargs["target"])
            arguments["duration_s"] = float(kwargs["duration_s"])
            arguments["sustain_action"] = bool(kwargs.get("sustain_action", False))
            return (
                "hover_by_object",
                self.operations._run_operation,
                self.operations.build_goal("hover_by_object", arguments),
                {"target": kwargs["target"], "duration_s": arguments["duration_s"]},
                None,
            )
        if name == "hover_on_cable":
            arguments = {
                "target_cable_id": int(kwargs["target_cable_id"]),
                "target_z_velocity": float(kwargs.get("target_z_velocity", 0.0)),
                "target_yaw_rate": float(kwargs.get("target_yaw_rate", 0.0)),
                "duration_s": float(kwargs["duration_s"]),
                "sustain_action": bool(kwargs.get("sustain_action", False)),
            }
            return (
                "hover_on_cable",
                self.operations._run_operation,
                self.operations.build_goal("hover_on_cable", arguments),
                {"target_cable_id": arguments["target_cable_id"], "duration_s": arguments["duration_s"]},
                None,
            )
        if name == "cable_landing":
            arguments = {"target_cable_id": int(kwargs["target_cable_id"])}
            return (
                "cable_landing",
                self.operations._run_operation,
                self.operations.build_goal("cable_landing", arguments),
                {"target_cable_id": arguments["target_cable_id"]},
                None,
            )
        if name == "cable_takeoff":
            arguments = {
                "target_cable_id": int(kwargs["target_cable_id"]),
                "target_cable_distance": float(kwargs["target_cable_distance"]),
            }
            return (
                "cable_takeoff",
                self.operations._run_operation,
                self.operations.build_goal("cable_takeoff", arguments),
                {"target_cable_id": arguments["target_cable_id"], "target_cable_distance": arguments["target_cable_distance"]},
                None,
            )
        raise ValueError(f"unknown operation start command: {name}")

    def _operation_target_summary(self, target: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        summary = dict(target)
        if "_relative_start" in request:
            target_displacement_m = float(request.get("_relative_target_displacement_m") or 0.0)
            summary["relative_start"] = request["_relative_start"]
            summary["requested_displacement_m"] = request.get("_relative_requested_displacement_m")
            summary["target_displacement_m"] = target_displacement_m
            summary["small_displacement_warning"] = (
                "Relative displacement is close to the configured 0.2 m maneuver success threshold; "
                "use a larger displacement for behavioral tests or inspect target progress explicitly."
                if target_displacement_m <= 0.25
                else None
            )
        return summary

    def _active_operation_record(self) -> OperationGoalRecord | None:
        self._expire_stale_operation_records()
        for record in self._operation_goals.values():
            self._poll_operation_goal(record)
            if record.state in {"accepted", "active", "cancel_requested"}:
                return record
        return None

    def _require_operation_goal(self, goal_id: str) -> OperationGoalRecord:
        record = self._operation_goals.get(goal_id)
        if record is None:
            raise KeyError(f"unknown_goal_id: {goal_id} (mcp_session_id={self._mcp_session_id})")
        return record

    def _unknown_operation_goal(self, goal_id: str) -> ToolResult:
        return ToolResult(
            False,
            {
                "error": "unknown_goal_id",
                "goal_id": goal_id,
                "mcp_session_id": self._mcp_session_id,
                "ros_goal_id": None,
                "action_name": None,
                "accepted": False,
                "state": "unknown",
                "started_at": None,
                "accepted_at": None,
                "completed_at": None,
                "cancel_requested_at": None,
                "cancelled_at": None,
                "failed_at": None,
                "feedback_count": 0,
                "last_feedback": None,
                "last_feedback_at": None,
                "last_feedback_age_sec": None,
                "result": None,
                "target_summary": {},
                "reference_frame": None,
                "goal_registry_started_at": self._goal_registry_started_at,
                "persistence": "process-local",
                "goal_not_recoverable": True,
                "mcp_session_mismatch": None,
                "message": "goal_id is unknown in this MCP process; handles are valid only for the process that created them",
                "suggested_next_tools": ["operation.goal_registry_status", "operation.discover_active_goals", "operation.cancel_all"],
            },
            f"unknown_goal_id: {goal_id}",
        )

    def _operation_rejected_payload(
        self,
        action_name: str,
        target_summary: dict[str, Any],
        reference_frame: str | None,
        error: str,
    ) -> dict[str, Any]:
        status = None
        try:
            status = self.operation_status(timeout_sec=1.0).data
        except Exception as exc:
            status = {"error": str(exc)}
        return {
            "goal_id": None,
            "mcp_session_id": self._mcp_session_id,
            "ros_goal_id": None,
            "action_name": action_name,
            "accepted": False,
            "state": "rejected",
            "started_at": None,
            "accepted_at": None,
            "completed_at": time.time(),
            "cancel_requested_at": None,
            "cancelled_at": None,
            "failed_at": None,
            "feedback_count": 0,
            "last_feedback": None,
            "last_feedback_at": None,
            "last_feedback_age_sec": None,
            "result": None,
            "target_summary": target_summary,
            "reference_frame": reference_frame,
            "error": error,
            "operation_status": status,
            "suggested_next_tools": ["operation.active", "operation.status", "operation.cancel_all", "maneuver.clear_queue"],
        }

    def _operation_goal_snapshot(self, record: OperationGoalRecord) -> dict[str, Any]:
        last_feedback_age = None if record.last_feedback_at is None else max(0.0, time.time() - record.last_feedback_at)
        return {
            "goal_id": record.goal_id,
            "mcp_session_id": record.mcp_session_id,
            "ros_goal_id": record.ros_goal_id,
            "action_name": record.action_name,
            "accepted": record.accepted_at is not None,
            "state": record.state,
            "started_at": record.started_at,
            "accepted_at": record.accepted_at,
            "completed_at": record.completed_at,
            "cancel_requested_at": record.cancel_requested_at,
            "cancelled_at": record.cancelled_at,
            "failed_at": record.failed_at,
            "feedback_count": record.feedback_count,
            "last_feedback": record.last_feedback,
            "last_feedback_at": record.last_feedback_at,
            "last_feedback_age_sec": last_feedback_age,
            "result": record.result,
            "error": record.error,
            "target_summary": record.target_summary,
            "reference_frame": record.reference_frame,
            "suggested_next_tools": ["operation.goal_status", "operation.goal_feedback", "operation.wait_goal", "operation.cancel_goal"],
        }

    def _poll_operation_goal(self, record: OperationGoalRecord) -> None:
        if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
            return
        try:
            rclpy.spin_once(self.node, timeout_sec=0.0)
        except Exception as exc:
            record.state = "failed"
            record.failed_at = record.failed_at or time.time()
            record.completed_at = record.completed_at or record.failed_at
            record.error = f"failed to poll goal result: {exc}"
            return
        if not record.result_future.done():
            if record.state != "cancel_requested":
                record.state = "active"
            return
        wrapped_result = record.result_future.result()
        status = int(wrapped_result.status)
        result = getattr(wrapped_result, "result", None)
        record.result = self._message_to_nested_dict(result)
        record.completed_at = record.completed_at or time.time()
        if status == GoalStatus.STATUS_SUCCEEDED and bool(getattr(result, "success", True)):
            record.state = "succeeded"
        elif status == GoalStatus.STATUS_CANCELED:
            record.state = "cancelled"
            record.cancelled_at = record.completed_at
        else:
            record.state = "failed"
            record.failed_at = record.completed_at
            record.error = f"goal finished with status={status}"

    @staticmethod
    def _ros_goal_id(goal_handle: Any) -> str | None:
        goal_id = getattr(goal_handle, "goal_id", None)
        if goal_id is None:
            return None
        uuid_value = getattr(goal_id, "uuid", goal_id)
        if isinstance(uuid_value, (bytes, bytearray)):
            return bytes(uuid_value).hex()
        if isinstance(uuid_value, (list, tuple)):
            return bytes(uuid_value).hex()
        return str(uuid_value)

    def cancel_operation(self, timeout_sec: Optional[float] = 2.0) -> ToolResult:
        cancelled = self.operations.cancel_active(timeout_sec=timeout_sec)
        return ToolResult(cancelled, {"cancelled": cancelled})

    def operation_status(self, timeout_sec: float = 5.0) -> ToolResult:
        status = self._run_tool_command(
            ["ros2", "topic", "echo", "--once", "/mission/custom_operation/status"],
            timeout_sec=timeout_sec,
            check=False,
        )
        system_status = self._run_tool_command(
            ["iii", "system", "status"],
            timeout_sec=timeout_sec,
            check=False,
        )
        actions = self._run_tool_command(
            ["ros2", "action", "list", "-t"],
            timeout_sec=timeout_sec,
            check=False,
        )
        custom_actions = [
            line for line in actions.data.get("stdout", "").splitlines()
            if "/mission/custom_operation/" in line
        ]
        data = {
            "status": status.data,
            "system_status": system_status.data,
            "custom_operation_actions": custom_actions,
            "process_local_active_goal": self.active_operation_goal().data,
        }
        system_stdout = system_status.data.get("stdout", "")
        success = "custom_operation: active" in system_stdout and bool(custom_actions)
        return ToolResult(success, data, "CustomOperation ready" if success else "CustomOperation not ready")

    def activate_custom_operation(
        self,
        timeout_sec: float = 5.0,
        postcondition_timeout_sec: float = 10.0,
        stable_sec: float = 1.0,
        repeat_count: int = 5,
        target_system: int = 1,
        target_component: int = 1,
        auto_recover: bool = True,
    ) -> ToolResult:
        status_result = self._wait_custom_operation_status(timeout_sec=timeout_sec, auto_recover=auto_recover)
        if not status_result.success:
            return status_result
        status = status_result.data["status"]
        try:
            status_data = json.loads(status.data)
            mode_id = int(status_data["mode_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid CustomOperation status payload: {getattr(status, 'data', None)!r}") from exc
        result = self._send_px4_nav_state(
            mode_id,
            target_system=target_system,
            target_component=target_component,
            repeat_count=repeat_count,
            postcondition_timeout_sec=postcondition_timeout_sec,
            stable_sec=stable_sec,
        )
        data = dict(result.data)
        data["custom_operation_mode_id"] = mode_id
        data["custom_operation_status_readiness"] = {
            key: value for key, value in status_result.data.items() if key != "status"
        }
        return ToolResult(result.success, data, "CustomOperation activated")

    def _wait_custom_operation_status(self, timeout_sec: float = 5.0, *, auto_recover: bool = True) -> ToolResult:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        recovery_attempted = False
        recovery_result: ToolResult | None = None
        last_error: str | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                status = self._take_message(
                    "/mission/custom_operation/status",
                    StringStamped,
                    min(1.0, remaining),
                    required=True,
                    qos_profile=QoSProfile(
                        reliability=ReliabilityPolicy.BEST_EFFORT,
                        durability=DurabilityPolicy.TRANSIENT_LOCAL,
                        history=HistoryPolicy.KEEP_LAST,
                        depth=1,
                    ),
                )
                return ToolResult(
                    True,
                    {
                        "status": status,
                        "auto_recover": bool(auto_recover),
                        "recovery_attempted": bool(recovery_attempted),
                        "recovery_result": None if recovery_result is None else recovery_result.data,
                    },
                    "CustomOperation status ready",
                )
            except TimeoutError as exc:
                last_error = str(exc)
                if auto_recover and not recovery_attempted:
                    recovery_attempted = True
                    recovery_result = self.system("start", timeout_sec=min(60.0, max(5.0, remaining)))
                else:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return ToolResult(
            False,
            {
                "error": last_error or "timeout",
                "auto_recover": bool(auto_recover),
                "recovery_attempted": bool(recovery_attempted),
                "recovery_result": None if recovery_result is None else recovery_result.data,
            },
            "CustomOperation status topic is not available",
        )

    def activate_mission_mode(
        self,
        mode_key: str = "reach_cable",
        timeout_sec: float = 5.0,
        postcondition_timeout_sec: float = 10.0,
        stable_sec: float = 1.0,
        repeat_count: int = 5,
        target_system: int = 1,
        target_component: int = 1,
    ) -> ToolResult:
        requested_mode_key = str(mode_key or "")
        topic_key = _mission_mode_topic_key(requested_mode_key)
        topic = f"/mission/modes/{topic_key}/status"
        try:
            status = self._take_message(topic, StringStamped, timeout_sec, required=True)
        except TimeoutError as exc:
            available_topics = [
                name for name, _types in self.node.get_topic_names_and_types()
                if name.startswith("/mission/modes/") and name.endswith("/status")
            ]
            raise TimeoutError(
                f"timed out waiting for topic: {topic} "
                f"(requested mode_key={requested_mode_key!r}, normalized={topic_key!r}, "
                f"available_mission_mode_status_topics={sorted(available_topics)!r})"
            ) from exc
        try:
            status_data = json.loads(status.data)
            mode_id = int(status_data["mode_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid mission mode status payload on {topic}: {getattr(status, 'data', None)!r}") from exc
        result = self._send_px4_nav_state(
            mode_id,
            target_system=target_system,
            target_component=target_component,
            repeat_count=repeat_count,
            postcondition_timeout_sec=postcondition_timeout_sec,
            stable_sec=stable_sec,
        )
        activation_status = self._wait_mission_mode_status_active(
            topic,
            mode_id=mode_id,
            timeout_sec=postcondition_timeout_sec,
            stable_sec=stable_sec,
        )
        data = dict(result.data)
        data["mission_mode_id"] = mode_id
        data["mission_mode_key"] = topic_key
        data["requested_mission_mode_key"] = requested_mode_key
        data["mission_mode_status_topic"] = topic
        data["mission_mode_status"] = status_data
        data["mission_mode_activation_status"] = activation_status
        return ToolResult(result.success, data, f"mission mode {topic_key} activated")

    def _wait_mission_mode_status_active(
        self,
        topic: str,
        *,
        mode_id: int,
        timeout_sec: float,
        stable_sec: float,
    ) -> dict[str, Any]:
        try:
            from px4_msgs.msg import VehicleStatus
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for mission-mode activation inspection") from exc

        latest_status: dict[str, Any] | None = None
        latest_vehicle_status: Any = None
        stable_since: float | None = None
        deadline = time.monotonic() + timeout_sec

        status_messages: list[StringStamped] = []
        vehicle_messages: list[Any] = []
        status_sub = self.node.create_subscription(
            StringStamped,
            topic,
            lambda message: status_messages.append(message),
            qos_profile_sensor_data,
        )
        vehicle_sub = self.node.create_subscription(
            VehicleStatus,
            "/fmu/out/vehicle_status_v1",
            lambda message: vehicle_messages.append(message),
            qos_profile_sensor_data,
        )
        try:
            while time.monotonic() < deadline and rclpy.ok():
                rclpy.spin_once(self.node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))

                while status_messages:
                    message = status_messages.pop(0)
                    try:
                        latest_status = json.loads(message.data)
                    except json.JSONDecodeError:
                        latest_status = {"raw": message.data, "parse_error": "invalid json"}

                while vehicle_messages:
                    latest_vehicle_status = vehicle_messages.pop(0)

                now = time.monotonic()
                status_ok = bool(
                    latest_status
                    and int(latest_status.get("mode_id", -1)) == int(mode_id)
                    and latest_status.get("active") is True
                    and latest_status.get("registered") is True
                    and latest_status.get("tree_running") is True
                    and latest_status.get("tree_finished") is False
                )
                vehicle_ok = bool(
                    latest_vehicle_status is not None
                    and int(latest_vehicle_status.nav_state) == int(mode_id)
                    and not bool(latest_vehicle_status.failsafe)
                )

                if status_ok and vehicle_ok:
                    if stable_since is None:
                        stable_since = now
                    if now - stable_since >= max(0.0, stable_sec):
                        return {
                            "mode_status": latest_status,
                            "nav_state": int(latest_vehicle_status.nav_state),
                            "nav_state_user_intention": int(latest_vehicle_status.nav_state_user_intention),
                            "failsafe": bool(latest_vehicle_status.failsafe),
                            "stable_sec_required": stable_sec,
                            "stable_sec_observed": now - stable_since,
                        }
                else:
                    stable_since = None
        finally:
            self.node.destroy_subscription(status_sub)
            self.node.destroy_subscription(vehicle_sub)

        raise TimeoutError(
            "timed out waiting for mission mode to become active and tree-running; "
            f"topic={topic}, mode_id={mode_id}, latest_status={latest_status}, "
            f"latest_nav_state={None if latest_vehicle_status is None else int(latest_vehicle_status.nav_state)}, "
            f"latest_failsafe={None if latest_vehicle_status is None else bool(latest_vehicle_status.failsafe)}"
        )

    def _wait_operation_idle(
        self,
        timeout_sec: float = 3.0,
        stable_sec: float = 0.3,
        *,
        require_reference_idle: bool = True,
    ) -> bool:
        deadline = time.monotonic() + timeout_sec
        stable_since: float | None = None
        latest_queue_idle = False
        latest_reference_idle = False

        def on_queue(msg: ManeuverQueue) -> None:
            nonlocal latest_queue_idle
            latest_queue_idle = msg.current_maneuver.maneuver_type == Maneuver.MANEUVER_TYPE_NONE

        def on_reference_mode(msg: StringStamped) -> None:
            nonlocal latest_reference_idle
            latest_reference_idle = msg.data in {"hover", "passthrough"}

        queue_sub = self.node.create_subscription(
            ManeuverQueue,
            "/control/maneuver_controller/maneuver_queue",
            on_queue,
            10,
        )
        reference_sub = self.node.create_subscription(
            StringStamped,
            "/mission/custom_operation/maneuver_reference_client/reference_mode",
            on_reference_mode,
            10,
        )
        try:
            while rclpy.ok() and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=0.05)
                reference_ok = latest_reference_idle or not require_reference_idle
                if latest_queue_idle and reference_ok:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    if time.monotonic() - stable_since >= stable_sec:
                        return True
                else:
                    stable_since = None
            return False
        finally:
            self.node.destroy_subscription(queue_sub)
            self.node.destroy_subscription(reference_sub)

    def clear_maneuver_queue(self, reason: str = "agent request", timeout_sec: float = 10.0) -> ToolResult:
        ready = self.wait_maneuver_controller_ready(timeout_sec=timeout_sec)
        if not ready.success:
            return ToolResult(False, ready.data, ready.message)
        request = ClearManeuverQueue.Request()
        request.reason = reason
        response = self._call_service(self._clear_queue, request, timeout_sec)
        return ToolResult(
            bool(response.success),
            {"cleared_count": int(response.cleared_count)},
            "queue cleared" if response.success else "queue clear failed",
        )

    def wait_maneuver_controller_ready(self, timeout_sec: float = 15.0, *, auto_recover: bool = True) -> ToolResult:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        last_state: dict[str, Any] = {"state": "unknown"}
        recovery_attempted = False
        recovery_result: ToolResult | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            state = self._maneuver_controller_state(timeout_sec=0.5)
            service_ready = self._clear_queue.service_is_ready() or self._clear_queue.wait_for_service(timeout_sec=0.1)
            last_state = {
                "lifecycle": state,
                "clear_queue_service_ready": bool(service_ready),
                "auto_recover": bool(auto_recover),
                "recovery_attempted": bool(recovery_attempted),
                "recovery_result": None if recovery_result is None else recovery_result.data,
            }
            if state.get("id") == LifecycleState.PRIMARY_STATE_ACTIVE and service_ready:
                return ToolResult(True, last_state, "maneuver controller ready")
            if auto_recover and not recovery_attempted and state.get("id") != LifecycleState.PRIMARY_STATE_ACTIVE:
                recovery_attempted = True
                recovery_timeout = min(60.0, max(5.0, deadline - time.monotonic()))
                recovery_result = self.system("start", timeout_sec=recovery_timeout)
                last_state["recovery_result"] = recovery_result.data
            rclpy.spin_once(self.node, timeout_sec=0.05)
        return ToolResult(False, last_state, "maneuver controller is not lifecycle-active")

    def _maneuver_controller_state(self, timeout_sec: float = 0.5) -> dict[str, Any]:
        if not self._maneuver_controller_get_state.wait_for_service(timeout_sec=timeout_sec):
            return {"available": False, "id": None, "label": "service_unavailable"}
        request = LifecycleGetState.Request()
        future = self._maneuver_controller_get_state.call_async(request)
        rclpy.spin_until_future_complete(self.node, future, timeout_sec=timeout_sec)
        if not future.done():
            return {"available": True, "id": None, "label": "state_timeout"}
        response = future.result()
        state = response.current_state
        return {"available": True, "id": int(state.id), "label": str(state.label)}

    def _maneuver_queue_idle(self, timeout_sec: float = 0.2) -> bool:
        queue = self._take_message("/control/maneuver_controller/maneuver_queue", ManeuverQueue, timeout_sec, required=False)
        if queue is None:
            return False
        current = queue.current_maneuver
        return bool(current.terminated) and int(current.maneuver_type) < 0 and len(queue.scheduled_maneuvers) == 0

    def _expire_stale_operation_records(self, *, force: bool = False) -> None:
        now = time.time()
        queue_idle: bool | None = None
        for record in list(self._operation_goals.values()):
            self._poll_operation_goal(record)
            if record.state in {"succeeded", "failed", "cancelled", "rejected"}:
                continue
            age = now - (record.accepted_at or record.started_at)
            cancel_age = None if record.cancel_requested_at is None else now - record.cancel_requested_at
            if queue_idle is None:
                queue_idle = self._maneuver_queue_idle(timeout_sec=0.05)
            if record.state == "cancel_requested" and (force or (cancel_age is not None and cancel_age > 2.0)) and queue_idle:
                record.state = "cancelled"
                record.cancelled_at = record.cancelled_at or now
                record.completed_at = record.completed_at or record.cancelled_at
                record.error = record.error or "cancel request did not receive terminal action result; maneuver queue is idle"
                continue
            if record.state == "active" and record.feedback_count == 0 and age > 30.0 and queue_idle:
                record.state = "failed"
                record.failed_at = record.failed_at or now
                record.completed_at = record.completed_at or record.failed_at
                record.error = record.error or "operation accepted but produced no feedback and maneuver queue is idle"

    def mission_executor_action(
        self,
        request: str,
        *,
        takeoff_altitude: float = 1.0,
        force_disarm: bool = False,
        timeout_sec: Optional[float] = None,
    ) -> ToolResult:
        request_map = {
            "takeoff": ModeExecutorAction.Goal.REQUEST_TAKEOFF,
            "land": ModeExecutorAction.Goal.REQUEST_LAND,
            "arm": ModeExecutorAction.Goal.REQUEST_ARM,
            "disarm": ModeExecutorAction.Goal.REQUEST_DISARM,
        }
        if request not in request_map:
            raise ValueError(f"unknown mission executor request: {request}")
        goal = ModeExecutorAction.Goal()
        goal.request = request_map[request]
        goal.takeoff_altitude = float(takeoff_altitude)
        goal.force_disarm = bool(force_disarm)
        return self._send_action(self._mode_executor, goal, request, timeout_sec=timeout_sec)

    def gripper(self, command: str, timeout_sec: float = 2.0) -> ToolResult:
        request = GripperCommand.Request()
        if command == "open":
            request.gripper_command = GripperCommand.Request.GRIPPER_COMMAND_OPEN
        elif command == "close":
            request.gripper_command = GripperCommand.Request.GRIPPER_COMMAND_CLOSE
        else:
            raise ValueError("gripper command must be 'open' or 'close'")
        response = self._call_service(self._gripper, request, timeout_sec)
        success = response.gripper_command_response == GripperCommand.Response.GRIPPER_COMMAND_RESPONSE_SUCCESS
        return ToolResult(success, {"response": int(response.gripper_command_response)})

    def pl_mapper(self, command: str, reset: bool = False, timeout_sec: float = 2.0) -> ToolResult:
        command_map = {
            "start": PLMapperCommandMsg.PL_MAPPER_CMD_START,
            "stop": PLMapperCommandMsg.PL_MAPPER_CMD_STOP,
            "pause": PLMapperCommandMsg.PL_MAPPER_CMD_PAUSE,
            "freeze": PLMapperCommandMsg.PL_MAPPER_CMD_FREEZE,
        }
        if command not in command_map:
            raise ValueError("pl_mapper command must be start, stop, pause, or freeze")
        request = PLMapperCommand.Request()
        request.pl_mapper_cmd.command = command_map[command]
        request.pl_mapper_cmd.reset = bool(reset)
        response = self._call_service(self._pl_mapper, request, timeout_sec)
        success = response.pl_mapper_ack == PLMapperCommand.Response.PL_MAPPER_ACK_SUCCESS
        return ToolResult(success, {"ack": int(response.pl_mapper_ack)})

    def update_powerline_overview(
        self,
        timeout_s: int = 5,
        service_timeout_sec: float | None = None,
        timeout_sec: float | None = None,
    ) -> ToolResult:
        request = UpdatePowerlineOverview.Request()
        request.timeout_s = int(timeout_s)
        call_timeout_sec = service_timeout_sec
        if call_timeout_sec is None:
            call_timeout_sec = timeout_sec if timeout_sec is not None else 15.0
        response = self._call_service(self._update_powerline, request, call_timeout_sec)
        return ToolResult(bool(response.success), {"success": bool(response.success)})

    def get_powerline_overview(
        self,
        min_lines: int = 1,
        timeout_sec: float = 2.0,
        filename: str = "stored_powerline_overview.json",
    ) -> ToolResult:
        response = self._call_service(self._get_powerline, GetPowerlineOverview.Request(), timeout_sec)
        line_count = len(response.stored_powerline.lines)
        data = {
            "success": bool(response.success),
            "overview_in_frame": bool(getattr(response, "overview_in_frame", bool(response.success))),
            "overview_gnss_only": bool(getattr(response, "overview_gnss_only", False)),
            "overview_source": str(getattr(response, "overview_source", "unknown")),
            "line_count": line_count,
            "min_lines": int(min_lines),
            "stored_powerline": self._message_to_nested_dict(response.stored_powerline),
        }
        if response.success:
            artifact = self._write_json_artifact(filename, data)
            data["artifact_path"] = str(artifact)
        success = bool(response.success) and line_count >= int(min_lines)
        return ToolResult(
            success,
            data,
            f"stored powerline overview has {line_count} line(s)"
            if response.success
            else "no stored powerline overview",
        )

    def validate_stored_powerline_overview_against_sim_geometry(
        self,
        *,
        geometry_path: str = "",
        max_line_error_m: float = 1.5,
        timeout_sec: float = 2.0,
    ) -> ToolResult:
        overview_result = self.get_powerline_overview(
            min_lines=1,
            timeout_sec=timeout_sec,
            filename="stored_powerline_overview_for_sim_validation.json",
        )
        if not overview_result.success:
            return overview_result

        try:
            geometry = load_geometry(self._workspace_root, geometry_path or None)
        except Exception as exc:
            return ToolResult(False, {"error": str(exc), "geometry_path": geometry_path}, "failed to load simulation geometry")

        stored_powerline = (overview_result.data or {}).get("stored_powerline", {})
        lines = list(stored_powerline.get("lines", []))
        conductors = list(geometry.conductors)
        if len(lines) != len(conductors):
            return ToolResult(
                False,
                {"line_count": len(lines), "conductor_count": len(conductors)},
                "stored powerline overview line count does not match simulation conductor count",
            )

        try:
            live_ros = self._lookup_world_drone_pose(timeout_sec=timeout_sec)
            live_gazebo = self._lookup_gazebo_drone_model_pose(timeout_sec=timeout_sec)
        except Exception as exc:
            return ToolResult(
                False,
                {"error": repr(exc), "geometry_path": geometry_path},
                "failed to map simulation geometry into live ROS world for powerline overview validation",
            )
        yaw_offset = self._normalize_angle(float(live_ros["yaw"]) - float(live_gazebo["yaw"]))

        def map_gazebo_point_to_ros(point: dict[str, Any]) -> dict[str, float]:
            dx = float(point["x"]) - float(live_gazebo["x"])
            dy = float(point["y"]) - float(live_gazebo["y"])
            dz = float(point["z"]) - float(live_gazebo["z"])
            delta_ros_x, delta_ros_y = rotate_gazebo_xy_delta_to_ros(dx, dy)
            return {
                "x": float(live_ros["x"]) + delta_ros_x,
                "y": float(live_ros["y"]) + delta_ros_y,
                "z": float(live_ros["z"]) + dz,
            }

        def line_z(line: dict[str, Any]) -> float:
            return float(line.get("pose", {}).get("position", {}).get("z", 0.0))

        def conductor_mean_z(conductor: dict[str, Any]) -> float:
            samples = conductor_samples(conductor)
            if not samples:
                return 0.0
            return sum(float(sample["z"]) for sample in samples) / len(samples)

        matched_lines = sorted(lines, key=line_z, reverse=True)
        matched_conductors = sorted(conductors, key=conductor_mean_z, reverse=True)
        comparisons: list[dict[str, Any]] = []
        max_error = 0.0

        for line, conductor in zip(matched_lines, matched_conductors):
            position = point_from_any(line.get("pose", {}).get("position", {"x": 0.0, "y": 0.0, "z": 0.0}))
            samples = [map_gazebo_point_to_ros(sample) for sample in conductor_samples(conductor)]
            projection = None
            for sample_start, sample_end in zip(samples, samples[1:]):
                candidate = self._segment_projection_for_validation(position, sample_start, sample_end)
                if projection is None or candidate["distance_m"] < projection["distance_m"]:
                    projection = candidate
            if projection is None:
                projection = {"distance_m": math.inf, "closest": None}
            error = float(projection["distance_m"])
            max_error = max(max_error, error)
            comparisons.append(
                {
                    "line_id": line.get("id"),
                    "line_position": position,
                    "matched_conductor_id": conductor.get("id"),
                    "distance_m": error,
                    "closest_point": projection.get("closest"),
                }
            )

        payload = {
            "max_line_error_m": max_error,
            "allowed_max_line_error_m": float(max_line_error_m),
            "comparisons": comparisons,
            "live_mapping": {
                "method": "simulation conductor samples mapped into live ROS world before comparison",
                "live_ros_drone_pose": live_ros,
                "live_gazebo_drone_pose": live_gazebo,
                "yaw_offset": yaw_offset,
                "position_yaw_offset": GAZEBO_TO_ROS_POSITION_YAW_RAD,
            },
        }
        if max_error > float(max_line_error_m):
            return ToolResult(
                False,
                payload,
                f"stored powerline overview does not match simulation geometry: max line error {max_error:.2f} m > {float(max_line_error_m):.2f} m",
            )
        return ToolResult(True, payload, f"stored powerline overview matches simulation geometry within {max_error:.2f} m")

    def validate_cable_aware_target_clearance(
        self,
        *,
        x: float,
        y: float,
        z: float,
        frame_id: str = "world",
        required_clearance_m: float | None = None,
        timeout_sec: float = 2.0,
    ) -> ToolResult:
        if str(frame_id or "world") != "world":
            return ToolResult(
                False,
                {"frame_id": frame_id},
                "cable-aware target clearance validation currently requires world frame target",
            )

        overview_result = self.get_powerline_overview(
            min_lines=1,
            timeout_sec=timeout_sec,
            filename="stored_powerline_overview_for_target_clearance.json",
        )
        if not overview_result.success:
            return overview_result

        target = {"x": float(x), "y": float(y), "z": float(z)}
        stored_powerline = (overview_result.data or {}).get("stored_powerline", {})
        lines = list(stored_powerline.get("lines", []))
        direction = self._powerline_direction_from_overview(stored_powerline)
        clearance_source = "argument"
        if required_clearance_m is None:
            required_clearance_m, configured = self._configured_double_parameter(
                "/control/trajectory_generator/cable_aware_clearance_m",
                fallback=1.0,
                timeout_sec=timeout_sec,
            )
            clearance_source = "configuration" if configured else "default"

        nearest: dict[str, Any] | None = None
        distances: list[dict[str, Any]] = []
        for line in lines:
            point = point_from_any(line.get("pose", {}).get("position", {"x": 0.0, "y": 0.0, "z": 0.0}))
            projection = self._point_to_infinite_line_distance(target, point, direction)
            candidate = {
                "line_id": line.get("id"),
                "distance_m": projection["distance_m"],
                "closest_point": projection["closest"],
                "line_position": point,
            }
            distances.append(candidate)
            if nearest is None or candidate["distance_m"] < nearest["distance_m"]:
                nearest = candidate

        nearest_distance = float(nearest["distance_m"]) if nearest else math.inf
        ok = nearest_distance >= float(required_clearance_m)
        payload = {
            "target": target,
            "frame_id": frame_id,
            "line_count": len(lines),
            "required_clearance_m": float(required_clearance_m),
            "required_clearance_source": clearance_source,
            "nearest_distance_m": nearest_distance,
            "nearest_line": nearest,
            "distances": distances,
            "powerline_direction": direction,
        }
        if not ok:
            return ToolResult(
                False,
                payload,
                (
                    "cable-aware target violates stored powerline clearance: "
                    f"nearest line distance {nearest_distance:.2f} m < required {float(required_clearance_m):.2f} m"
                ),
            )
        return ToolResult(
            True,
            payload,
            f"cable-aware target clearance ok: nearest line distance {nearest_distance:.2f} m",
        )

    @staticmethod
    def _segment_projection_for_validation(point: dict[str, float], start: dict[str, float], end: dict[str, float]) -> dict[str, Any]:
        segment = {"x": end["x"] - start["x"], "y": end["y"] - start["y"], "z": end["z"] - start["z"]}
        offset = {"x": point["x"] - start["x"], "y": point["y"] - start["y"], "z": point["z"] - start["z"]}
        length_sq = segment["x"] ** 2 + segment["y"] ** 2 + segment["z"] ** 2
        t = 0.0 if length_sq <= 1.0e-12 else max(
            0.0,
            min(1.0, (offset["x"] * segment["x"] + offset["y"] * segment["y"] + offset["z"] * segment["z"]) / length_sq),
        )
        closest = {"x": start["x"] + t * segment["x"], "y": start["y"] + t * segment["y"], "z": start["z"] + t * segment["z"]}
        return {"t": t, "closest": closest, "distance_m": distance(point, closest)}

    @staticmethod
    def _point_to_infinite_line_distance(point: dict[str, float], line_point: dict[str, float], direction: dict[str, float]) -> dict[str, Any]:
        unit = {
            "x": float(direction["x"]),
            "y": float(direction["y"]),
            "z": float(direction["z"]),
        }
        length = math.sqrt(unit["x"] ** 2 + unit["y"] ** 2 + unit["z"] ** 2)
        if length <= 1.0e-9:
            unit = {"x": 1.0, "y": 0.0, "z": 0.0}
        else:
            unit = {"x": unit["x"] / length, "y": unit["y"] / length, "z": unit["z"] / length}
        delta = {
            "x": float(point["x"]) - float(line_point["x"]),
            "y": float(point["y"]) - float(line_point["y"]),
            "z": float(point["z"]) - float(line_point["z"]),
        }
        along = delta["x"] * unit["x"] + delta["y"] * unit["y"] + delta["z"] * unit["z"]
        closest = {
            "x": float(line_point["x"]) + along * unit["x"],
            "y": float(line_point["y"]) + along * unit["y"],
            "z": float(line_point["z"]) + along * unit["z"],
        }
        return {"closest": closest, "distance_m": distance(point, closest), "along_m": along}

    @staticmethod
    def _powerline_direction_from_overview(stored_powerline: dict[str, Any]) -> dict[str, float]:
        normal = stored_powerline.get("projection_plane", {}).get("normal", {})
        direction = {
            "x": float(normal.get("x", 0.0)),
            "y": float(normal.get("y", 0.0)),
            "z": float(normal.get("z", 0.0)),
        }
        length = math.sqrt(direction["x"] ** 2 + direction["y"] ** 2 + direction["z"] ** 2)
        if length <= 1.0e-9:
            return {"x": 1.0, "y": 0.0, "z": 0.0}
        return {"x": direction["x"] / length, "y": direction["y"] / length, "z": direction["z"] / length}

    def _configured_double_parameter(self, parameter_name: str, *, fallback: float, timeout_sec: float = 2.0) -> tuple[float, bool]:
        try:
            config = self.configuration("get_yaml", timeout_sec=timeout_sec)
            parsed = yaml.safe_load((config.data or {}).get("yaml", "")) or {}
            value = self._find_nested_parameter_value(parsed, parameter_name)
            if value is not None:
                return float(value), True
        except Exception as exc:
            self.node.get_logger().warn(f"failed to read configured parameter {parameter_name}: {exc}")
        return float(fallback), False

    @staticmethod
    def _find_nested_parameter_value(value: Any, parameter_name: str) -> Any:
        leaf_name = str(parameter_name).rstrip("/").split("/")[-1]
        if isinstance(value, dict):
            if parameter_name in value:
                direct = value[parameter_name]
                if isinstance(direct, dict) and "value" in direct:
                    return direct["value"]
                return direct
            if leaf_name in value:
                direct = value[leaf_name]
                if isinstance(direct, dict) and "value" in direct:
                    return direct["value"]
                return direct
            for key, child in value.items():
                if key == "name" and child == parameter_name and "value" in value:
                    return value["value"]
                found = DroneAgentTools._find_nested_parameter_value(child, parameter_name)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = DroneAgentTools._find_nested_parameter_value(child, parameter_name)
                if found is not None:
                    return found
        return None

    def store_pylon_overview(
        self,
        *,
        pylon_id: int,
        x: float,
        y: float,
        frame_id: str = "world",
        timeout_sec: float = 2.0,
        filename: str = "stored_pylon_overview.json",
    ) -> ToolResult:
        request = StorePylonOverview.Request()
        request.id = int(pylon_id)
        request.x = float(x)
        request.y = float(y)
        request.frame_id = str(frame_id)
        response = self._call_service(self._store_pylon, request, timeout_sec)
        pylon_count = len(response.stored_pylon_overview.pylons)
        data = {
            "success": bool(response.success),
            "message": str(response.message),
            "pylon_count": pylon_count,
            "stored_pylon_overview": self._message_to_nested_dict(response.stored_pylon_overview),
        }
        if response.success:
            data["artifact_path"] = str(self._write_json_artifact(filename, data))
        return ToolResult(bool(response.success), data, str(response.message))

    def get_pylon_overview(
        self,
        min_pylons: int = 2,
        timeout_sec: float = 2.0,
        filename: str = "stored_pylon_overview.json",
    ) -> ToolResult:
        response = self._call_service(self._get_pylon, GetPylonOverview.Request(), timeout_sec)
        pylon_count = len(response.stored_pylon_overview.pylons)
        data = {
            "success": bool(response.success),
            "valid": bool(response.valid),
            "message": str(response.message),
            "overview_in_frame": bool(getattr(response, "overview_in_frame", bool(response.valid))),
            "overview_gnss_only": bool(getattr(response, "overview_gnss_only", False)),
            "overview_source": str(getattr(response, "overview_source", "unknown")),
            "pylon_count": pylon_count,
            "min_pylons": int(min_pylons),
            "stored_pylon_overview": self._message_to_nested_dict(response.stored_pylon_overview),
        }
        if response.success:
            data["artifact_path"] = str(self._write_json_artifact(filename, data))
        success = bool(response.success) and bool(response.valid) and pylon_count >= int(min_pylons)
        return ToolResult(
            success,
            data,
            f"stored pylon overview has {pylon_count} pylon(s)"
            if response.success
            else "no valid stored pylon overview",
        )

    def clear_pylon_overview(self, timeout_sec: float = 2.0) -> ToolResult:
        response = self._call_service(self._clear_pylon, ClearPylonOverview.Request(), timeout_sec)
        return ToolResult(bool(response.success), {"success": bool(response.success), "message": str(response.message)}, str(response.message))

    def set_bool_service(
        self,
        *,
        service_name: str,
        value: bool,
        timeout_sec: float = 2.0,
    ) -> ToolResult:
        service_name = str(service_name)
        if not service_name.startswith("/"):
            raise ValueError("service_name must be an absolute ROS service name")
        client = self.node.create_client(SetBool, service_name)
        try:
            request = SetBool.Request()
            request.data = bool(value)
            response = self._call_service(client, request, timeout_sec)
            return ToolResult(
                bool(response.success),
                {
                    "service_name": service_name,
                    "value": bool(value),
                    "success": bool(response.success),
                    "message": str(response.message),
                },
                str(response.message),
            )
        finally:
            self.node.destroy_client(client)

    def get_mission_catalog(
        self,
        *,
        include_incompatible: bool = False,
        timeout_sec: float = 5.0,
    ) -> ToolResult:
        request = GetMissionCatalog.Request()
        request.include_incompatible = bool(include_incompatible)
        response = self._call_service(self._get_mission_catalog, request, timeout_sec)
        catalog = json.loads(str(response.catalog_json)) if response.success and response.catalog_json else {}
        data = {
            "success": bool(response.success),
            "message": str(response.message),
            "include_incompatible": bool(request.include_incompatible),
            "catalog": catalog,
        }
        return ToolResult(bool(response.success), data, str(response.message))

    def select_mission_catalog_entry(
        self,
        *,
        catalog_id: str = "",
        use_default: bool = False,
        timeout_sec: float = 10.0,
    ) -> ToolResult:
        request = SelectMissionCatalogEntry.Request()
        request.catalog_id = str(catalog_id or "")
        request.use_default = bool(use_default)
        response = self._call_service(self._select_mission_catalog_entry, request, timeout_sec)
        data = {
            "success": bool(response.success),
            "message": str(response.message),
            "catalog_id": request.catalog_id,
            "use_default": bool(request.use_default),
            "active_catalog_id": str(response.active_catalog_id),
            "active_entry_hash": str(response.active_entry_hash),
            "temporary_override": bool(response.temporary_override),
            "warning": str(response.warning),
        }
        return ToolResult(bool(response.success), data, str(response.message))

    def mission_status(self, timeout_sec: float = 2.0) -> ToolResult:
        status = self._take_message("/mission/status", MissionModeStatus, timeout_sec, required=True)
        data = {
            "mission_state": int(status.mission_state),
            "mission_state_label": str(status.mission_state_label),
            "active_catalog_id": str(status.active_catalog_id),
            "catalog_hash": str(status.catalog_hash),
            "active_entry_hash": str(status.active_entry_hash),
            "default_catalog_id": str(status.default_catalog_id),
            "configuration_profile": str(status.configuration_profile),
            "classification": str(status.classification),
            "compatible_profiles": list(status.compatible_profiles),
            "temporary_override": bool(status.temporary_override),
            "experimental": bool(status.experimental),
            "experimental_warning": str(status.experimental_warning),
            "catalog_ready": bool(status.catalog_ready),
            "catalog_error": str(status.catalog_error),
            "mission_active": bool(status.mission_active),
            "required_modes_registered": bool(status.required_modes_registered),
            "required_modes": list(status.required_modes),
            "registered_modes": list(status.registered_modes),
            "owned_mode": str(status.owned_mode),
            "control_owner": str(status.control_owner),
            "ready": bool(status.ready),
            "degraded": bool(status.degraded),
            "degraded_reason": str(status.degraded_reason),
            "degraded_reasons": list(status.degraded_reasons),
        }
        return ToolResult(True, data, "mission status received")

    def wait_powerline_lines(
        self,
        topic: str = "/perception/pl_mapper/powerline",
        min_lines: int = 4,
        timeout_sec: float = 10.0,
        filename: str = "powerline_latest.json",
    ) -> ToolResult:
        deadline = time.monotonic() + float(timeout_sec)
        latest: Any = None
        latest_count = 0
        while time.monotonic() < deadline and rclpy.ok():
            latest = self._take_message(topic, Powerline, min(0.5, max(0.1, deadline - time.monotonic())), required=False)
            if latest is not None:
                latest_count = len(latest.lines)
                if latest_count >= int(min_lines):
                    break
        data: dict[str, Any] = {
            "topic": topic,
            "min_lines": int(min_lines),
            "line_count": latest_count,
            "message": self._message_to_nested_dict(latest),
        }
        if latest is not None:
            artifact = self._write_json_artifact(filename, data)
            data["artifact_path"] = str(artifact)
        success = latest_count >= int(min_lines)
        return ToolResult(
            success,
            data,
            f"observed {latest_count} powerline lines"
            if latest is not None
            else f"no powerline message on {topic}",
        )

    def start_mission_deploy_workflow(self, **kwargs: Any) -> ToolResult:
        workflow_id = str(kwargs.get("workflow_id") or f"mission_deploy_{uuid.uuid4().hex[:12]}")
        artifact_dir = Path(kwargs.get("artifact_dir") or Path("/tmp/iii_drone/mission_deploy") / workflow_id)
        artifact_dir = self._resolve_artifact_dir(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        status_path = artifact_dir / "status.json"
        log_path = artifact_dir / "workflow.log"
        self._write_json_path(
            status_path,
            {
                "workflow_id": workflow_id,
                "state": "starting",
                "started_at": self._utc_now_iso(),
                "artifact_dir": str(artifact_dir),
                "status_path": str(status_path),
                "log_path": str(log_path),
            },
        )

        command = [
            sys.executable,
            "-m",
            "iii_drone_mcp.mission_deploy_workflow",
            "--workflow-id",
            workflow_id,
            "--artifact-dir",
            str(artifact_dir),
            "--status-path",
            str(status_path),
        ]
        for option in (
            "position_id",
            "frame_id",
            "x",
            "y",
            "z",
            "yaw",
            "mission_start_position_id",
            "mission_start_frame_id",
            "mission_start_x",
            "mission_start_y",
            "mission_start_z",
            "mission_start_yaw",
            "takeoff_altitude",
            "min_powerline_lines",
            "powerline_timeout_sec",
            "overview_timeout_s",
            "overview_service_timeout_sec",
            "overview_query_timeout_sec",
            "overview_store_attempts",
            "overview_retry_delay_sec",
            "min_pylons",
            "pylon_overview_timeout_sec",
            "demo_pos_over_corridor_id",
            "demo_pos_pylon_1_id",
            "demo_pos_pylon_2_id",
            "mission_catalog_id",
            "mission_mode",
            "px4_timeout_sec",
            "custom_mode_timeout_sec",
            "fly_send_timeout_sec",
            "fly_wait_timeout_sec",
            "fly_feedback_stale_timeout_sec",
            "position_timeout_sec",
            "position_tolerance_m",
            "position_settle_sec",
            "minimum_staging_z",
            "minimum_staging_above_ground",
            "staging_ground_clearance_margin",
            "ground_estimate_timeout_sec",
        ):
            if option in kwargs and kwargs[option] is not None:
                command.extend([f"--{option.replace('_', '-')}", str(kwargs[option])])
        if bool(kwargs.get("skip_mission_activation", False)):
            command.append("--skip-mission-activation")
        if bool(kwargs.get("require_pylon_overview", False)):
            command.append("--require-pylon-overview")
        if bool(kwargs.get("use_default_mission_catalog", False)):
            command.append("--use-default-mission-catalog")
        force_update_overview = bool(kwargs.get("force_update_overview", True))
        if force_update_overview:
            command.append("--force-update-overview")
        else:
            command.append("--reuse-stored-overview")

        log_file = open(log_path, "a", encoding="utf-8")
        env = os.environ.copy()
        env.setdefault("FASTDDS_BUILTIN_TRANSPORTS", "UDPv4")
        env.setdefault("GZ_IP", "127.0.0.1")
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=env,
        )
        log_file.close()
        record = {
            "workflow_id": workflow_id,
            "pid": process.pid,
            "process": process,
            "artifact_dir": str(artifact_dir),
            "status_path": str(status_path),
            "log_path": str(log_path),
            "command": command,
            "started_at": time.time(),
        }
        self._workflow_runs[workflow_id] = record
        data = self._mission_deploy_status_from_record(record)
        data["suggested_next_tools"] = ["workflow.mission_deploy_status", "workflow.cancel_mission_deploy"]
        return ToolResult(True, data, "mission deployment workflow started")

    def mission_deploy_workflow_status(
        self,
        workflow_id: str = "",
        status_path: str = "",
        artifact_dir: str = "",
        tail_log_lines: int = 40,
    ) -> ToolResult:
        record = self._workflow_runs.get(workflow_id) if workflow_id else None
        if record is None and workflow_id:
            candidate = Path("/tmp/iii_drone/mission_deploy") / workflow_id / "status.json"
            if candidate.exists():
                status_path = str(candidate)
        if record is None and not status_path and artifact_dir:
            candidate = Path(artifact_dir) / "status.json"
            if candidate.exists():
                status_path = str(candidate)
        if record is not None:
            data = self._mission_deploy_status_from_record(record)
        elif status_path:
            path = Path(status_path)
            data = self._read_json_path(path) if path.exists() else {"state": "unknown", "error": f"missing status file: {path}"}
            pid = data.get("pid")
            if isinstance(pid, int):
                data["process_running"] = self._pid_running(pid)
        else:
            runs = [self._mission_deploy_status_from_record(value) for value in self._workflow_runs.values()]
            return ToolResult(True, {"workflows": runs}, f"{len(runs)} workflow(s) tracked")
        log_path = data.get("log_path")
        if log_path and int(tail_log_lines) > 0:
            data["log_tail"] = self._tail_file(Path(log_path), int(tail_log_lines))
        success = data.get("state") not in {"failed", "cancelled", "unknown"}
        return ToolResult(bool(success), data, str(data.get("message") or data.get("state") or "workflow status"))

    def cancel_mission_deploy_workflow(self, workflow_id: str = "", status_path: str = "", timeout_sec: float = 5.0) -> ToolResult:
        status = self.mission_deploy_workflow_status(workflow_id=workflow_id, status_path=status_path, tail_log_lines=0).data
        pid = status.get("pid")
        if not isinstance(pid, int):
            return ToolResult(False, status, "workflow pid unavailable")
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            status["process_running"] = False
            return ToolResult(True, status, "workflow process already stopped")
        deadline = time.monotonic() + float(timeout_sec)
        while time.monotonic() < deadline and self._pid_running(pid):
            time.sleep(0.1)
        if self._pid_running(pid):
            os.killpg(pid, signal.SIGKILL)
        final_status = self.mission_deploy_workflow_status(workflow_id=workflow_id, status_path=status_path, tail_log_lines=20).data
        return ToolResult(True, final_status, "workflow cancellation requested")

    def discover_container(
        self,
        local_folder: str = "",
        *,
        workspace_root: str = "",
        timeout_sec: float = 3.0,
    ) -> ToolResult:
        folder = str(local_folder or workspace_root or os.environ.get("III_DRONE_HOST_WORKSPACE", "")).strip()
        data: dict[str, Any] = {
            "requested_local_folder": folder,
            "runtime_workspace_root": str(self._workspace_root),
            "hostname": socket.gethostname(),
            "inside_container": Path("/.dockerenv").exists(),
            "docker_cli": shutil.which("docker"),
        }
        if data["inside_container"]:
            data["current_container_hint"] = {
                "hostname": data["hostname"],
                "note": "MCP server is already running inside a container; host Docker discovery may be unavailable from here.",
            }
        if not data["docker_cli"]:
            return ToolResult(
                bool(data["inside_container"]),
                data,
                "Docker CLI unavailable; reported current container context" if data["inside_container"] else "Docker CLI unavailable",
            )

        command = ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Label \"devcontainer.local_folder\"}}"]
        if folder:
            command[2:2] = ["--filter", f"label=devcontainer.local_folder={folder}"]
        result = self._run_tool_command(command, timeout_sec=float(timeout_sec), check=False)
        data["command"] = (result.data or {}).get("command") if isinstance(result.data, dict) else command
        data["returncode"] = (result.data or {}).get("returncode") if isinstance(result.data, dict) else None
        data["stderr"] = (result.data or {}).get("stderr") if isinstance(result.data, dict) else ""
        containers = []
        stdout = ((result.data or {}).get("stdout") if isinstance(result.data, dict) else "") or ""
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            containers.append(
                {
                    "id": parts[0],
                    "name": parts[1],
                    "devcontainer_local_folder": parts[2] if len(parts) > 2 else "",
                    "exec_prefix": ["docker", "exec", "-u", "iii", parts[0]],
                }
            )
        data["containers"] = containers
        data["selected"] = containers[0] if containers else None
        return ToolResult(bool(containers), data, f"found {len(containers)} matching devcontainer(s)")

    def configuration(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "get_yaml":
            response = self._call_service(self._get_parameter_yaml, GetParameterYaml.Request(), kwargs.get("timeout_sec", 2.0))
            return ToolResult(True, {"yaml": response.yaml})
        if command == "get_declared":
            response = self._call_service(
                self._get_declared_parameters,
                GetDeclaredParameters.Request(),
                kwargs.get("timeout_sec", 2.0),
            )
            return ToolResult(True, {"declared_parameters_yaml": response.declared_parameters_yaml})
        if command == "files":
            response = self._call_service(self._get_parameter_files, GetParameterFiles.Request(), kwargs.get("timeout_sec", 2.0))
            return ToolResult(True, {"parameter_files": list(response.parameter_files)})
        if command == "current_file":
            response = self._call_service(
                self._get_current_parameter_file,
                GetCurrentParameterFile.Request(),
                kwargs.get("timeout_sec", 2.0),
            )
            return ToolResult(
                True,
                {
                    "current_parameter_file": response.current_parameter_file,
                    "default_parameter_file": response.default_parameter_file,
                },
            )
        if command == "save":
            request = SaveParameters.Request()
            request.file = kwargs.get("file", "")
            request.set_as_default = bool(kwargs.get("set_as_default", True))
            request.overwrite = bool(kwargs.get("overwrite", False))
            response = self._call_service(self._save_parameters, request, kwargs.get("timeout_sec", 5.0))
            return ToolResult(bool(response.success), {"file": response.file, "message": response.message})
        if command == "load":
            request = LoadParameters.Request()
            request.file = kwargs["file"]
            request.set_as_default = bool(kwargs.get("set_as_default", False))
            response = self._call_service(self._load_parameters, request, kwargs.get("timeout_sec", 5.0))
            return ToolResult(bool(response.success), {"message": response.message})
        if command == "set":
            request = SetParameterFromGC.Request()
            request.parameter_name = kwargs["parameter_name"]
            request.parameter_string_value = str(kwargs["value"])
            response = self._call_service(self._set_parameter, request, kwargs.get("timeout_sec", 5.0))
            return ToolResult(bool(response.success), {"message": response.message})
        raise ValueError(f"unknown configuration command: {command}")

    def px4(self, command: str, timeout_sec: float = 10.0, **kwargs: Any) -> ToolResult:
        if command == "health":
            return self.px4_health(
                timeout_sec=float(kwargs.get("timeout_sec", 30.0)),
                stable_sec=float(kwargs.get("stable_sec", 0.0)),
            )
        if command == "set_nav_state":
            return self._send_px4_nav_state(
                int(kwargs["nav_state"]),
                target_system=int(kwargs.get("target_system", 1)),
                target_component=int(kwargs.get("target_component", 1)),
                repeat_count=int(kwargs.get("repeat_count", 5)),
                postcondition_timeout_sec=float(kwargs.get("postcondition_timeout_sec", 10.0)),
                stable_sec=float(kwargs.get("stable_sec", 1.0)),
            )
        if command == "set_mode":
            return self._send_px4_standard_mode(
                kwargs["mode"],
                target_system=int(kwargs.get("target_system", 1)),
                target_component=int(kwargs.get("target_component", 1)),
                repeat_count=int(kwargs.get("repeat_count", 5)),
            )
        if command == "arm_direct":
            return self._send_px4_arm_disarm(
                arm=True,
                force=bool(kwargs.get("force", False)),
                target_system=int(kwargs.get("target_system", 1)),
                target_component=int(kwargs.get("target_component", 1)),
                repeat_count=int(kwargs.get("repeat_count", 5)),
                postcondition_timeout_sec=float(kwargs.get("postcondition_timeout_sec", timeout_sec)),
            )
        if command == "disarm_direct":
            return self._send_px4_arm_disarm(
                arm=False,
                force=bool(kwargs.get("force", False)),
                target_system=int(kwargs.get("target_system", 1)),
                target_component=int(kwargs.get("target_component", 1)),
                repeat_count=int(kwargs.get("repeat_count", 5)),
                postcondition_timeout_sec=float(kwargs.get("postcondition_timeout_sec", timeout_sec)),
            )
        if command == "get_param":
            async def run_get_param() -> ToolResult:
                client = Px4CommandClient(self._px4_system_address)
                try:
                    await client.connect()
                    data = await client.get_param_mavsdk(str(kwargs["param_name"]))
                    return ToolResult(True, data)
                finally:
                    await client.close_async()
            return asyncio.run(asyncio.wait_for(run_get_param(), timeout=timeout_sec))
        if command == "set_param":
            async def run_set_param() -> ToolResult:
                client = Px4CommandClient(self._px4_system_address)
                try:
                    await client.connect()
                    data = await client.set_param_mavsdk(
                        str(kwargs["param_name"]),
                        float(kwargs["param_value"]),
                        param_type=int(kwargs["param_type"]) if kwargs.get("param_type") is not None else None,
                    )
                    return ToolResult(True, data)
                finally:
                    await client.close_async()
            return asyncio.run(asyncio.wait_for(run_set_param(), timeout=timeout_sec))

        async def run() -> ToolResult:
            client = Px4CommandClient(self._px4_system_address)
            try:
                await client.connect()
                postcondition_timeout = float(kwargs.get("postcondition_timeout_sec", max(5.0, timeout_sec)))
                if command == "arm":
                    await self._px4_arm_when_health_stable(
                        client,
                        timeout_sec=timeout_sec,
                        health_stable_sec=float(kwargs.get("health_stable_sec", 2.0)),
                    )
                    telemetry = await client.wait_until(lambda item: item.armed is True, timeout_sec=postcondition_timeout)
                    return ToolResult(True, asdict(telemetry), "armed")
                if command == "takeoff":
                    await client.takeoff()
                    telemetry = await client.wait_until(lambda item: item.in_air is True, timeout_sec=postcondition_timeout)
                    data = asdict(telemetry)
                    min_altitude_m = float(kwargs.get("min_altitude_m", 0.0))
                    if min_altitude_m > 0.0:
                        data["altitude"] = self._wait_px4_local_altitude(
                            min_altitude_m,
                            timeout_sec=postcondition_timeout,
                        )
                    return ToolResult(True, data, "takeoff complete")
                if command == "disarm":
                    await client.disarm()
                    telemetry = await client.wait_until(lambda item: item.armed is False, timeout_sec=postcondition_timeout)
                    return ToolResult(True, asdict(telemetry), "disarmed")
                if command == "land":
                    await client.land()
                    telemetry = await client.wait_until(lambda item: item.in_air is False, timeout_sec=postcondition_timeout)
                    return ToolResult(True, asdict(telemetry), "landing complete")
                if command == "hold":
                    await client.hold()
                    telemetry = await client.telemetry_snapshot()
                    return ToolResult(True, asdict(telemetry), "hold requested")
                if command == "return_to_launch":
                    await client.return_to_launch()
                    telemetry = await client.telemetry_snapshot()
                    return ToolResult(True, asdict(telemetry), "RTL requested")
                if command == "status":
                    telemetry = await client.telemetry_snapshot()
                    return ToolResult(True, asdict(telemetry))
                raise ValueError(f"unknown PX4 command: {command}")
            finally:
                await client.close_async()

        return asyncio.run(asyncio.wait_for(run(), timeout=timeout_sec))

    def battery_reset(
        self,
        remaining_pct: float = 100.0,
        timeout_sec: float = 10.0,
        tolerance_pct: float = 1.0,
    ) -> ToolResult:
        try:
            from px4_msgs.msg import BatteryStatus
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for simulated battery reset verification") from exc

        target_pct = float(remaining_pct)
        tolerance_pct = max(0.1, float(tolerance_pct))
        if not 0.0 <= target_pct <= 100.0:
            raise ValueError(f"remaining_pct must be within [0, 100], got {target_pct}")

        before = self._take_message(
            "/fmu/out/battery_status", BatteryStatus, min(2.0, timeout_sec), required=False
        )
        floor_result = self.px4("get_param", param_name="SIM_BAT_MIN_PCT", timeout_sec=timeout_sec)
        floor_pct = float(floor_result.data["param_value"])
        if target_pct + tolerance_pct < floor_pct:
            raise ValueError(
                f"remaining_pct={target_pct} is below SIM_BAT_MIN_PCT={floor_pct}; "
                "lower the simulation floor first"
            )

        initial_result = self.px4(
            "set_param",
            param_name="SIM_BAT_INIT_PCT",
            param_value=target_pct,
            param_type=9,
            timeout_sec=timeout_sec,
        )
        token_result = self.px4("get_param", param_name="SIM_BAT_RESET", timeout_sec=timeout_sec)
        previous_token = int(round(float(token_result.data["param_value"])))
        previous_ack_result = self.px4(
            "get_param", param_name="SIM_BAT_RST_ACK", timeout_sec=timeout_sec
        )
        previous_ack_token = int(round(float(previous_ack_result.data["param_value"])))
        next_token = max(previous_token, previous_ack_token) + 1
        if next_token > 2_147_483_647:
            next_token = 0
        reset_result = self.px4(
            "set_param",
            param_name="SIM_BAT_RESET",
            param_value=next_token,
            param_type=6,
            timeout_sec=timeout_sec,
        )

        acknowledgement = None
        acknowledgement_token = None
        acknowledgement_deadline = time.monotonic() + timeout_sec
        while time.monotonic() < acknowledgement_deadline:
            acknowledgement = self.px4(
                "get_param",
                param_name="SIM_BAT_RST_ACK",
                timeout_sec=max(0.1, acknowledgement_deadline - time.monotonic()),
            )
            acknowledgement_token = int(round(float(acknowledgement.data["param_value"])))
            if acknowledgement_token == next_token:
                break
            time.sleep(min(0.1, max(0.0, acknowledgement_deadline - time.monotonic())))

        deadline = time.monotonic() + timeout_sec
        after = None
        observed_pct = None
        while acknowledgement_token == next_token and time.monotonic() < deadline and rclpy.ok():
            after = self._take_message(
                "/fmu/out/battery_status",
                BatteryStatus,
                min(0.5, max(0.1, deadline - time.monotonic())),
                required=False,
            )
            if after is not None and math.isfinite(float(after.remaining)):
                observed_pct = float(after.remaining) * 100.0
                if abs(observed_pct - target_pct) <= tolerance_pct:
                    break

        # The reset token is a transient command/acknowledgement handshake, but
        # both values are exposed through the complete PX4 parameter inventory.
        # Restore the release baseline before returning so a successful HIL
        # fixture cannot make the next deployment audit report false drift.
        cleanup_result = None
        cleanup_acknowledgement = None
        if next_token != 0:
            cleanup_result = self.px4(
                "set_param",
                param_name="SIM_BAT_RESET",
                param_value=0,
                param_type=6,
                timeout_sec=timeout_sec,
            )
            cleanup_deadline = time.monotonic() + timeout_sec
            while time.monotonic() < cleanup_deadline:
                cleanup_acknowledgement = self.px4(
                    "get_param",
                    param_name="SIM_BAT_RST_ACK",
                    timeout_sec=max(0.1, cleanup_deadline - time.monotonic()),
                )
                if int(round(float(cleanup_acknowledgement.data["param_value"]))) == 0:
                    break
                time.sleep(min(0.1, max(0.0, cleanup_deadline - time.monotonic())))

        cleanup_ack_token = (
            0
            if next_token == 0
            else None
            if cleanup_acknowledgement is None
            else int(round(float(cleanup_acknowledgement.data["param_value"])))
        )

        data = {
            "target_remaining_pct": target_pct,
            "tolerance_pct": tolerance_pct,
            "minimum_remaining_pct": floor_pct,
            "previous_reset_token": previous_token,
            "previous_acknowledgement_token": previous_ack_token,
            "reset_token": next_token,
            "acknowledgement_token": acknowledgement_token,
            "acknowledgement_parameter": None if acknowledgement is None else acknowledgement.data,
            "initial_percentage_parameter": initial_result.data,
            "reset_parameter": reset_result.data,
            "cleanup_reset_parameter": None if cleanup_result is None else cleanup_result.data,
            "cleanup_acknowledgement_token": cleanup_ack_token,
            "battery_before": None if before is None else self._message_to_nested_dict(before),
            "battery_after": None if after is None else self._message_to_nested_dict(after),
            "observed_remaining_pct": observed_pct,
            "observed_within_tolerance": (
                observed_pct is not None and abs(observed_pct - target_pct) <= tolerance_pct
            ),
        }
        if acknowledgement_token != next_token:
            return ToolResult(False, data, "battery simulator did not acknowledge the reset request")
        if cleanup_ack_token != 0:
            return ToolResult(False, data, "battery simulator reset handshake did not return to baseline")
        if observed_pct is None:
            return ToolResult(False, data, "battery reset was acknowledged but no battery status was observed")
        return ToolResult(
            True,
            data,
            f"simulated battery reset applied at {target_pct:.1f}%; current status {observed_pct:.1f}%",
        )

    async def _px4_arm_when_health_stable(
        self,
        client: Px4CommandClient,
        *,
        timeout_sec: float,
        health_stable_sec: float,
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            health = self.px4_health(
                timeout_sec=min(max(health_stable_sec + 1.0, 2.0), remaining),
                stable_sec=health_stable_sec,
            )
            if health.success:
                try:
                    await client.arm()
                    return
                except Exception as exc:
                    last_error = exc
            await asyncio.sleep(0.5)
        if last_error is not None:
            raise RuntimeError(f"PX4 arm was denied before timeout; last_error={last_error}")
        raise TimeoutError(f"PX4 health did not remain stable for {health_stable_sec}s before arming")

    def px4_health(self, timeout_sec: float = 30.0, stable_sec: float = 0.0) -> ToolResult:
        try:
            from px4_msgs.msg import FailsafeFlags, VehicleCommandAck, VehicleLandDetected, VehicleStatus
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 health inspection") from exc

        deadline = time.monotonic() + timeout_sec
        data: dict[str, Any] = {}
        success = False
        stable_since: float | None = None
        latest: dict[str, Any] = {
            "status": None,
            "land_detected": None,
            "failsafe_flags": None,
            "command_ack": None,
        }

        subscriptions = [
            self.node.create_subscription(
                VehicleStatus,
                "/fmu/out/vehicle_status_v1",
                lambda message: latest.__setitem__("status", message),
                qos_profile_sensor_data,
            ),
            self.node.create_subscription(
                VehicleLandDetected,
                "/fmu/out/vehicle_land_detected",
                lambda message: latest.__setitem__("land_detected", message),
                qos_profile_sensor_data,
            ),
            self.node.create_subscription(
                FailsafeFlags,
                "/fmu/out/failsafe_flags",
                lambda message: latest.__setitem__("failsafe_flags", message),
                qos_profile_sensor_data,
            ),
            self.node.create_subscription(
                VehicleCommandAck,
                "/fmu/out/vehicle_command_ack",
                lambda message: latest.__setitem__("command_ack", message),
                qos_profile_sensor_data,
            ),
        ]
        try:
            while time.monotonic() < deadline and rclpy.ok():
                rclpy.spin_once(self.node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
                status = latest["status"]
                land_detected = latest["land_detected"]
                failsafe_flags = latest["failsafe_flags"]
                command_ack = latest["command_ack"]

                data = {
                    "pre_flight_checks_pass": bool(status.pre_flight_checks_pass) if status else None,
                    "arming_state": int(status.arming_state) if status else None,
                    "latest_arming_reason": int(status.latest_arming_reason) if status else None,
                    "latest_disarming_reason": int(status.latest_disarming_reason) if status else None,
                    "nav_state": int(status.nav_state) if status else None,
                    "nav_state_user_intention": int(status.nav_state_user_intention) if status else None,
                    "failsafe": bool(status.failsafe) if status else None,
                    "landed": bool(land_detected.landed) if land_detected else None,
                    "ground_contact": bool(land_detected.ground_contact) if land_detected else None,
                    "in_freefall": bool(land_detected.freefall) if land_detected else None,
                    "failsafe_flags": self._message_to_plain_dict(failsafe_flags),
                    "last_command_ack": self._message_to_plain_dict(command_ack),
                }
                sample_ok = bool(data["pre_flight_checks_pass"]) and not bool(data["failsafe"])
                now = time.monotonic()
                if sample_ok:
                    if stable_since is None:
                        stable_since = now
                    success = (now - stable_since) >= max(0.0, stable_sec)
                else:
                    stable_since = None
                    success = False
                data["stable_sec_required"] = stable_sec
                data["stable_sec_observed"] = (now - stable_since) if stable_since is not None else 0.0
                if success:
                    break
        finally:
            for subscription in subscriptions:
                self.node.destroy_subscription(subscription)
        return ToolResult(success, data, "PX4 health ok" if success else "PX4 health not ready")

    def px4_safety(self, timeout_sec: float = 2.0) -> ToolResult:
        def exception_message(exc: Exception) -> str:
            message = str(exc).strip()
            return message if message else exc.__class__.__name__

        data: dict[str, Any] = {
            "mavsdk": {"available": False, "error": None, "status": None},
            "ros": {"available": False, "error": None, "vehicle_status": None, "land_detected": None, "failsafe_flags": None},
            "derived": {
                "armed": None,
                "in_air": None,
                "flight_mode": None,
                "nav_state": None,
                "failsafe": None,
                "unexpected_recovery": None,
                "degraded": False,
                "degraded_sources": [],
                "verdict_source": None,
                "source_sufficient": False,
            },
        }

        try:
            status = self.px4("status", timeout_sec=timeout_sec)
            data["mavsdk"] = {
                "available": bool(status.success),
                "error": None if status.success else status.message,
                "status": status.data if status.success else None,
            }
            if isinstance(status.data, dict):
                data["derived"]["armed"] = status.data.get("armed")
                data["derived"]["in_air"] = status.data.get("in_air")
                data["derived"]["flight_mode"] = status.data.get("flight_mode")
        except Exception as exc:
            data["mavsdk"]["error"] = exception_message(exc)

        try:
            from px4_msgs.msg import FailsafeFlags, VehicleLandDetected, VehicleStatus

            sample_timeout = max(0.1, min(timeout_sec, 1.0))
            vehicle_status = self._take_message("/fmu/out/vehicle_status_v1", VehicleStatus, sample_timeout, required=False)
            land_detected = self._take_message("/fmu/out/vehicle_land_detected", VehicleLandDetected, sample_timeout, required=False)
            failsafe_flags = self._take_message("/fmu/out/failsafe_flags", FailsafeFlags, sample_timeout, required=False)

            data["ros"] = {
                "available": any(item is not None for item in (vehicle_status, land_detected, failsafe_flags)),
                "error": None,
                "vehicle_status": self._message_to_plain_dict(vehicle_status),
                "land_detected": self._message_to_plain_dict(land_detected),
                "failsafe_flags": self._message_to_plain_dict(failsafe_flags),
            }

            if vehicle_status is not None:
                data["derived"]["nav_state"] = int(vehicle_status.nav_state)
                data["derived"]["failsafe"] = bool(vehicle_status.failsafe)
            if land_detected is not None and data["derived"]["in_air"] is None:
                data["derived"]["in_air"] = not bool(land_detected.landed)
        except Exception as exc:
            data["ros"]["error"] = exception_message(exc)

        flight_mode = str(data["derived"].get("flight_mode") or "").upper()
        nav_state = data["derived"].get("nav_state")
        data["derived"]["unexpected_recovery"] = bool(
            flight_mode in {"RETURN_TO_LAUNCH", "RTL"}
            or nav_state in {5, 20}
            or data["derived"].get("failsafe") is True
        )

        degraded_sources = []
        if not data["mavsdk"]["available"]:
            degraded_sources.append("mavsdk")
        if not data["ros"]["available"]:
            degraded_sources.append("ros")
        data["derived"]["degraded_sources"] = degraded_sources

        if data["ros"]["available"] and data["derived"].get("failsafe") is not None:
            data["derived"]["verdict_source"] = "ros"
            data["derived"]["source_sufficient"] = True
        elif data["mavsdk"]["available"]:
            data["derived"]["verdict_source"] = "mavsdk"
            data["derived"]["source_sufficient"] = True

        data["derived"]["degraded"] = bool(not data["derived"]["source_sufficient"])

        success = not bool(data["derived"]["failsafe"]) and not bool(data["derived"]["unexpected_recovery"])
        if data["derived"]["degraded"]:
            return ToolResult(False, data, "PX4 safety state degraded")
        return ToolResult(success, data, "PX4 safety ok" if success else "PX4 safety violation")

    def px4_ulog_events(
        self,
        ulog_path: str | None = None,
        filename: str = "px4_ulog_events.json",
        max_events: int = 200,
    ) -> ToolResult:
        if ulog_path:
            path = Path(ulog_path)
        else:
            log_root = self._workspace_root / "PX4-Autopilot" / "build" / "px4_sitl_default" / "rootfs" / "log"
            candidates = sorted(log_root.glob("**/*.ulg"), key=lambda item: item.stat().st_mtime)
            if not candidates:
                return ToolResult(False, {"log_root": str(log_root), "events": []}, "no PX4 ULog files found")
            path = candidates[-1]

        if not path.exists():
            return ToolResult(False, {"ulog_path": str(path), "events": []}, "PX4 ULog file does not exist")

        events = []
        seen: set[str] = set()
        extraction_method = "pyulog"
        parsed_events: list[dict[str, Any]] = []
        unique_parsed_events: list[dict[str, Any]] = []
        try:
            parsed_events = self._extract_px4_ulog_logged_messages(path, max_events=max_events)
        except Exception as exc:
            extraction_method = f"strings_fallback_after_pyulog_{type(exc).__name__}"
            parsed_events = self._extract_px4_ulog_strings(path, max_events=max_events)

        for event in parsed_events:
            line = str(event.get("line", ""))
            if line in seen:
                continue
            seen.add(line)
            events.append(line)
            unique_parsed_events.append(event)

        classified_events = [
            self._classify_px4_ulog_event(
                str(event.get("line", "")),
                px4_severity=event.get("px4_severity"),
                timestamp_s=event.get("timestamp_s"),
                log_level=event.get("log_level"),
            )
            for event in unique_parsed_events
        ]
        severity_rank = {"info": 0, "warning": 1, "critical": 2}
        max_severity = "info"
        for event in classified_events:
            if severity_rank[event["severity"]] > severity_rank[max_severity]:
                max_severity = event["severity"]

        data = {
            "ulog_path": str(path),
            "extraction_method": extraction_method,
            "event_count": len(events),
            "events": events,
            "classified_events": classified_events,
            "critical_event_count": sum(1 for event in classified_events if event["severity"] == "critical"),
            "warning_event_count": sum(1 for event in classified_events if event["severity"] == "warning"),
            "max_severity": max_severity,
        }
        artifact_path = self._write_artifact(filename, json.dumps(data, indent=2))
        data["artifact_path"] = str(artifact_path)
        return ToolResult(True, data, f"extracted {len(events)} PX4 ULog event line(s)")

    @staticmethod
    def _px4_log_level_to_severity(log_level: int | None) -> str:
        if log_level is None:
            return "info"
        if log_level <= 51:
            return "critical"
        if log_level <= 52:
            return "warning"
        return "info"

    def _extract_px4_ulog_logged_messages(self, path: Path, *, max_events: int) -> list[dict[str, Any]]:
        from pyulog import ULog

        keywords = self._px4_ulog_event_keywords()
        ulog = ULog(str(path))
        events: list[dict[str, Any]] = []
        for message in ulog.logged_messages:
            line = self._clean_px4_ulog_line(str(message.message))
            if not line or not keywords.search(line):
                continue
            log_level = int(message.log_level)
            events.append(
                {
                    "line": line,
                    "timestamp_s": float(message.timestamp) / 1_000_000.0,
                    "log_level": log_level,
                    "px4_severity": self._px4_log_level_to_severity(log_level),
                }
            )
            if len(events) >= max_events:
                break
        return events

    def _extract_px4_ulog_strings(self, path: Path, *, max_events: int) -> list[dict[str, Any]]:
        keywords = self._px4_ulog_event_keywords()
        try:
            completed = subprocess.run(
                ["strings", str(path)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10.0,
                check=False,
            )
            raw_lines = completed.stdout.splitlines()
        except Exception:
            raw_lines = path.read_bytes().decode("latin1", errors="ignore").splitlines()

        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in raw_lines:
            cleaned = self._clean_px4_ulog_line(line)
            if not cleaned or not keywords.search(cleaned):
                continue
            if not self._looks_like_px4_event_line(cleaned):
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            events.append({"line": cleaned, "px4_severity": None})
            if len(events) >= max_events:
                break
        return events

    @staticmethod
    def _px4_ulog_event_keywords() -> re.Pattern[str]:
        return re.compile(
            r"(failsafe|invalid setpoints|rtl|return|unresponsive|no response|armed|disarmed|arming|land|takeoff|offboard|external|mode|time jump|time sync)",
            re.IGNORECASE,
        )

    @staticmethod
    def _clean_px4_ulog_line(line: str) -> str:
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        return ansi_escape.sub("", line).strip().rstrip("\x00")

    @staticmethod
    def _looks_like_px4_event_line(line: str) -> bool:
        if line.startswith(("F", "int32_t ", "float ", "char[", "!char[")):
            return False
        if "[" in line and "]" in line:
            return True
        return bool(re.search(r"\b(Armed|Disarmed|Failsafe|RTL|land|takeoff|unresponsive|No response)\b", line, re.IGNORECASE))

    @staticmethod
    def _classify_px4_ulog_event(
        line: str,
        *,
        px4_severity: Any = None,
        timestamp_s: Any = None,
        log_level: Any = None,
    ) -> dict[str, Any]:
        normalized = line.lower()
        severity = str(px4_severity) if px4_severity in {"info", "warning", "critical"} else "info"
        category = "event"

        startup_or_health_check = any(
            marker in normalized
            for marker in (
                "preflight fail",
                "startup script",
                "ready for takeoff",
                "successfully created",
                "arming warning",
            )
        )
        if any(marker in normalized for marker in ("invalid setpoints",)):
            severity = "critical"
            category = "setpoint"
        elif any(marker in normalized for marker in ("failsafe", "safe recovery", "unresponsive", "no response")):
            severity = "critical"
            category = "failsafe"
        elif any(marker in normalized for marker in ("rtl", "return to launch", "return mode")):
            severity = "critical"
            category = "recovery"
        elif any(marker in normalized for marker in ("time sync converged",)):
            severity = "info"
            category = "timing"
        elif any(marker in normalized for marker in ("time jump", "time sync no longer", "time sync")):
            severity = "warning"
            category = "timing"
        elif startup_or_health_check:
            if "preflight fail" in normalized or "arming warning" in normalized:
                severity = "warning"
            else:
                severity = "info"
            category = "preflight" if "preflight" in normalized or "arming" in normalized else "startup"
        elif any(marker in normalized for marker in ("landing detected", "disarmed by landing", "landing at current position")):
            severity = "info"
            category = "landing"
        elif any(marker in normalized for marker in ("armed", "disarmed", "takeoff")):
            severity = "info"
            category = "flight_state"
        elif any(marker in normalized for marker in ("mode", "offboard", "external")):
            severity = "info"
            category = "mode"

        classified = {"message": line, "line": line, "severity": severity, "category": category}
        if px4_severity in {"info", "warning", "critical"}:
            classified["px4_severity"] = px4_severity
        if timestamp_s is not None:
            classified["timestamp_s"] = timestamp_s
        if log_level is not None:
            classified["log_level"] = log_level
        return classified

    def _send_px4_nav_state(
        self,
        nav_state: int,
        *,
        target_system: int,
        target_component: int,
        repeat_count: int,
        postcondition_timeout_sec: float,
        stable_sec: float,
    ) -> ToolResult:
        if 23 <= int(nav_state) <= 30:
            return self._send_px4_external_nav_state(
                int(nav_state),
                target_system=target_system,
                target_component=target_component,
                repeat_count=repeat_count,
                postcondition_timeout_sec=postcondition_timeout_sec,
                stable_sec=stable_sec,
            )

        status = self._publish_px4_nav_state_command(
            int(nav_state),
            target_system=target_system,
            target_component=target_component,
            repeat_count=repeat_count,
            postcondition_timeout_sec=postcondition_timeout_sec,
            stable_sec=stable_sec,
        )
        return ToolResult(True, status, "nav-state command accepted")

    def _publish_px4_nav_state_command(
        self,
        nav_state: int,
        *,
        target_system: int,
        target_component: int,
        repeat_count: int,
        postcondition_timeout_sec: float,
        stable_sec: float,
    ) -> dict[str, Any]:
        try:
            from px4_msgs.msg import VehicleCommand
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 nav-state commands") from exc

        publisher = self.node.create_publisher(VehicleCommand, "/fmu/in/vehicle_command", self._px4_input_qos())
        try:
            deadline = time.monotonic() + 2.0
            while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=0.1)

            for _ in range(max(1, repeat_count)):
                message = VehicleCommand()
                message.timestamp = int(self.node.get_clock().now().nanoseconds / 1000)
                message.command = VehicleCommand.VEHICLE_CMD_SET_NAV_STATE
                message.param1 = float(nav_state)
                message.target_system = target_system
                message.target_component = target_component
                message.source_system = 255
                message.source_component = 0
                message.from_external = True
                publisher.publish(message)
                rclpy.spin_once(self.node, timeout_sec=0.05)
        finally:
            self.node.destroy_publisher(publisher)

        status = self._wait_px4_nav_state(
            nav_state,
            timeout_sec=postcondition_timeout_sec,
            stable_sec=stable_sec,
        )
        status["command_method"] = "ros_vehicle_command_set_nav_state"
        return status

    def _send_px4_external_nav_state(
        self,
        nav_state: int,
        *,
        target_system: int,
        target_component: int,
        repeat_count: int,
        postcondition_timeout_sec: float,
        stable_sec: float,
    ) -> ToolResult:
        try:
            status = self._publish_px4_nav_state_command(
                nav_state,
                target_system=target_system,
                target_component=target_component,
                repeat_count=repeat_count,
                postcondition_timeout_sec=postcondition_timeout_sec,
                stable_sec=stable_sec,
            )
            status["external_mode_command_method"] = "ros_vehicle_command_set_nav_state"
            return ToolResult(True, status, "PX4 external mode command accepted")
        except TimeoutError as ros_exc:
            ros_error = str(ros_exc)

        client = Px4CommandClient(self._px4_system_address)
        command_data = client.set_external_nav_state_mavlink(
            nav_state,
            target_system=target_system,
            target_component=target_component,
            timeout_sec=max(5.0, min(20.0, postcondition_timeout_sec)),
        )
        try:
            status = self._wait_px4_nav_state(
                nav_state,
                timeout_sec=postcondition_timeout_sec,
                stable_sec=stable_sec,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"{exc}; ros_vehicle_command_error={ros_error}; external mode command data={command_data}"
            ) from exc
        data = dict(command_data)
        data.update(status)
        data["external_mode_command_method"] = "mavlink_do_set_mode"
        data["ros_vehicle_command_error"] = ros_error
        return ToolResult(True, data, "PX4 external mode command accepted")

    @staticmethod
    def _px4_input_qos() -> QoSProfile:
        return QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )

    def _wait_px4_nav_state(self, nav_state: int, *, timeout_sec: float, stable_sec: float) -> dict[str, Any]:
        try:
            from px4_msgs.msg import VehicleStatus
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 nav-state inspection") from exc

        deadline = time.monotonic() + timeout_sec
        stable_since: float | None = None
        last_status: Any = None
        while time.monotonic() < deadline and rclpy.ok():
            status = self._take_message(
                "/fmu/out/vehicle_status_v1",
                VehicleStatus,
                min(0.5, max(0.1, deadline - time.monotonic())),
                required=False,
            )
            if status is not None:
                last_status = status
                now = time.monotonic()
                if int(status.nav_state) == nav_state:
                    if stable_since is None:
                        stable_since = now
                    if now - stable_since >= max(0.0, stable_sec):
                        return {
                            "nav_state": int(status.nav_state),
                            "nav_state_user_intention": int(status.nav_state_user_intention),
                            "stable_sec_required": stable_sec,
                            "stable_sec_observed": now - stable_since,
                        }
                else:
                    stable_since = None
            time.sleep(0.1)
        if last_status is None:
            raise TimeoutError(f"timed out waiting for PX4 nav_state={nav_state}; no VehicleStatus received")
        raise TimeoutError(
            "timed out waiting for PX4 nav_state="
            f"{nav_state}; last nav_state={int(last_status.nav_state)}, "
            f"user_intention={int(last_status.nav_state_user_intention)}"
        )

    def _send_px4_arm_disarm(
        self,
        *,
        arm: bool,
        force: bool,
        target_system: int,
        target_component: int,
        repeat_count: int,
        postcondition_timeout_sec: float,
    ) -> ToolResult:
        try:
            from px4_msgs.msg import VehicleCommand, VehicleStatus
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 arm/disarm commands") from exc

        publisher = self.node.create_publisher(VehicleCommand, "/fmu/in/vehicle_command", self._px4_input_qos())
        try:
            deadline = time.monotonic() + 2.0
            while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=0.1)

            for _ in range(max(1, repeat_count)):
                message = VehicleCommand()
                message.timestamp = int(self.node.get_clock().now().nanoseconds / 1000)
                message.command = VehicleCommand.VEHICLE_CMD_COMPONENT_ARM_DISARM
                message.param1 = 1.0 if arm else 0.0
                message.param2 = 21196.0 if force else 0.0
                message.target_system = target_system
                message.target_component = target_component
                message.source_system = 255
                message.source_component = 0
                message.from_external = True
                publisher.publish(message)
                rclpy.spin_once(self.node, timeout_sec=0.05)
        finally:
            self.node.destroy_publisher(publisher)

        target_state = VehicleStatus.ARMING_STATE_ARMED if arm else VehicleStatus.ARMING_STATE_DISARMED
        deadline = time.monotonic() + postcondition_timeout_sec
        last_status: Any = None
        while time.monotonic() < deadline and rclpy.ok():
            status = self._take_message(
                "/fmu/out/vehicle_status_v1",
                VehicleStatus,
                min(0.5, max(0.1, deadline - time.monotonic())),
                required=False,
            )
            if status is not None:
                last_status = status
                if int(status.arming_state) == int(target_state):
                    return ToolResult(
                        True,
                        {
                            "armed": arm,
                            "arming_state": int(status.arming_state),
                            "nav_state": int(status.nav_state),
                            "nav_state_user_intention": int(status.nav_state_user_intention),
                        },
                        "armed" if arm else "disarmed",
                    )
            time.sleep(0.1)
        if last_status is None:
            raise TimeoutError("timed out waiting for PX4 arming state; no VehicleStatus received")
        raise TimeoutError(
            "timed out waiting for PX4 arming_state="
            f"{int(target_state)}; last arming_state={int(last_status.arming_state)}, "
            f"nav_state={int(last_status.nav_state)}"
        )

    def _wait_px4_local_altitude(self, min_altitude_m: float, *, timeout_sec: float) -> dict[str, Any]:
        try:
            from px4_msgs.msg import VehicleOdometry
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 altitude inspection") from exc

        deadline = time.monotonic() + timeout_sec
        last_altitude: float | None = None
        while time.monotonic() < deadline and rclpy.ok():
            odometry = self._take_message(
                "/fmu/out/vehicle_odometry",
                VehicleOdometry,
                min(0.5, max(0.1, deadline - time.monotonic())),
                required=False,
            )
            if odometry is not None:
                last_altitude = -float(odometry.position[2])
                if last_altitude >= min_altitude_m:
                    return {"local_altitude_m": last_altitude, "min_altitude_m": min_altitude_m}
            time.sleep(0.1)
        raise TimeoutError(
            f"timed out waiting for local altitude >= {min_altitude_m}m; "
            f"last_altitude_m={last_altitude}"
        )

    def _send_px4_standard_mode(
        self,
        mode: str,
        *,
        target_system: int,
        target_component: int,
        repeat_count: int,
    ) -> ToolResult:
        try:
            from px4_msgs.msg import VehicleCommand
        except ImportError as exc:
            raise RuntimeError("px4_msgs is required for PX4 mode commands") from exc

        mode_map = {
            "manual": 1,
            "altitude": 2,
            "altctl": 2,
            "position": 3,
            "posctl": 3,
            "mission": 4,
            "acro": 5,
            "offboard": 6,
            "stabilized": 7,
            "rattitude": 8,
        }
        mode_key = mode.strip().lower()
        if mode_key not in mode_map:
            raise ValueError(f"unsupported PX4 mode: {mode}")

        publisher = self.node.create_publisher(VehicleCommand, "/fmu/in/vehicle_command", self._px4_input_qos())
        try:
            deadline = time.monotonic() + 2.0
            while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=0.1)

            for _ in range(max(1, repeat_count)):
                message = VehicleCommand()
                message.timestamp = int(self.node.get_clock().now().nanoseconds / 1000)
                message.command = VehicleCommand.VEHICLE_CMD_DO_SET_MODE
                message.param1 = 1.0
                message.param2 = float(mode_map[mode_key])
                message.target_system = target_system
                message.target_component = target_component
                message.source_system = 1
                message.source_component = 1
                message.from_external = True
                publisher.publish(message)
                rclpy.spin_once(self.node, timeout_sec=0.05)
        finally:
            self.node.destroy_publisher(publisher)

        return ToolResult(True, {"mode": mode_key}, "mode command published")

    def system(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "boot":
            timeout_sec = float(kwargs.get("timeout_sec", 120.0))
            result = self._run_tool_command(
                self._iii_command("system", "boot"),
                timeout_sec=timeout_sec,
                daemon_timeout_sec=timeout_sec,
            )
            if result.success:
                daemon_ready = self._wait_for_system_daemon_ready(timeout_sec=max(5.0, timeout_sec))
                if not daemon_ready.success:
                    return daemon_ready
            return result
        if command == "start":
            if kwargs.get("entity_id"):
                return self._system_start_until_ready(
                    timeout_sec=float(kwargs.get("timeout_sec", 180.0)),
                    entity_id=str(kwargs["entity_id"]),
                    include_dependencies=bool(kwargs.get("include_dependencies", True)),
                )
            return self._system_start_until_ready(timeout_sec=float(kwargs.get("timeout_sec", 120.0)))
        if command == "stop":
            args = self._iii_command("system", "stop")
            if kwargs.get("entity_id"):
                args.extend(["--select-nodes", str(kwargs["entity_id"])])
                if kwargs.get("include_dependencies", True):
                    args.append("--include-dependencies")
            timeout_sec = float(kwargs.get("timeout_sec", 60.0))
            return self._run_tool_command(args, timeout_sec=timeout_sec, daemon_timeout_sec=timeout_sec)
        if command == "restart":
            args = self._iii_command("system", "restart")
            if kwargs.get("cold", True):
                args.append("--cold")
            if kwargs.get("entity_id"):
                args.extend(["--select-nodes", str(kwargs["entity_id"])])
                if kwargs.get("include_dependencies", True):
                    args.append("--include-dependencies")
            timeout_sec = float(kwargs.get("timeout_sec", 180.0))
            return self._run_tool_command(args, timeout_sec=timeout_sec, daemon_timeout_sec=timeout_sec)
        if command == "shutdown":
            args = self._iii_command("system", "shutdown")
            if kwargs.get("keep_session", False):
                args.append("--keep-session")
            timeout_sec = float(kwargs.get("timeout_sec", 90.0))
            return self._run_tool_command(args, timeout_sec=timeout_sec, daemon_timeout_sec=timeout_sec)
        if command == "daemon_restart":
            return self._run_tool_command(self._iii_command("system", "daemon", "restart"), timeout_sec=kwargs.get("timeout_sec", 30.0))
        if command == "status":
            return self._run_tool_command(self._iii_command("system", "status"), timeout_sec=kwargs.get("timeout_sec", 10.0))
        if command == "service_list":
            return self._run_tool_command(self._iii_command("system", "service", "list"), timeout_sec=kwargs.get("timeout_sec", 10.0))
        if command == "service_restart":
            timeout_sec = float(kwargs.get("timeout_sec", 60.0))
            return self._run_tool_command(
                self._iii_command("system", "service", "restart", str(kwargs["entity_id"])),
                timeout_sec=timeout_sec,
                daemon_timeout_sec=timeout_sec,
            )
        raise ValueError(f"unknown system command: {command}")

    def _wait_for_system_daemon_ready(self, timeout_sec: float) -> ToolResult:
        deadline = time.monotonic() + timeout_sec
        last_result: ToolResult | None = None
        while time.monotonic() < deadline:
            per_attempt_timeout = min(5.0, max(1.0, deadline - time.monotonic()))
            last_result = self._run_tool_command(
                self._iii_command("system", "status"),
                timeout_sec=per_attempt_timeout,
                daemon_timeout_sec=per_attempt_timeout,
                check=False,
            )
            stdout = str(last_result.data.get("stdout", ""))
            stderr = str(last_result.data.get("stderr", ""))
            if int(last_result.data.get("returncode", 1)) == 0:
                return ToolResult(True, dict(last_result.data), "system daemon ready")
            if int(last_result.data.get("returncode", 1)) == -1:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            if "Resource temporarily unavailable" in stderr or "BlockingIOError" in stderr:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            if "System daemon not running" not in stdout and "System daemon not running" not in stderr:
                return ToolResult(False, dict(last_result.data), "system daemon status failed")
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

        data = dict(last_result.data) if last_result else {"stdout": "", "stderr": ""}
        return ToolResult(False, data, f"system daemon did not become ready within {timeout_sec:.1f}s")

    def logs(self, command: str, **kwargs: Any) -> ToolResult:
        if command != "capture":
            raise ValueError(f"unknown logs command: {command}")
        if "entity_id" in kwargs:
            args = self._iii_command("system", "logs", str(kwargs["entity_id"]))
            if kwargs.get("history", False):
                args.append("--history")
            requested_tail_lines = int(kwargs.get("tail_lines", 200))
            args.extend(["--lines", str(max(1, requested_tail_lines))])
            result = self._run_tool_command(
                args,
                timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
                check=False,
            )
            stdout = result.data.get("stdout", "")
            tail_lines = requested_tail_lines
            if tail_lines > 0:
                stdout = "\n".join(stdout.splitlines()[-tail_lines:])
                result.data["stdout"] = stdout
            if kwargs.get("save", True):
                filename = kwargs.get("filename", f"system_log_{self._safe_name(str(kwargs['entity_id']))}.log")
                result.data["artifact_path"] = str(self._write_artifact(filename, stdout))
            if int(result.data.get("returncode", 1)) != 0:
                return ToolResult(False, result.data, result.data.get("stderr", "system log capture failed"))
            return result
        session = str(kwargs.get("session", "iii_sim_tools"))
        window = str(kwargs.get("window", "simulation"))
        pane = str(kwargs.get("pane", "0"))
        start_line = str(int(kwargs.get("start_line", -200)))
        tail_lines = int(kwargs.get("tail_lines", 200))
        target = f"{session}:{window}.{pane}"
        result = self._run_tool_command(
            ["tmux", "capture-pane", "-pt", target, "-S", start_line],
            timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
            check=False,
        )
        stdout = result.data.get("stdout", "")
        if tail_lines > 0:
            stdout = "\n".join(stdout.splitlines()[-tail_lines:])
            result.data["stdout"] = stdout
        if kwargs.get("save", True):
            filename = kwargs.get("filename", f"tmux_{self._safe_name(target)}.log")
            result.data["artifact_path"] = str(self._write_artifact(filename, stdout))
        if int(result.data.get("returncode", 1)) != 0:
            return ToolResult(False, result.data, result.data.get("stderr", "tmux capture failed"))
        return result

    def critical_node_safety(
        self,
        entities: Sequence[str] | None = None,
        since_iso: str | None = None,
        timeout_sec: float = 5.0,
    ) -> ToolResult:
        selected_entities = list(entities or ("maneuver_controller", "mission_executor"))
        since = None
        if since_iso:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)

        run_end_re = re.compile(
            r"RUN END: entity=(?P<entity>\S+) .*? returncode=(?P<returncode>-?\d+) time=(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \+0000"
        )
        failures: list[dict[str, Any]] = []
        inspected: dict[str, Any] = {}

        for entity in selected_entities:
            try:
                log_result = self.logs(
                    command="capture",
                    entity_id=entity,
                    history=True,
                    timeout_sec=timeout_sec,
                    save=False,
                    tail_lines=0,
                )
            except Exception as exc:
                inspected[entity] = {"available": False, "error": str(exc)}
                continue

            stdout = ""
            if isinstance(log_result.data, dict):
                stdout = str(log_result.data.get("stdout", ""))
            inspected[entity] = {"available": bool(log_result.success), "error": None if log_result.success else log_result.message}

            for match in run_end_re.finditer(stdout):
                returncode = int(match.group("returncode"))
                event_time = datetime.strptime(match.group("time"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if since is not None and event_time < since:
                    continue
                if returncode != 0:
                    failures.append(
                        {
                            "entity": entity,
                            "returncode": returncode,
                            "time": event_time.isoformat(),
                            "verdict": "node_crash",
                        }
                    )

        data = {
            "success": len(failures) == 0,
            "verdict": "passed" if not failures else "node_crash",
            "entities": selected_entities,
            "since_iso": since.isoformat() if since else None,
            "inspected": inspected,
            "failures": failures,
        }
        return ToolResult(data["success"], data, "critical nodes ok" if data["success"] else "critical node failure detected")

    def _system_start_until_ready(
        self,
        timeout_sec: float,
        *,
        entity_id: str | None = None,
        include_dependencies: bool = False,
    ) -> ToolResult:
        deadline = time.monotonic() + timeout_sec
        attempts: list[dict[str, Any]] = []
        last_result: ToolResult | None = None
        while time.monotonic() < deadline:
            per_attempt_timeout = max(5.0, min(120.0, deadline - time.monotonic()))
            command = self._iii_command("system", "start")
            if entity_id:
                command.extend(["--select-nodes", entity_id])
                if include_dependencies:
                    command.append("--include-dependencies")
            last_result = self._run_tool_command(
                command,
                timeout_sec=per_attempt_timeout,
                daemon_timeout_sec=max(per_attempt_timeout, 30.0),
            )
            stdout = last_result.data.get("stdout", "")
            stderr = last_result.data.get("stderr", "")
            daemon_not_running = (
                "System daemon not running" in str(stdout)
                or "System daemon not running" in str(stderr)
            )
            daemon_timed_out = (
                int(last_result.data.get("returncode", 1)) == -1
                and "command timed out" in str(stderr)
                and "Starting system:" in str(stdout)
            )
            if daemon_not_running or daemon_timed_out:
                boot_timeout = min(30.0, max(5.0, deadline - time.monotonic()))
                if boot_timeout > 0.0:
                    if daemon_timed_out:
                        self._run_tool_command(
                            self._iii_command("system", "daemon", "restart"),
                            timeout_sec=boot_timeout,
                            daemon_timeout_sec=boot_timeout,
                            check=False,
                        )
                    self._run_tool_command(
                        self._iii_command("system", "boot"),
                        timeout_sec=boot_timeout,
                        daemon_timeout_sec=boot_timeout,
                        check=False,
                    )
                    self._wait_for_system_daemon_ready(timeout_sec=boot_timeout)
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                time.sleep(min(1.0, remaining))
                continue

            attempts.append(dict(last_result.data))
            blocked = "Blocked nodes:" in stdout or "service-blocked nodes left inactive" in stdout
            if last_result.success and not blocked:
                data = dict(last_result.data)
                data["attempts"] = attempts
                return ToolResult(True, data, last_result.message)
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(3.0, remaining))

        data = dict(last_result.data) if last_result else {"stdout": "", "stderr": ""}
        data["attempts"] = attempts
        scope = entity_id or "all managed nodes"
        return ToolResult(False, data, f"system start for {scope} did not reach active state within {timeout_sec}s")

    def simulation(
        self,
        command: str,
        timeout_sec: Optional[float] = None,
        headless: bool = False,
        wait_ready: bool = True,
        ready_timeout_sec: float = 180.0,
    ) -> ToolResult:
        script = self._workspace_root / "tools" / "simulation" / "launch_simulation_tools.sh"
        if command == "start":
            timeout_sec = 180.0 if timeout_sec is None else timeout_sec
            command_args = [str(script), "--no-attach"]
            if headless:
                command_args.append("--headless")
            result = self._run_tool_command(command_args, timeout_sec=timeout_sec)
            return self._wait_for_simulation_ready(result, ready_timeout_sec) if result.success and wait_ready else result
        if command == "restart":
            timeout_sec = 180.0 if timeout_sec is None else timeout_sec
            command_args = [str(script), "--no-attach", "--recreate"]
            if headless:
                command_args.append("--headless")
            result = self._run_tool_command(command_args, timeout_sec=timeout_sec)
            return self._wait_for_simulation_ready(result, ready_timeout_sec) if result.success and wait_ready else result
        if command == "stop":
            timeout_sec = 30.0 if timeout_sec is None else timeout_sec
            return self._run_tool_command([str(script), "--stop"], timeout_sec=timeout_sec)
        if command == "status":
            timeout_sec = 10.0 if timeout_sec is None else timeout_sec
            return self._run_tool_command([str(script), "--status"], timeout_sec=timeout_sec)
        raise ValueError(f"unknown simulation command: {command}")

    @staticmethod
    def _simulation_status_flags(stdout: str) -> dict[str, bool]:
        lines = {line.strip() for line in str(stdout).splitlines()}
        session_running = "tmux_session: running" in lines
        px4_instance_present = "px4_instance_state: lock_or_socket_present" in lines
        gazebo_transport_available = "gazebo_transport: available" in lines
        simulation_processes_running = any(
            line.startswith("simulation_process_groups:") and not line.endswith("none")
            for line in lines
        )
        return {
            "session_running": session_running,
            "simulation_processes_running": simulation_processes_running,
            "px4_instance_present": px4_instance_present,
            "gazebo_transport_available": gazebo_transport_available,
            "backend_processes_ready": (
                session_running and simulation_processes_running and px4_instance_present
            ),
        }

    @staticmethod
    def _system_status_booted(result: ToolResult) -> bool:
        if not result.success or not isinstance(result.data, dict):
            return False
        return "Booted: True" in str(result.data.get("stdout", "")).splitlines()

    def _wait_for_simulation_ready(self, launch_result: ToolResult, timeout_sec: float) -> ToolResult:
        started = time.monotonic()
        deadline = started + timeout_sec
        attempts: list[dict[str, Any]] = []
        last_error = ""

        while time.monotonic() < deadline:
            status = self.simulation("status", timeout_sec=10.0, wait_ready=False)
            attempts.append({"status": status.data})
            stdout = status.data.get("stdout", "")
            status_flags = self._simulation_status_flags(stdout)
            if status_flags["backend_processes_ready"]:
                px4 = Px4CommandClient(self._px4_system_address)
                try:
                    remaining = max(1.0, deadline - time.monotonic())
                    telemetry = asyncio.run(
                        asyncio.wait_for(
                            self._px4_connect_and_snapshot(px4),
                            timeout=min(12.0, remaining),
                        )
                    )
                    data = dict(launch_result.data)
                    data["ready_after_sec"] = time.monotonic() - started
                    data["ready_attempts"] = attempts
                    data["px4_telemetry"] = asdict(telemetry)
                    data["simulation_status_flags"] = status_flags
                    data["readiness_warnings"] = []
                    if not status_flags["gazebo_transport_available"]:
                        data["readiness_warnings"].append(
                            "Gazebo scene-service discovery was not confirmed by this status sample; "
                            "backend processes and PX4 telemetry are ready."
                        )
                    return ToolResult(True, data, "simulation ready")
                except Exception as exc:
                    last_error = str(exc)
            time.sleep(2.0)

        data = dict(launch_result.data)
        data["ready_after_sec"] = time.monotonic() - started
        data["ready_attempts"] = attempts
        data["last_error"] = last_error
        return ToolResult(False, data, f"simulation not ready within {timeout_sec}s")

    @staticmethod
    async def _px4_connect_and_snapshot(px4: Px4CommandClient) -> Any:
        try:
            await px4.connect()
            return await px4.telemetry_snapshot()
        finally:
            await px4.close_async()

    def inspect(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "ros_nodes":
            return self._run_tool_command(
                ["ros2", "node", "list", "--no-daemon", "--spin-time", str(float(kwargs.get("spin_time_sec", 2.0)))],
                timeout_sec=kwargs.get("timeout_sec", 8.0),
            )
        if command == "ros_topics":
            return self._run_tool_command(["ros2", "topic", "list", "-t"], timeout_sec=kwargs.get("timeout_sec", 5.0))
        if command == "ros_services":
            return self._run_tool_command(["ros2", "service", "list", "-t"], timeout_sec=kwargs.get("timeout_sec", 5.0))
        if command == "ros_actions":
            return self._run_tool_command(["ros2", "action", "list", "-t"], timeout_sec=kwargs.get("timeout_sec", 5.0))
        if command == "topic_once":
            topic = kwargs["topic"]
            timeout_sec = float(kwargs.get("timeout_sec", 5.0))
            result = self._run_tool_command(["ros2", "topic", "echo", "--once", topic], timeout_sec=timeout_sec)
            if kwargs.get("save", False):
                artifact = self._write_artifact(f"ros_topic_{self._safe_name(topic)}.txt", result.data["stdout"])
                result.data["artifact_path"] = str(artifact)
            return result
        if command == "plot_path_topic":
            topic = kwargs["topic"]
            timeout_sec = float(kwargs.get("timeout_sec", 5.0))
            result = self._run_tool_command(["ros2", "topic", "echo", "--once", topic], timeout_sec=timeout_sec)
            artifact = self._plot_path_echo(
                result.data["stdout"],
                kwargs.get("filename", f"ros_path_{self._safe_name(topic)}.png"),
            )
            result.data["artifact_path"] = str(artifact)
            return result
        raise ValueError(f"unknown inspection command: {command}")

    def topic(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "list":
            return self._run_tool_command(
                self._ros_topic_list_command(
                    include_types=bool(kwargs.get("include_types", True)),
                    include_hidden=bool(kwargs.get("include_hidden", False)),
                ),
                timeout_sec=kwargs.get("timeout_sec", 5.0),
            )
        if command == "list_info":
            return self._record_topic_info(**kwargs)
        if command == "record_seconds":
            return self._record_topic_for_seconds(**kwargs)
        if command == "record_messages":
            return self._record_topic_messages(**kwargs)
        raise ValueError(f"unknown topic command: {command}")

    def rosbag_record(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "start":
            return self._start_rosbag_recording(**kwargs)
        if command == "stop":
            return self._stop_rosbag_recording(**kwargs)
        if command == "status":
            return self._rosbag_recording_status(**kwargs)
        raise ValueError(f"unknown rosbag_record command: {command}")

    def gazebo(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "topics":
            return self._run_gazebo_command(
                ["gz", "topic", "--list"],
                timeout_sec=kwargs.get("timeout_sec", 5.0),
                require_stdout=True,
            )
        if command == "services":
            return self._run_gazebo_command(
                ["gz", "service", "--list"],
                timeout_sec=kwargs.get("timeout_sec", 5.0),
                require_stdout=True,
            )
        if command == "topic_once":
            topic = kwargs["topic"]
            result = self._run_gazebo_command(
                ["gz", "topic", "-e", "-t", topic, "-n", "1"],
                timeout_sec=kwargs.get("timeout_sec", 5.0),
                check=False,
                require_stdout=True,
            )
            artifact = self._write_artifact(
                kwargs.get("filename", f"gz_topic_{self._safe_name(topic)}.txt"),
                result.data["stdout"],
            )
            result.data["artifact_path"] = str(artifact)
            return result
        if command == "set_camera_pose":
            pose = self._external_camera_pose_from_kwargs(kwargs)
            model_name = kwargs.get("model_name", "agent_external_camera")
            topic = self._external_camera_topic(kwargs, model_name)
            self._ensure_gazebo_external_camera(
                world=kwargs.get("world", "hca_full_pylon_setup"),
                model_name=model_name,
                topic=topic,
                width=int(kwargs.get("width", 1280)),
                height=int(kwargs.get("height", 720)),
                horizontal_fov=float(kwargs.get("horizontal_fov", 1.2)),
                update_rate=float(kwargs.get("update_rate", 5.0)),
                timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
            )
            self._set_gazebo_external_camera_pose(
                world=kwargs.get("world", "hca_full_pylon_setup"),
                model_name=model_name,
                pose=pose,
                timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
            )
            return ToolResult(True, {"pose": pose}, "external Gazebo camera pose set")
        if command == "image_snapshot":
            model_name = kwargs.get("model_name", "agent_external_camera")
            topic = self._external_camera_topic(kwargs, model_name)
            artifact = self._capture_gazebo_external_snapshot(
                filename=kwargs.get("filename", self._timestamped_artifact_name("gazebo_image", topic, "png")),
                world=kwargs.get("world", "hca_full_pylon_setup"),
                model_name=model_name,
                topic=topic,
                pose=self._external_camera_pose_from_kwargs(kwargs),
                width=int(kwargs.get("width", 1280)),
                height=int(kwargs.get("height", 720)),
                horizontal_fov=float(kwargs.get("horizontal_fov", 1.2)),
                update_rate=float(kwargs.get("update_rate", 5.0)),
                timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
            )
            return ToolResult(
                True,
                {"topic": topic, "artifact_path": str(artifact)},
                "external Gazebo image snapshot captured",
            )
        if command == "ros_image_snapshot":
            topic = kwargs.get("topic", "/sensor/cable_camera/image_raw")
            artifact = self._capture_ros_image_snapshot(
                topic=topic,
                filename=kwargs.get("filename", self._timestamped_artifact_name("ros_image", topic, "png")),
                timeout_sec=float(kwargs.get("timeout_sec", 5.0)),
            )
            return ToolResult(True, {"topic": topic, "artifact_path": str(artifact)}, "ROS image snapshot captured")
        raise ValueError(f"unknown Gazebo command: {command}")

    def _run_gazebo_command(
        self,
        command: Sequence[str],
        *,
        timeout_sec: Optional[float] = None,
        check: bool = True,
        require_stdout: bool = False,
    ) -> ToolResult:
        result = self._run_tool_command(command, timeout_sec=timeout_sec, check=check)
        stdout = ((result.data or {}).get("stdout") if isinstance(result.data, dict) else "") or ""
        if (not require_stdout or stdout.strip()) and result.success:
            return result
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return result

        runtime_user = os.environ.get("III_RUNTIME_USER", "iii")
        setup_dev = self._workspace_root / "setup" / "setup_dev.bash"
        install_setup = self._workspace_root / "install" / "setup.bash"
        script = (
            "source /opt/ros/jazzy/setup.bash >/dev/null 2>&1 || true; "
            f"source {shlex.quote(str(install_setup))} >/dev/null 2>&1 || true; "
            f"source {shlex.quote(str(setup_dev))} >/dev/null 2>&1 || true; "
            "export GZ_IP=${GZ_IP:-127.0.0.1}; "
            f"exec {shlex.join(list(command))}"
        )
        fallback = self._run_tool_command(
            ["sudo", "-H", "-u", runtime_user, "bash", "-lc", script],
            timeout_sec=timeout_sec,
            check=check,
        )
        fallback_stdout = ((fallback.data or {}).get("stdout") if isinstance(fallback.data, dict) else "") or ""
        if require_stdout and not fallback_stdout.strip() and result.success:
            return result
        return fallback

    def sim_observation(self, command: str, **kwargs: Any) -> ToolResult:
        if command == "geometry_state":
            return self._sim_geometry_state(**kwargs)
        if command == "record_drone_position":
            return self._sim_record_drone_position(**kwargs)
        if command == "visibility_state":
            return self._sim_visibility_state(**kwargs)
        if command == "trajectory_state":
            return self._sim_trajectory_state(**kwargs)
        if command == "render_snapshot":
            return self._sim_render_snapshot(**kwargs)
        if command == "render_snapshot_set":
            return self._sim_render_snapshot_set(**kwargs)
        if command == "plot_state":
            return self._sim_plot_state(**kwargs)
        if command == "observe_window":
            return self._sim_observe_window(**kwargs)
        if command == "observe_active_goal":
            return self._sim_observe_active_goal(**kwargs)
        if command == "observation_timeline":
            return self._sim_observation_timeline(**kwargs)
        if command == "perception_verdict":
            return self._sim_perception_verdict(**kwargs)
        raise ValueError(f"unknown simulation observation command: {command}")

    def _sim_geometry_state(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        timeout_sec = float(kwargs.get("tf_timeout_sec", kwargs.get("timeout_sec", 3.0)))
        try:
            pose = self._lookup_world_drone_pose(timeout_sec=timeout_sec)
        except Exception as exc:
            pose = kwargs.get("pose") or {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "unavailable_reason": str(exc)}
        point = {"x": float(pose["x"]), "y": float(pose["y"]), "z": float(pose["z"])}
        include_samples = bool(kwargs.get("include_samples", False))
        data = {
            "timestamp": self.node.get_clock().now().nanoseconds / 1e9,
            "frame_id": geometry.frame_id,
            "geometry_path": str(geometry.path),
            "drone_pose": pose,
            "powerline_corridor": corridor_model(geometry),
            "corridor_membership": corridor_membership(geometry, point, margin_m=float(kwargs.get("corridor_margin_m", 0.5))),
            "nearest_conductor": nearest_conductor(geometry, point),
            "conductors": compact_conductors(geometry, include_samples=include_samples),
            "pylons": geometry.pylons,
            "mission_start_positions": geometry.data.get("mission_start_positions", []),
            "demo_overview_positions": geometry.data.get("demo_overview_positions", []),
        }
        try:
            from px4_msgs.msg import VehicleOdometry

            odometry = self._take_message("/fmu/out/vehicle_odometry", VehicleOdometry, 0.3, required=False)
            if odometry is not None:
                data["drone_velocity_m_s"] = {
                    "x": float(odometry.velocity[0]),
                    "y": float(odometry.velocity[1]),
                    "z": float(odometry.velocity[2]),
                }
        except Exception:
            pass
        return ToolResult(True, data, "simulation geometry state")

    def _sim_record_drone_position(self, **kwargs: Any) -> ToolResult:
        raw_id = str(kwargs.get("position_id") or kwargs.get("id") or "").strip()
        if not raw_id:
            raise ValueError("position_id is required")
        position_id = self._normalize_fixture_id(raw_id)
        section = str(kwargs.get("section") or "mission_start_positions")
        valid_sections = {"mission_start_positions", "drone_positions", "demo_overview_positions"}
        if section not in valid_sections:
            raise ValueError("section must be mission_start_positions, drone_positions, or demo_overview_positions")

        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        transform = self._lookup_world_drone_transform(timeout_sec=float(kwargs.get("tf_timeout_sec", kwargs.get("timeout_sec", 3.0))))
        data = dict(geometry.data)
        positions = list(data.get(section, []))
        index = next((i for i, item in enumerate(positions) if item.get("id") == position_id), None)
        record = dict(positions[index]) if index is not None else {"id": position_id}
        record["id"] = position_id
        record["label"] = str(kwargs.get("label") or record.get("label") or raw_id.replace("_", " ").replace("-", " ").title())
        if kwargs.get("category") is not None:
            record["category"] = str(kwargs["category"])
        record["frame_id"] = "world"
        record["recorded_from"] = {
            "transform": "world -> drone",
            "parent_frame": "world",
            "child_frame": "drone",
            "source": "ROS 2 TF",
            "ground_truth_reference": "simulation ROS world frame used by the geometry fixture; do not derive these positions from PX4 local drift",
            "stamp": transform["stamp"],
        }
        record["pose"] = transform["pose"]
        record["orientation_quaternion"] = transform["orientation_quaternion"]
        try:
            gazebo_pose = self._lookup_gazebo_drone_model_pose(timeout_sec=float(kwargs.get("gazebo_timeout_sec", 5.0)))
            record["gazebo_ground_truth_pose"] = gazebo_pose
            record["recorded_from"]["gazebo_world"] = "hca_full_pylon_setup"
            record["recorded_from"]["gazebo_model"] = os.environ.get(
                "III_GAZEBO_DRONE_MODEL", "d4s_dc_drone_0"
            )
            record["recorded_from"]["gazebo_model_pose"] = {
                "position": gazebo_pose["position"],
                "orientation": gazebo_pose["orientation"],
                "yaw": gazebo_pose["yaw"],
            }
            record["recorded_from"][
                "ground_truth_mapping"
            ] = "Mission deploy replays this Gazebo pose remapped through the live ROS/Gazebo drone offset."
        except Exception as exc:
            record["recorded_from"]["gazebo_model_pose_unavailable_reason"] = str(exc)
        record["updated_at"] = self._utc_now_iso()
        intended_use = kwargs.get("intended_use")
        if isinstance(intended_use, list):
            record["intended_use"] = [str(item) for item in intended_use]
        else:
            existing_use = record.get("intended_use") if isinstance(record.get("intended_use"), list) else []
            if "mission_start_scenario" not in existing_use:
                existing_use = [*existing_use, "mission_start_scenario"]
            record["intended_use"] = existing_use
        if isinstance(kwargs.get("expected"), dict):
            record["expected"] = kwargs["expected"]
        else:
            record.setdefault("expected", {})
        note = str(kwargs.get("note") or "").strip()
        notes = list(record.get("notes") or [])
        base_note = "Recorded from live ROS TF as child frame 'drone' relative to parent frame 'world' for scenario start replay."
        if base_note not in notes:
            notes.append(base_note)
        if note:
            notes.append(note)
        record["notes"] = notes

        if index is None:
            positions.append(record)
        else:
            positions[index] = record
        data[section] = positions
        self._write_geometry_json_path(geometry.path, data)
        return ToolResult(
            True,
            {
                "geometry_path": str(geometry.path),
                "section": section,
                "position": record,
                "normalized_position_id": position_id,
            },
            f"recorded {section} position {position_id}",
        )

    def _find_fixture_position(self, position_id: str, *, geometry_path: str = "") -> dict[str, Any] | None:
        target_id = canonical_fixture_id(str(position_id or ""))
        if not target_id:
            return None
        path = Path(geometry_path) if geometry_path else DEFAULT_GEOMETRY_PATH
        if not path.is_absolute():
            path = self._workspace_root / path
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for section in ("mission_start_positions", "drone_positions", "demo_overview_positions"):
            for item in fixture.get(section, []):
                if not isinstance(item, dict):
                    continue
                if canonical_fixture_id(str(item.get("id") or "")) != target_id:
                    continue
                result = dict(item)
                result["_fixture_section"] = section
                result["_fixture_path"] = str(path)
                return result
        return None

    def _fixture_gazebo_pose(self, position: dict[str, Any]) -> dict[str, float] | None:
        pose = position.get("gazebo_ground_truth_pose")
        if not isinstance(pose, dict):
            pose = (position.get("recorded_from") or {}).get("gazebo_model_pose")
        if not isinstance(pose, dict):
            return None
        source = pose.get("position") if isinstance(pose.get("position"), dict) else pose
        if not isinstance(source, dict) or not {"x", "y", "z"}.issubset(source.keys()):
            return None
        yaw = pose.get("yaw")
        orientation = pose.get("orientation") if isinstance(pose.get("orientation"), dict) else None
        if yaw is None and orientation is not None:
            yaw = self._yaw_from_quaternion_dict(orientation)
        if yaw is None:
            fallback = position.get("pose") if isinstance(position.get("pose"), dict) else {}
            yaw = fallback.get("yaw", 0.0)
        return {
            "x": float(source["x"]),
            "y": float(source["y"]),
            "z": float(source["z"]),
            "yaw": float(yaw),
        }

    def apply_fixture_pose(
        self,
        position_id: str,
        *,
        geometry_path: str = "",
        timeout_sec: float = 5.0,
    ) -> ToolResult:
        """Place the Gazebo aircraft at a stored fixture for simulation setup."""
        if os.environ.get("SIMULATION", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return ToolResult(False, message="fixture pose application is available only in simulation")
        position = self._find_fixture_position(position_id, geometry_path=geometry_path)
        if position is None:
            return ToolResult(False, message=f"unknown fixture position: {position_id}")
        gazebo_pose = self._fixture_gazebo_pose(position)
        if gazebo_pose is None:
            return ToolResult(False, message=f"fixture has no Gazebo ground-truth pose: {position_id}")

        try:
            from gz.msgs10.boolean_pb2 import Boolean
            from gz.msgs10.pose_pb2 import Pose
            from gz.transport13 import Node
        except ImportError as exc:
            raise RuntimeError("gz.msgs10 and gz.transport13 are required for fixture pose application") from exc

        recorded_from = position.get("recorded_from") if isinstance(position.get("recorded_from"), dict) else {}
        world = str(recorded_from.get("gazebo_world") or "hca_full_pylon_setup")
        model = os.environ.get(
            "III_GAZEBO_DRONE_MODEL",
            str(recorded_from.get("gazebo_model") or "d4s_dc_drone_0"),
        )
        half_yaw = gazebo_pose["yaw"] / 2.0
        request = Pose()
        request.name = model
        request.position.x = gazebo_pose["x"]
        request.position.y = gazebo_pose["y"]
        request.position.z = gazebo_pose["z"]
        request.orientation.z = math.sin(half_yaw)
        request.orientation.w = math.cos(half_yaw)
        ok, response = Node().request(
            f"/world/{world}/set_pose",
            request,
            Pose,
            Boolean,
            int(float(timeout_sec) * 1000),
        )
        if not ok or not bool(response.data):
            return ToolResult(False, message=f"Gazebo rejected fixture pose application: {position_id}")
        return ToolResult(
            True,
            {
                "fixture_id": position_id,
                "geometry_path": position.get("_fixture_path"),
                "fixture_section": position.get("_fixture_section"),
                "gazebo_world": world,
                "gazebo_model": model,
                "gazebo_pose": gazebo_pose,
                "setup_only": True,
            },
            f"applied simulation fixture pose: {position_id}",
        )

    def _map_gazebo_fixture_to_live_ros_world(
        self,
        gazebo_pose: dict[str, float],
        *,
        mapping_timeout_sec: float = 30.0,
        gazebo_timeout_sec: float = 5.0,
        tf_timeout_sec: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(mapping_timeout_sec)
        attempt = 0
        last_error = ""
        live_ros: dict[str, float] | None = None
        live_gazebo: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            attempt += 1
            try:
                live_ros = self._lookup_world_drone_pose(timeout_sec=float(tf_timeout_sec))
                live_gazebo = self._lookup_gazebo_drone_model_pose(timeout_sec=float(gazebo_timeout_sec))
                break
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.5)
        if live_ros is None or live_gazebo is None:
            raise RuntimeError(f"live Gazebo/ROS mapping unavailable after {attempt} attempt(s): {last_error}")

        yaw_offset = self._normalize_angle(float(live_ros["yaw"]) - float(live_gazebo["yaw"]))
        delta_gazebo = {
            "x": float(gazebo_pose["x"]) - float(live_gazebo["x"]),
            "y": float(gazebo_pose["y"]) - float(live_gazebo["y"]),
            "z": float(gazebo_pose["z"]) - float(live_gazebo["z"]),
            "yaw": self._normalize_angle(float(gazebo_pose["yaw"]) - float(live_gazebo["yaw"])),
        }
        delta_ros_x, delta_ros_y = rotate_gazebo_xy_delta_to_ros(
            delta_gazebo["x"],
            delta_gazebo["y"],
        )
        delta_ros = {
            "x": delta_ros_x,
            "y": delta_ros_y,
            "z": delta_gazebo["z"],
            "yaw": delta_gazebo["yaw"],
        }
        live_gazebo_ros_x, live_gazebo_ros_y = rotate_gazebo_xy_delta_to_ros(
            float(live_gazebo["x"]),
            float(live_gazebo["y"]),
        )
        offset = {
            "x": float(live_ros["x"]) - live_gazebo_ros_x,
            "y": float(live_ros["y"]) - live_gazebo_ros_y,
            "z": float(live_ros["z"]) - float(live_gazebo["z"]),
            "yaw": yaw_offset,
        }
        return {
            "x": float(live_ros["x"]) + delta_ros["x"],
            "y": float(live_ros["y"]) + delta_ros["y"],
            "z": float(live_ros["z"]) + delta_ros["z"],
            "yaw": self._normalize_angle(float(gazebo_pose["yaw"]) + offset["yaw"]),
            "gazebo_ground_truth_pose": gazebo_pose,
            "live_mapping": {
                "method": "target_ros_xy = live_ros_xy + R(-pi/2) * (fixture_gazebo_xy - live_gazebo_xy); target_ros_z = live_ros_z + fixture_gazebo_z - live_gazebo_z; target_yaw = fixture_gazebo_yaw + (live_ros_yaw - live_gazebo_yaw)",
                "position_yaw_offset": GAZEBO_TO_ROS_POSITION_YAW_RAD,
                "attempts": attempt,
                "live_ros_drone_pose": live_ros,
                "live_gazebo_drone_pose": live_gazebo,
                "offset": offset,
                "delta_gazebo": delta_gazebo,
                "delta_ros": delta_ros,
            },
        }

    def _lookup_gazebo_drone_model_pose(
        self,
        *,
        world: str = "hca_full_pylon_setup",
        model_name: str | None = None,
        timeout_sec: float = 5.0,
    ) -> dict[str, Any]:
        model_name = model_name or os.environ.get("III_GAZEBO_DRONE_MODEL", "d4s_dc_drone_0")
        result = self.gazebo(
            "topic_once",
            topic=f"/world/{world}/dynamic_pose/info",
            timeout_sec=timeout_sec,
            filename="gz_dynamic_pose_for_recorded_position.txt",
        )
        stdout = ((result.data or {}).get("stdout") if isinstance(result.data, dict) else "") or ""
        pose = self._parse_gazebo_named_pose(stdout, model_name)
        if pose is None:
            raise RuntimeError(f"Gazebo pose for model {model_name!r} not found on world {world!r}")
        return pose

    @staticmethod
    def _parse_gazebo_named_pose(stdout: str, model_name: str) -> dict[str, Any] | None:
        for block in re.findall(r"pose\s*\{(.*?)(?=\npose\s*\{|\Z)", stdout, flags=re.S):
            if f'name: "{model_name}"' not in block:
                continue
            position = DroneAgentTools._parse_proto_numeric_block(block, "position")
            orientation = DroneAgentTools._parse_proto_numeric_block(block, "orientation")
            orientation.setdefault("x", 0.0)
            orientation.setdefault("y", 0.0)
            orientation.setdefault("z", 0.0)
            orientation.setdefault("w", 1.0)
            yaw = DroneAgentTools._yaw_from_quaternion_dict(orientation)
            return {
                "x": float(position.get("x", 0.0)),
                "y": float(position.get("y", 0.0)),
                "z": float(position.get("z", 0.0)),
                "yaw": yaw,
                "position": {
                    "x": float(position.get("x", 0.0)),
                    "y": float(position.get("y", 0.0)),
                    "z": float(position.get("z", 0.0)),
                },
                "orientation": orientation,
            }
        return None

    @staticmethod
    def _parse_proto_numeric_block(text: str, name: str) -> dict[str, float]:
        match = re.search(rf"{re.escape(name)}\s*\{{(.*?)\}}", text, flags=re.S)
        if not match:
            return {}
        return {
            key: float(value)
            for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([-+0-9.eE]+)", match.group(1))
        }

    @staticmethod
    def _yaw_from_quaternion_dict(quaternion: dict[str, Any]) -> float:
        x = float(quaternion.get("x", 0.0))
        y = float(quaternion.get("y", 0.0))
        z = float(quaternion.get("z", 0.0))
        w = float(quaternion.get("w", 1.0))
        return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))

    def _sim_visibility_state(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        pose = kwargs.get("pose") or self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", kwargs.get("timeout_sec", 3.0))))
        data = visibility_state(
            geometry,
            pose,
            max_range_m=float(kwargs.get("max_range_m", 15.0)),
            horizontal_fov_rad=float(kwargs.get("horizontal_fov_rad", 1.6)),
            upward_cone_rad=float(kwargs.get("upward_cone_rad", 1.2)),
        )
        return ToolResult(True, data, "simulation visibility state")

    def _sim_trajectory_state(self, **kwargs: Any) -> ToolResult:
        timeout_sec = float(kwargs.get("timeout_sec", 3.0))
        sample_duration_sec = float(kwargs.get("sample_duration_sec", 0.5))
        sample_period_sec = float(kwargs.get("sample_period_sec", 0.1))
        max_samples = int(kwargs.get("max_samples", 25))
        samples = self._sample_drone_path(sample_duration_sec=sample_duration_sec, sample_period_sec=sample_period_sec)
        current_pose = samples[-1] if samples else self._lookup_world_drone_pose(timeout_sec=timeout_sec)
        queue = self._take_message("/control/maneuver_controller/maneuver_queue", ManeuverQueue, timeout_sec, required=False)
        reference_mode = self._take_message(
            "/mission/custom_operation/maneuver_reference_client/reference_mode",
            StringStamped,
            min(timeout_sec, 1.0),
            required=False,
        )
        trajectory_setpoint = self._run_tool_command(
            ["ros2", "topic", "echo", "--once", "/fmu/in/trajectory_setpoint"],
            timeout_sec=min(timeout_sec, 2.0),
            check=False,
        )
        data = {
            "timestamp": self.node.get_clock().now().nanoseconds / 1e9,
            "frame_id": "world",
            "current_pose": current_pose,
            "recent_path": decimate(samples, max_samples),
            "sample_count": len(samples),
            "maneuver_queue": self._message_to_nested_dict(queue),
            "reference_mode": self._message_to_nested_dict(reference_mode),
            "trajectory_setpoint_echo": trajectory_setpoint.data,
        }
        return ToolResult(True, data, "simulation trajectory state")

    def _sim_render_snapshot(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        view = str(kwargs.get("view", "custom"))
        pose = self._snapshot_pose_for_view(geometry, view, kwargs)
        model_name = kwargs.get("model_name", f"agent_external_camera_{view}")
        topic = self._external_camera_topic(kwargs, model_name)
        filename = kwargs.get("filename", self._timestamped_artifact_name(f"sim_{view}", topic, "png"))
        artifact = self._capture_gazebo_external_snapshot(
            filename=filename,
            world=kwargs.get("world", geometry.data.get("world", "hca_full_pylon_setup")),
            model_name=model_name,
            topic=topic,
            pose=pose,
            width=int(kwargs.get("width", 1280)),
            height=int(kwargs.get("height", 720)),
            horizontal_fov=float(kwargs.get("horizontal_fov", 1.2)),
            update_rate=float(kwargs.get("update_rate", 5.0)),
            timeout_sec=float(kwargs.get("timeout_sec", 8.0)),
        )
        width = int(kwargs.get("width", 1280))
        height = int(kwargs.get("height", 720))
        horizontal_fov = float(kwargs.get("horizontal_fov", 1.2))
        projection = self._snapshot_projection_audit(geometry, pose, width=width, height=height, horizontal_fov=horizontal_fov, kwargs=kwargs)
        semantic_audit = self._semantic_image_audit(artifact, projection, view=view, expected_subjects=kwargs.get("expected_subjects"))
        data = {
            "view": view,
            "topic": topic,
            "camera_pose": pose,
            "camera_target_metadata": self._snapshot_target_metadata(geometry, view, kwargs),
            "projection": projection,
            "semantic_audit": semantic_audit,
            "image": image_metadata(artifact),
        }
        return ToolResult(True, data, "simulation render snapshot captured")

    def _sim_render_snapshot_set(self, **kwargs: Any) -> ToolResult:
        views = kwargs.get("views") or ["topdown", "follow_drone", "corridor", "perception_fov"]
        artifacts = {}
        for view in views:
            local_kwargs = dict(kwargs)
            local_kwargs["view"] = view
            local_kwargs["filename"] = kwargs.get("filename") or self._timestamped_artifact_name(f"sim_{view}", view, "png")
            result = self._sim_render_snapshot(**local_kwargs)
            artifacts[view] = result.data
        return ToolResult(True, {"snapshots": artifacts}, "simulation render snapshot set captured")

    def _sim_plot_state(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        sample_duration_sec = float(kwargs.get("sample_duration_sec", 2.0))
        sample_period_sec = float(kwargs.get("sample_period_sec", 0.2))
        path_samples = kwargs.get("path_samples") or self._sample_drone_path(
            sample_duration_sec=sample_duration_sec,
            sample_period_sec=sample_period_sec,
        )
        plots = self._write_simulation_plots(geometry, path_samples, prefix=str(kwargs.get("prefix", "sim_state")))
        return ToolResult(True, {"plots": plots, "sample_count": len(path_samples)}, "simulation plots written")

    def _sim_observe_window(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        duration_sec = float(kwargs.get("duration_sec", 3.0))
        sample_period_sec = float(kwargs.get("sample_period_sec", 0.2))
        capture_snapshots = bool(kwargs.get("capture_snapshots", True))
        start_snapshots = {}
        end_snapshots = {}
        if capture_snapshots:
            snapshot_kwargs = {
                key: kwargs[key]
                for key in ("geometry_path", "world", "width", "height", "horizontal_fov", "timeout_sec")
                if key in kwargs
            }
            start_snapshots = self._sim_render_snapshot_set(
                views=["topdown", "follow_drone"],
                filename=None,
                **snapshot_kwargs,
            ).data["snapshots"]
        samples = kwargs.get("path_samples") or self._sample_drone_path(
            sample_duration_sec=duration_sec,
            sample_period_sec=sample_period_sec,
        )
        if capture_snapshots:
            end_snapshots = self._sim_render_snapshot_set(
                views=["topdown", "follow_drone", "corridor"],
                filename=None,
                **snapshot_kwargs,
            ).data["snapshots"]
        plots = self._write_simulation_plots(geometry, samples, prefix=str(kwargs.get("prefix", "observe_window")))
        verdict = self._observation_verdict(
            geometry,
            samples,
            expected_corridor=kwargs.get("expected_corridor"),
            min_conductor_clearance_m=kwargs.get("min_conductor_clearance_m"),
            min_sample_count=int(kwargs.get("min_sample_count", 2)),
            expected_dx=kwargs.get("expected_dx"),
            expected_dy=kwargs.get("expected_dy"),
            expected_dz=kwargs.get("expected_dz"),
            expected_displacement_tolerance_m=kwargs.get("expected_displacement_tolerance_m"),
            min_distance_traveled_m=kwargs.get("min_distance_traveled_m"),
            max_distance_traveled_m=kwargs.get("max_distance_traveled_m"),
            hover_max_drift_m=kwargs.get("hover_max_drift_m"),
        )
        px4_failsafe_samples = [
            sample for sample in samples
            if (((sample.get("state") or {}).get("vehicle_status") or {}).get("data") or {}).get("failsafe") is True
        ]
        verdict["checks"]["px4_failsafe_absent"] = len(px4_failsafe_samples) == 0
        verdict["metrics"]["px4_failsafe_sample_count"] = len(px4_failsafe_samples)
        verdict["success"] = bool(verdict["success"]) and verdict["checks"]["px4_failsafe_absent"]
        expected_mission_mode = kwargs.get("expected_mission_mode")
        expected_mission_success = kwargs.get("expected_mission_success")
        fail_on_mission_failure = bool(kwargs.get("fail_on_mission_failure", False))
        mission_summary = self._mission_status_summary(samples, expected_mode=expected_mission_mode)
        if mission_summary["available_count"] > 0:
            verdict["metrics"]["mission"] = mission_summary
        if fail_on_mission_failure:
            verdict["checks"]["mission_failure_absent"] = not mission_summary["failed"]
        if expected_mission_success is not None:
            verdict["checks"]["mission_success_confirmed"] = (
                mission_summary["succeeded"] is bool(expected_mission_success)
            )
        verdict["success"] = bool(verdict["success"]) and all(bool(value) for value in verdict["checks"].values())
        output = {
            "duration_sec": duration_sec,
            "sample_period_sec": sample_period_sec,
            "samples": decimate(samples, int(kwargs.get("max_returned_samples", 50))),
            "sample_count": len(samples),
            "start_snapshots": start_snapshots,
            "end_snapshots": end_snapshots,
            "plots": plots,
            "verdict": verdict,
        }
        artifact = write_json(self.artifact_dir / kwargs.get("filename", "sim_observe_window.json"), output)
        output["artifact_path"] = str(artifact)
        return ToolResult(bool(verdict["success"]), output, verdict["summary"])

    @staticmethod
    def _mission_status_summary(samples: list[dict[str, Any]], expected_mode: Any = None) -> dict[str, Any]:
        expected_mode_key = str(expected_mode) if expected_mode else None
        statuses: list[dict[str, Any]] = []
        for sample in samples:
            mission_status = ((sample.get("state") or {}).get("mission_status") or {})
            if not isinstance(mission_status, dict):
                continue
            mode_items = (
                [(expected_mode_key, mission_status.get(expected_mode_key))]
                if expected_mode_key
                else list(mission_status.items())
            )
            for mode_key, wrapper in mode_items:
                parsed = DroneAgentTools._parse_mission_status_wrapper(wrapper)
                if parsed is None:
                    continue
                parsed.setdefault("mode_key", mode_key)
                statuses.append(parsed)

        latest = statuses[-1] if statuses else None
        terminal_statuses = [status for status in statuses if status.get("tree_finished") is True]
        failed_statuses = [
            status for status in terminal_statuses
            if status.get("tree_success") is False
        ]
        succeeded_statuses = [
            status for status in terminal_statuses
            if status.get("tree_success") is True
        ]
        return {
            "expected_mode": expected_mode_key,
            "available_count": len(statuses),
            "terminal_count": len(terminal_statuses),
            "failed": bool(failed_statuses),
            "succeeded": bool(succeeded_statuses),
            "latest": latest,
            "first_failure": failed_statuses[0] if failed_statuses else None,
            "first_success": succeeded_statuses[0] if succeeded_statuses else None,
        }

    @staticmethod
    def _parse_mission_status_wrapper(wrapper: Any) -> dict[str, Any] | None:
        if not isinstance(wrapper, dict) or not wrapper.get("available"):
            return None
        data = wrapper.get("data")
        if not isinstance(data, dict):
            return None
        payload = data.get("data")
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"raw": payload}
        elif isinstance(payload, dict):
            parsed = dict(payload)
        else:
            parsed = {}
        if not parsed:
            return None
        parsed["stamp"] = data.get("stamp")
        metadata = wrapper.get("metadata")
        if isinstance(metadata, dict):
            parsed["metadata"] = {
                key: metadata.get(key)
                for key in ("received_at_wall", "age_sec", "stale")
                if key in metadata
            }
        return parsed

    def _sim_observe_active_goal(self, **kwargs: Any) -> ToolResult:
        goal_id = str(kwargs["goal_id"])
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        max_duration_sec = float(kwargs.get("max_duration_sec", 30.0))
        sample_period_sec = float(kwargs.get("sample_period_sec", 0.2))
        capture_snapshots = bool(kwargs.get("capture_snapshots", False))
        samples: list[dict[str, Any]] = []
        goal_timeline: list[dict[str, Any]] = []
        start_snapshots = {}
        end_snapshots = {}
        start_control_snapshot = self._operation_control_snapshot()

        if capture_snapshots:
            start_snapshots = self._sim_render_snapshot_set(views=["topdown", "follow_drone"], filename=None).data["snapshots"]

        deadline = time.monotonic() + max_duration_sec
        terminal_states = {"succeeded", "failed", "cancelled", "rejected"}
        final_goal: dict[str, Any] | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            status = self.operation_goal_status(goal_id)
            goal_data = status.data if isinstance(status.data, dict) else {"state": "unknown", "error": status.message}
            goal_timeline.append(
                {
                    "t": time.time(),
                    "state": goal_data.get("state"),
                    "feedback_count": goal_data.get("feedback_count"),
                    "last_feedback_age_sec": goal_data.get("last_feedback_age_sec"),
                }
            )
            final_goal = goal_data
            try:
                samples.append(self._lookup_world_drone_pose(timeout_sec=min(0.5, sample_period_sec)))
            except Exception:
                pass
            if goal_data.get("state") in terminal_states:
                break
            time.sleep(max(0.02, sample_period_sec))

        if capture_snapshots:
            end_snapshots = self._sim_render_snapshot_set(views=["topdown", "follow_drone", "corridor"], filename=None).data["snapshots"]
        end_control_snapshot = self._operation_control_snapshot()

        plots = self._write_simulation_plots(geometry, samples, prefix=str(kwargs.get("prefix", "observe_active_goal")))
        verdict = self._observation_verdict(
            geometry,
            samples,
            expected_corridor=kwargs.get("expected_corridor"),
            min_conductor_clearance_m=kwargs.get("min_conductor_clearance_m"),
            min_sample_count=int(kwargs.get("min_sample_count", 2)),
            expected_dx=kwargs.get("expected_dx"),
            expected_dy=kwargs.get("expected_dy"),
            expected_dz=kwargs.get("expected_dz"),
            expected_displacement_tolerance_m=kwargs.get("expected_displacement_tolerance_m"),
            min_distance_traveled_m=kwargs.get("min_distance_traveled_m"),
            max_distance_traveled_m=kwargs.get("max_distance_traveled_m"),
            hover_max_drift_m=kwargs.get("hover_max_drift_m"),
        )
        final_state = (final_goal or {}).get("state", "unknown")
        target_summary = (final_goal or {}).get("target_summary") or {}
        if samples and all(key in target_summary for key in ("x", "y", "z")):
            target_point = point_from_any(target_summary)
            valid_points = [point for point in samples if all(key in point for key in ("x", "y", "z"))]
            if valid_points:
                verdict["metrics"]["distance_to_target_initial_m"] = distance(valid_points[0], target_point)
                verdict["metrics"]["distance_to_target_final_m"] = distance(valid_points[-1], target_point)
                verdict["metrics"]["target_progress_m"] = verdict["metrics"]["distance_to_target_initial_m"] - verdict["metrics"]["distance_to_target_final_m"]
                min_progress = kwargs.get("min_target_progress_m")
                if min_progress is not None:
                    verdict["checks"]["minimum_target_progress_ok"] = verdict["metrics"]["target_progress_m"] >= float(min_progress)
                    verdict["success"] = all(verdict["checks"].values())
                max_regression = kwargs.get("max_target_regression_m")
                if max_regression is not None:
                    verdict["checks"]["target_regression_ok"] = verdict["metrics"]["target_progress_m"] >= -float(max_regression)
                    verdict["success"] = all(verdict["checks"].values())
        output = {
            "goal_id": goal_id,
            "max_duration_sec": max_duration_sec,
            "sample_period_sec": sample_period_sec,
            "samples": decimate(samples, int(kwargs.get("max_returned_samples", 50))),
            "sample_count": len(samples),
            "goal_timeline": decimate(goal_timeline, int(kwargs.get("max_returned_goal_samples", 100))),
            "final_goal": final_goal,
            "start_control_snapshot": start_control_snapshot,
            "end_control_snapshot": end_control_snapshot,
            "start_snapshots": start_snapshots,
            "end_snapshots": end_snapshots,
            "plots": plots,
            "verdict": verdict,
        }
        artifact = write_json(self.artifact_dir / kwargs.get("filename", "sim_observe_active_goal.json"), output)
        output["artifact_path"] = str(artifact)
        require_terminal = bool(kwargs.get("require_terminal", False))
        terminal_ok = final_state in terminal_states or not require_terminal
        success = bool(verdict["success"]) and terminal_ok
        return ToolResult(success, output, f"observed goal {goal_id}: {final_state}, {verdict['summary']}")

    def _operation_control_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        try:
            from px4_msgs.msg import TrajectorySetpoint, VehicleStatus
            trajectory_setpoint = self._take_message("/fmu/in/trajectory_setpoint", TrajectorySetpoint, 0.15, required=False)
            vehicle_status = self._take_message("/fmu/out/vehicle_status_v1", VehicleStatus, 0.15, required=False)
            snapshot["trajectory_setpoint"] = self._message_to_nested_dict(trajectory_setpoint) if trajectory_setpoint else None
            snapshot["vehicle_status"] = self._message_to_nested_dict(vehicle_status) if vehicle_status else None
        except Exception as exc:
            snapshot["px4_snapshot_error"] = str(exc)
        try:
            reference_mode = self._take_message(
                "/mission/custom_operation/maneuver_reference_client/reference_mode",
                StringStamped,
                0.15,
                required=False,
            )
            snapshot["reference_mode"] = self._message_to_nested_dict(reference_mode) if reference_mode else None
        except Exception as exc:
            snapshot["reference_mode_error"] = str(exc)
        try:
            maneuver_queue = self._take_message("/control/maneuver_controller/maneuver_queue", ManeuverQueue, 0.15, required=False)
            snapshot["maneuver_queue"] = self._message_to_nested_dict(maneuver_queue) if maneuver_queue else None
        except Exception as exc:
            snapshot["maneuver_queue_error"] = str(exc)
        return snapshot

    def _take_cached_message(
        self,
        topic: str,
        msg_type: Any,
        timeout_sec: float = 0.05,
        *,
        stale_after_sec: float | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        if topic not in self._topic_cache_subscriptions:
            def on_message(message: Any, *, cache_topic: str = topic) -> None:
                self._topic_cache[cache_topic] = {
                    "message": message,
                    "received_at_wall": time.time(),
                    "received_at_ros": self.node.get_clock().now().nanoseconds / 1e9,
                }

            self._topic_cache_subscriptions[topic] = self.node.create_subscription(
                msg_type,
                topic,
                on_message,
                qos_profile_sensor_data,
            )

        deadline = time.monotonic() + max(0.0, timeout_sec)
        while topic not in self._topic_cache and time.monotonic() < deadline and rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))

        cached = self._topic_cache.get(topic)
        if cached is None:
            return None, {
                "source": "cache",
                "cache_hit": False,
                "received_at_wall": None,
                "received_at_ros": None,
                "age_sec": None,
                "stale_after_sec": stale_after_sec,
                "stale": None,
            }

        age_sec = max(0.0, time.time() - float(cached["received_at_wall"]))
        return cached["message"], {
            "source": "cache",
            "cache_hit": True,
            "received_at_wall": cached["received_at_wall"],
            "received_at_ros": cached["received_at_ros"],
            "age_sec": age_sec,
            "stale_after_sec": stale_after_sec,
            "stale": bool(stale_after_sec is not None and age_sec > stale_after_sec),
        }

    def _take_optional_nested_message(
        self,
        topic: str,
        msg_type: Any,
        timeout_sec: float = 0.05,
        *,
        cached: bool = False,
        stale_after_sec: float | None = None,
    ) -> dict[str, Any]:
        try:
            metadata: dict[str, Any] = {"source": "one_shot", "cache_hit": None, "age_sec": None}
            if cached:
                msg, metadata = self._take_cached_message(topic, msg_type, timeout_sec, stale_after_sec=stale_after_sec)
            else:
                msg = self._take_message(topic, msg_type, timeout_sec, required=False)
            return {
                "available": msg is not None,
                "topic": topic,
                "data": self._message_to_nested_dict(msg) if msg is not None else None,
                "error": None,
                "metadata": metadata,
            }
        except Exception as exc:
            return {
                "available": False,
                "topic": topic,
                "data": None,
                "error": str(exc),
                "metadata": {
                    "source": "cache" if cached else "one_shot",
                    "stale_after_sec": stale_after_sec,
                    "stale": None,
                },
            }

    def _collect_observation_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        try:
            from px4_msgs.msg import FailsafeFlags, TrajectorySetpoint, VehicleControlMode, VehicleStatus
            state["vehicle_status"] = self._take_optional_nested_message("/fmu/out/vehicle_status_v1", VehicleStatus, cached=True, stale_after_sec=1.0)
            state["vehicle_control_mode"] = self._take_optional_nested_message("/fmu/out/vehicle_control_mode", VehicleControlMode, cached=True, stale_after_sec=1.0)
            state["failsafe_flags"] = self._take_optional_nested_message("/fmu/out/failsafe_flags", FailsafeFlags, cached=True, stale_after_sec=1.0)
            state["trajectory_setpoint"] = self._take_optional_nested_message("/fmu/in/trajectory_setpoint", TrajectorySetpoint, cached=True, stale_after_sec=1.0)
        except Exception as exc:
            state["px4_state_error"] = str(exc)

        maneuver_queue = self._take_optional_nested_message(
            "/control/maneuver_controller/maneuver_queue",
            ManeuverQueue,
            cached=True,
            stale_after_sec=1.0,
        )
        state["maneuver_queue"] = maneuver_queue
        queue_data = maneuver_queue.get("data") if isinstance(maneuver_queue, dict) else None
        state["current_maneuver"] = None if not isinstance(queue_data, dict) else queue_data.get("current_maneuver")

        state["mission_status"] = {
            "reach_cable": self._take_optional_nested_message("/mission/modes/reach_cable/status", StringStamped, cached=True, stale_after_sec=2.0),
        }
        state["operation_status"] = {
            "custom_operation": self._take_optional_nested_message("/mission/custom_operation/status", StringStamped, cached=True, stale_after_sec=2.0),
        }
        try:
            state["operation_active"] = {"available": True, "data": self.active_operation_goal().data, "error": None}
        except Exception as exc:
            state["operation_active"] = {"available": False, "data": None, "error": str(exc)}
        return state

    def _prime_observation_timeline_cache(self, timeout_sec: float) -> None:
        try:
            from px4_msgs.msg import FailsafeFlags, TrajectorySetpoint, VehicleControlMode, VehicleStatus

            self._take_cached_message("/fmu/out/vehicle_status_v1", VehicleStatus, 0.0)
            self._take_cached_message("/fmu/out/vehicle_control_mode", VehicleControlMode, 0.0)
            self._take_cached_message("/fmu/out/failsafe_flags", FailsafeFlags, 0.0)
            self._take_cached_message("/fmu/in/trajectory_setpoint", TrajectorySetpoint, 0.0)
        except Exception:
            pass
        self._take_cached_message("/control/maneuver_controller/maneuver_queue", ManeuverQueue, 0.0)
        self._take_cached_message("/mission/modes/reach_cable/status", StringStamped, 0.0)
        self._take_cached_message("/mission/custom_operation/status", StringStamped, 0.0)
        try:
            from iii_drone_interfaces.msg import Powerline

            self._take_cached_message("/perception/pl_mapper/powerline", Powerline, 0.0)
        except Exception:
            pass

        deadline = time.monotonic() + max(0.0, timeout_sec)
        while time.monotonic() < deadline and rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())))

    def _sim_observation_timeline(self, **kwargs: Any) -> ToolResult:
        duration_sec = float(kwargs.get("duration_sec", 5.0))
        sample_period_sec = max(0.05, float(kwargs.get("sample_period_sec", 0.5)))
        warmup_sec = max(0.0, float(kwargs.get("warmup_sec", 0.25)))
        self._prime_observation_timeline_cache(warmup_sec)
        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + duration_sec
        while rclpy.ok() and time.monotonic() <= deadline:
            sample: dict[str, Any] = {"t": time.time()}
            try:
                sample["pose"] = self._lookup_world_drone_pose(timeout_sec=min(0.2, sample_period_sec))
            except Exception as exc:
                sample["pose_error"] = str(exc)
            try:
                sample["operation_active"] = self.active_operation_goal().data
            except Exception as exc:
                sample["operation_error"] = str(exc)
            sample["state"] = self._collect_observation_state()
            try:
                from iii_drone_interfaces.msg import Powerline
                powerline, metadata = self._take_cached_message("/perception/pl_mapper/powerline", Powerline, 0.1, stale_after_sec=1.0)
                sample["perception_powerline"] = (
                    None
                    if powerline is None
                    else {
                        "line_count": len(powerline.lines),
                        "stamp": self._message_to_nested_dict(powerline.stamp),
                        "metadata": metadata,
                    }
                )
                if powerline is None:
                    sample["perception_powerline_metadata"] = metadata
            except Exception as exc:
                sample["perception_error"] = str(exc)
            samples.append(sample)
            time.sleep(sample_period_sec)
        artifact = write_json(
            self.artifact_dir / kwargs.get("filename", "sim_observation_timeline.json"),
            {"samples": samples, "sample_count": len(samples), "warmup_sec": warmup_sec},
        )
        return ToolResult(
            True,
            {
                "sample_count": len(samples),
                "warmup_sec": warmup_sec,
                "samples": decimate(samples, int(kwargs.get("max_returned_samples", 50))),
                "artifact_path": str(artifact),
            },
            "simulation observation timeline",
        )

    def _sim_perception_verdict(self, **kwargs: Any) -> ToolResult:
        geometry = load_geometry(self._workspace_root, kwargs.get("geometry_path"))
        timeout_sec = float(kwargs.get("timeout_sec", 3.0))
        try:
            pose = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 1.0)))
        except Exception:
            pose = None
        expected = visibility_state(geometry, pose) if pose else None
        try:
            from iii_drone_interfaces.msg import Powerline
            msg = self._take_message(str(kwargs.get("topic", "/perception/pl_mapper/powerline")), Powerline, timeout_sec, required=False)
        except Exception as exc:
            msg = None
            topic_error = str(exc)
        else:
            topic_error = None
        detected_lines = [] if msg is None else [self._message_to_nested_dict(line) for line in msg.lines]
        expected_ids = [] if expected is None else list(expected["expected_visible_conductor_ids"])
        detected_count = len(detected_lines)
        success = (not expected_ids) or detected_count > 0
        verdict = {
            "success": success,
            "expected_visible_conductor_ids": expected_ids,
            "detection_count": detected_count,
            "detected_lines": detected_lines,
            "topic_error": topic_error,
            "expected_visibility": expected,
            "notes": [] if success else ["conductors expected visible but no powerline detections were received"],
        }
        artifact = write_json(self.artifact_dir / kwargs.get("filename", "sim_perception_verdict.json"), verdict)
        verdict["artifact_path"] = str(artifact)
        return ToolResult(success, verdict, "perception expected-vs-detected verdict")

    def _sample_drone_path(self, *, sample_duration_sec: float, sample_period_sec: float) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + max(0.0, sample_duration_sec)
        period = max(0.02, sample_period_sec)
        next_sample = time.monotonic()
        while time.monotonic() <= deadline and rclpy.ok():
            now = time.monotonic()
            if now < next_sample:
                rclpy.spin_once(self.node, timeout_sec=min(0.05, next_sample - now))
                continue
            try:
                pose = self._lookup_world_drone_pose(timeout_sec=min(0.25, period))
                pose["t"] = self.node.get_clock().now().nanoseconds / 1e9
                pose["state"] = self._collect_observation_state()
                samples.append(pose)
            except Exception as exc:
                samples.append({
                    "t": self.node.get_clock().now().nanoseconds / 1e9,
                    "unavailable_reason": str(exc),
                    "state": self._collect_observation_state(),
                })
            next_sample += period
        return samples

    def _snapshot_pose_for_view(self, geometry: Any, view: str, kwargs: dict[str, Any]) -> dict[str, float]:
        if view == "custom":
            return self._external_camera_pose_from_kwargs(kwargs)

        corridor = corridor_model(geometry)
        center = corridor["center"]
        span_axis = corridor["span_axis"]
        lateral_axis = corridor["lateral_axis"]
        z_range = conductor_height_range(geometry)
        try:
            drone = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 0.5)))
        except Exception:
            drone = {"x": center["x"], "y": center["y"], "z": max(1.0, center["z"]), "yaw": 0.0}

        if view == "topdown":
            height = float(kwargs.get("camera_height_m", max(z_range["max"] + 8.0, drone["z"] + 8.0)))
            target = {
                "x": (center["x"] + float(drone["x"])) * 0.5,
                "y": (center["y"] + float(drone["y"])) * 0.5,
                "z": center["z"],
            }
            return self._camera_pose_looking_at(
                target["x"],
                target["y"],
                height,
                target["x"],
                target["y"],
                target["z"],
            )
        if view == "corridor":
            nearest = nearest_conductor(geometry, point_from_any(drone)).get("closest_point") or center
            target = {
                "x": (float(drone["x"]) + float(nearest["x"])) * 0.5,
                "y": (float(drone["y"]) + float(nearest["y"])) * 0.5,
                "z": (float(drone["z"]) + float(nearest["z"])) * 0.5,
            }
            offset = float(kwargs.get("camera_offset_m", 9.0))
            height = float(kwargs.get("camera_height_m", max(target["z"] + 4.0, drone["z"] + 3.0)))
            return self._camera_pose_looking_at(
                target["x"] - lateral_axis["x"] * offset,
                target["y"] - lateral_axis["y"] * offset,
                height,
                target["x"],
                target["y"],
                target["z"],
            )
        if view == "follow_drone":
            yaw = float(drone.get("yaw", 0.0))
            distance_m = float(kwargs.get("camera_distance_m", 5.0))
            return self._camera_pose_looking_at(
                float(drone["x"]) - math.cos(yaw) * distance_m,
                float(drone["y"]) - math.sin(yaw) * distance_m,
                float(drone["z"]) + float(kwargs.get("camera_height_offset_m", 2.5)),
                float(drone["x"]),
                float(drone["y"]),
                float(drone["z"]),
            )
        if view == "target":
            target = kwargs.get("target") or {
                "x": kwargs.get("target_x", drone["x"]),
                "y": kwargs.get("target_y", drone["y"]),
                "z": kwargs.get("target_z", drone["z"]),
            }
            point = point_from_any(target)
            target_mid = {
                "x": (point["x"] + float(drone["x"])) * 0.5,
                "y": (point["y"] + float(drone["y"])) * 0.5,
                "z": (point["z"] + float(drone["z"])) * 0.5,
            }
            return self._camera_pose_looking_at(
                target_mid["x"] - lateral_axis["x"] * float(kwargs.get("camera_offset_m", 6.0)),
                target_mid["y"] - lateral_axis["y"] * float(kwargs.get("camera_offset_m", 6.0)),
                target_mid["z"] + float(kwargs.get("camera_height_offset_m", 3.0)),
                target_mid["x"],
                target_mid["y"],
                target_mid["z"],
            )
        if view == "perception_fov":
            yaw = float(drone.get("yaw", 0.0))
            visible = visibility_state(geometry, drone).get("conductors", [])
            visible_points = [item["closest_point"] for item in visible if item.get("expected_visible") and item.get("closest_point")]
            if visible_points:
                look = {
                    "x": sum(point["x"] for point in visible_points) / len(visible_points),
                    "y": sum(point["y"] for point in visible_points) / len(visible_points),
                    "z": sum(point["z"] for point in visible_points) / len(visible_points),
                }
            else:
                look = {
                    "x": float(drone["x"]) + math.cos(yaw) * 2.0,
                    "y": float(drone["y"]) + math.sin(yaw) * 2.0,
                    "z": max(float(drone["z"]) + 1.5, center["z"]),
                }
            return self._camera_pose_looking_at(
                float(drone["x"]) - math.cos(yaw) * 4.0,
                float(drone["y"]) - math.sin(yaw) * 4.0,
                float(drone["z"]) + 2.0,
                look["x"],
                look["y"],
                look["z"],
            )
        raise ValueError(f"unknown simulation snapshot view: {view}")

    @staticmethod
    def _camera_pose_looking_at(
        x: float,
        y: float,
        z: float,
        target_x: float,
        target_y: float,
        target_z: float,
    ) -> dict[str, float]:
        dx = target_x - x
        dy = target_y - y
        dz = target_z - z
        yaw = math.atan2(dy, dx)
        horizontal = math.hypot(dx, dy)
        pitch = math.atan2(-dz, horizontal)
        qx, qy, qz, qw = DroneAgentTools._quaternion_from_euler(0.0, pitch, yaw)
        return {"x": x, "y": y, "z": z, "qx": qx, "qy": qy, "qz": qz, "qw": qw}

    def _snapshot_target_metadata(self, geometry: Any, view: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            drone = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 0.2)))
        except Exception:
            drone = None
        target = kwargs.get("target")
        if target is None and any(key in kwargs for key in ("target_x", "target_y", "target_z")):
            target = {"x": kwargs.get("target_x", 0.0), "y": kwargs.get("target_y", 0.0), "z": kwargs.get("target_z", 0.0)}
        return {
            "view": view,
            "drone_pose": drone,
            "target": point_from_any(target) if target else None,
            "corridor_center": corridor_model(geometry)["center"],
            "nearest_conductor": None if drone is None else nearest_conductor(geometry, point_from_any(drone)),
        }

    def _snapshot_projection_audit(
        self,
        geometry: Any,
        camera_pose: dict[str, float],
        *,
        width: int,
        height: int,
        horizontal_fov: float,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        vertical_fov = 2.0 * math.atan(math.tan(horizontal_fov / 2.0) * (height / max(1.0, width)))
        subjects: dict[str, Any] = {}
        try:
            drone = self._lookup_world_drone_pose(timeout_sec=float(kwargs.get("tf_timeout_sec", 0.2)))
            subjects["drone"] = self._project_points(camera_pose, [point_from_any(drone)], width, height, horizontal_fov, vertical_fov)
        except Exception as exc:
            subjects["drone"] = {"error": str(exc), "visible_count": 0, "points": []}

        conductor_items = []
        for conductor in geometry.conductors:
            projection = self._project_points(camera_pose, conductor_samples(conductor), width, height, horizontal_fov, vertical_fov)
            projection["id"] = conductor.get("id")
            conductor_items.append(projection)
        subjects["conductors"] = conductor_items

        pylon_items = []
        pylons = geometry.pylons.get("items", []) if isinstance(geometry.pylons, dict) else geometry.pylons
        for pylon in pylons:
            bbox = pylon.get("bounding_box", {})
            points = self._bbox_corners(bbox) if bbox else []
            projection = self._project_points(camera_pose, points, width, height, horizontal_fov, vertical_fov)
            projection["id"] = pylon.get("id")
            pylon_items.append(projection)
        subjects["pylons"] = pylon_items
        return {"image_size": {"width": width, "height": height}, "horizontal_fov": horizontal_fov, "vertical_fov": vertical_fov, "subjects": subjects}

    def _project_points(
        self,
        camera_pose: dict[str, float],
        points: list[dict[str, Any]],
        width: int,
        height: int,
        horizontal_fov: float,
        vertical_fov: float,
    ) -> dict[str, Any]:
        rotation = self._rotation_matrix_from_quaternion(camera_pose["qx"], camera_pose["qy"], camera_pose["qz"], camera_pose["qw"])
        cx, cy, cz = float(camera_pose["x"]), float(camera_pose["y"]), float(camera_pose["z"])
        projected = []
        for point in points:
            vector = [float(point["x"]) - cx, float(point["y"]) - cy, float(point["z"]) - cz]
            cam = [
                rotation[0][0] * vector[0] + rotation[1][0] * vector[1] + rotation[2][0] * vector[2],
                rotation[0][1] * vector[0] + rotation[1][1] * vector[1] + rotation[2][1] * vector[2],
                rotation[0][2] * vector[0] + rotation[1][2] * vector[1] + rotation[2][2] * vector[2],
            ]
            forward = cam[0]
            visible = forward > 0.05
            if visible:
                px = width / 2.0 + (cam[1] / forward) / math.tan(horizontal_fov / 2.0) * width / 2.0
                py = height / 2.0 - (cam[2] / forward) / math.tan(vertical_fov / 2.0) * height / 2.0
                visible = 0.0 <= px <= width and 0.0 <= py <= height
            else:
                px = py = math.nan
            projected.append({"world": point, "camera": {"x": cam[0], "y": cam[1], "z": cam[2]}, "pixel": {"x": px, "y": py}, "visible": bool(visible)})
        visible_points = [item["pixel"] for item in projected if item["visible"]]
        bbox = None
        if visible_points:
            bbox = {
                "min_x": min(point["x"] for point in visible_points),
                "min_y": min(point["y"] for point in visible_points),
                "max_x": max(point["x"] for point in visible_points),
                "max_y": max(point["y"] for point in visible_points),
            }
        return {"visible_count": len(visible_points), "bbox": bbox, "points": projected}

    @staticmethod
    def _bbox_corners(bbox: dict[str, Any]) -> list[dict[str, float]]:
        mn = bbox["min"]
        mx = bbox["max"]
        return [{"x": x, "y": y, "z": z} for x in (mn["x"], mx["x"]) for y in (mn["y"], mx["y"]) for z in (mn["z"], mx["z"])]

    def _semantic_image_audit(self, path: Path, projection: dict[str, Any], *, view: str, expected_subjects: Any = None) -> dict[str, Any]:
        try:
            from PIL import Image, ImageFilter, ImageStat

            image = Image.open(path).convert("RGB")
            gray = image.convert("L")
            mean_brightness = float(ImageStat.Stat(gray).mean[0])
            edge = gray.filter(ImageFilter.FIND_EDGES)
            edge_mean = float(ImageStat.Stat(edge).mean[0])
        except Exception as exc:
            audit = {"success": False, "error": str(exc)}
            write_json(path.with_suffix(".semantic_audit.json"), audit)
            return audit
        subjects = projection.get("subjects", {})
        conductor_visible = any(item.get("visible_count", 0) >= 2 for item in subjects.get("conductors", []))
        pylon_visible = any(item.get("visible_count", 0) >= 1 for item in subjects.get("pylons", []))
        drone_visible = subjects.get("drone", {}).get("visible_count", 0) >= 1
        checks = {
            "not_blank": bool(image.getbbox()),
            "brightness_ok": mean_brightness >= 8.0,
            "edge_density_ok": edge_mean >= 0.4,
            "conductor_projected_visible": bool(conductor_visible),
        }
        if view != "perception_fov":
            checks["drone_projected_visible"] = bool(drone_visible)
        if view in {"corridor", "topdown"}:
            checks["pylon_or_conductor_projected_visible"] = bool(pylon_visible or conductor_visible)
        if expected_subjects:
            for subject in expected_subjects:
                if subject == "drone":
                    checks["expected_drone_visible"] = bool(drone_visible)
                if subject == "conductors":
                    checks["expected_conductors_visible"] = bool(conductor_visible)
                if subject == "pylons":
                    checks["expected_pylons_visible"] = bool(pylon_visible)
        audit = {
            "success": all(checks.values()),
            "checks": checks,
            "metrics": {"mean_brightness": mean_brightness, "edge_mean": edge_mean},
            "projection_summary": {
                "drone_visible": drone_visible,
                "conductor_visible": conductor_visible,
                "pylon_visible": pylon_visible,
            },
        }
        audit_path = write_json(path.with_suffix(".semantic_audit.json"), audit)
        audit["artifact_path"] = str(audit_path)
        return audit

    @staticmethod
    def _rotation_matrix_from_quaternion(x: float, y: float, z: float, w: float) -> list[list[float]]:
        xx, yy, zz = x * x, y * y, z * z
        xy, xz, yz = x * y, x * z, y * z
        wx, wy, wz = w * x, w * y, w * z
        return [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
        ]

    def _write_simulation_plots(self, geometry: Any, path_samples: list[dict[str, Any]], *, prefix: str) -> dict[str, str]:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        prefix = self._safe_name(prefix)
        points = [
            {"x": float(sample["x"]), "y": float(sample["y"]), "z": float(sample["z"]), "t": float(sample.get("t", idx))}
            for idx, sample in enumerate(path_samples)
            if all(key in sample for key in ("x", "y", "z"))
        ]
        conductors = geometry.conductors
        corridor = corridor_model(geometry)
        artifacts: dict[str, str] = {}

        topdown = self.artifact_dir / f"{prefix}_topdown.png"
        fig, ax = plt.subplots(figsize=(8, 6), dpi=140)
        for conductor in conductors:
            samples = conductor.get("samples") or [conductor.get("start"), conductor.get("end")]
            xs = [float(point["x"]) for point in samples if point]
            ys = [float(point["y"]) for point in samples if point]
            ax.plot(xs, ys, linewidth=1.6, label=conductor.get("id", "conductor"))
        for pylon in geometry.pylons.get("items", []) if isinstance(geometry.pylons, dict) else geometry.pylons:
            bbox = pylon.get("bounding_box", {})
            if bbox:
                min_pt = bbox["min"]
                max_pt = bbox["max"]
                ax.add_patch(
                    plt.Rectangle(
                        (min_pt["x"], min_pt["y"]),
                        max_pt["x"] - min_pt["x"],
                        max_pt["y"] - min_pt["y"],
                        fill=False,
                        linewidth=1.0,
                        color="0.35",
                    )
                )
        if points:
            ax.plot([p["x"] for p in points], [p["y"] for p in points], color="tab:red", linewidth=2.0, label="drone path")
            ax.scatter([points[-1]["x"]], [points[-1]["y"]], color="tab:red", s=24)
        ax.scatter([corridor["center"]["x"]], [corridor["center"]["y"]], color="tab:green", s=18, label="corridor center")
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlabel("world x [m]")
        ax.set_ylabel("world y [m]")
        ax.set_title("Powerline corridor top-down")
        ax.legend(loc="best", fontsize="small")
        ax.grid(True, linewidth=0.3)
        fig.tight_layout()
        fig.savefig(topdown)
        plt.close(fig)
        artifacts["topdown"] = str(topdown)

        side = self.artifact_dir / f"{prefix}_side.png"
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
        span = corridor["span_axis"]
        origin = corridor["center"]

        def span_s(point: dict[str, Any]) -> float:
            return (float(point["x"]) - origin["x"]) * span["x"] + (float(point["y"]) - origin["y"]) * span["y"]

        for conductor in conductors:
            samples = conductor.get("samples") or [conductor.get("start"), conductor.get("end")]
            ax.plot(
                [span_s(point) for point in samples if point],
                [float(point["z"]) for point in samples if point],
                linewidth=1.6,
                label=conductor.get("id", "conductor"),
            )
        if points:
            ax.plot([span_s(p) for p in points], [p["z"] for p in points], color="tab:red", linewidth=2.0, label="drone path")
        ax.set_xlabel("corridor span coordinate [m]")
        ax.set_ylabel("world z [m]")
        ax.set_title("Altitude vs conductor height")
        ax.legend(loc="best", fontsize="small")
        ax.grid(True, linewidth=0.3)
        fig.tight_layout()
        fig.savefig(side)
        plt.close(fig)
        artifacts["side"] = str(side)

        clearance = self.artifact_dir / f"{prefix}_conductor_clearance.png"
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
        if points:
            distances = [nearest_conductor(geometry, p)["distance_m"] for p in points]
            ts = [p["t"] - points[0]["t"] for p in points]
            ax.plot(ts, distances, color="tab:purple", linewidth=2.0)
            ax.set_ylim(bottom=0.0)
        ax.set_xlabel("time [s]")
        ax.set_ylabel("nearest conductor distance [m]")
        ax.set_title("Conductor clearance")
        ax.grid(True, linewidth=0.3)
        fig.tight_layout()
        fig.savefig(clearance)
        plt.close(fig)
        artifacts["conductor_clearance"] = str(clearance)
        return artifacts

    def _observation_verdict(
        self,
        geometry: Any,
        samples: list[dict[str, Any]],
        *,
        expected_corridor: Any = None,
        min_conductor_clearance_m: Any = None,
        min_sample_count: int = 2,
        expected_dx: Any = None,
        expected_dy: Any = None,
        expected_dz: Any = None,
        expected_displacement_tolerance_m: Any = None,
        min_distance_traveled_m: Any = None,
        max_distance_traveled_m: Any = None,
        hover_max_drift_m: Any = None,
    ) -> dict[str, Any]:
        points = [
            {"x": float(sample["x"]), "y": float(sample["y"]), "z": float(sample["z"]), "t": float(sample.get("t", idx))}
            for idx, sample in enumerate(samples)
            if all(key in sample for key in ("x", "y", "z"))
        ]
        checks: dict[str, bool] = {"sampled_state_present": len(points) >= min_sample_count}
        metrics: dict[str, Any] = {"sample_count": len(samples), "valid_pose_sample_count": len(points)}
        if points:
            distances = [nearest_conductor(geometry, point)["distance_m"] for point in points]
            metrics["distance_traveled_m"] = sum(distance(a, b) for a, b in zip(points, points[1:]))
            displacement = {
                "dx": points[-1]["x"] - points[0]["x"],
                "dy": points[-1]["y"] - points[0]["y"],
                "dz": points[-1]["z"] - points[0]["z"],
            }
            metrics["net_displacement_m"] = displacement
            metrics["net_displacement_norm_m"] = math.sqrt(displacement["dx"] ** 2 + displacement["dy"] ** 2 + displacement["dz"] ** 2)
            metrics["altitude_min_m"] = min(point["z"] for point in points)
            metrics["altitude_max_m"] = max(point["z"] for point in points)
            metrics["nearest_conductor_distance_min_m"] = min(distances)
            last_membership = corridor_membership(geometry, points[-1])
            metrics["last_corridor_membership"] = last_membership
            if expected_corridor is not None:
                checks["corridor_membership_expected"] = bool(last_membership["inside_powerline_corridor"]) == bool(expected_corridor)
            if min_conductor_clearance_m is not None:
                checks["minimum_conductor_clearance_ok"] = min(distances) >= float(min_conductor_clearance_m)
            if min_distance_traveled_m is not None:
                checks["minimum_distance_traveled_ok"] = metrics["distance_traveled_m"] >= float(min_distance_traveled_m)
            if max_distance_traveled_m is not None:
                checks["maximum_distance_traveled_ok"] = metrics["distance_traveled_m"] <= float(max_distance_traveled_m)
            if hover_max_drift_m is not None:
                checks["hover_drift_ok"] = metrics["net_displacement_norm_m"] <= float(hover_max_drift_m)
            expected_components = {"dx": expected_dx, "dy": expected_dy, "dz": expected_dz}
            if any(value is not None for value in expected_components.values()):
                tolerance = float(expected_displacement_tolerance_m if expected_displacement_tolerance_m is not None else 0.15)
                metrics["expected_displacement_m"] = {key: (None if value is None else float(value)) for key, value in expected_components.items()}
                metrics["expected_displacement_tolerance_m"] = tolerance
                for key, value in expected_components.items():
                    if value is not None:
                        checks[f"expected_{key}_ok"] = abs(displacement[key] - float(value)) <= tolerance
            checks["visibility_state_available"] = bool(visibility_state(geometry, points[-1]).get("conductors"))
        else:
            metrics["distance_traveled_m"] = 0.0
            metrics["net_displacement_m"] = {"dx": 0.0, "dy": 0.0, "dz": 0.0}
            metrics["net_displacement_norm_m"] = 0.0
            metrics["altitude_min_m"] = None
            metrics["altitude_max_m"] = None
            metrics["nearest_conductor_distance_min_m"] = None
            checks["visibility_state_available"] = False
        success = all(checks.values())
        summary = (
            f"observed {len(points)} valid pose samples, traveled {metrics['distance_traveled_m']:.3f} m"
            if points
            else "no valid world->drone pose samples observed"
        )
        return {"success": success, "summary": summary, "checks": checks, "metrics": metrics}

    def _message_to_nested_dict(self, message: Any) -> Any:
        if message is None:
            return None
        if isinstance(message, (str, int, float, bool)):
            return message
        if isinstance(message, bytes):
            return message.hex()
        if isinstance(message, (list, tuple)):
            if len(message) > 128:
                return {"length": len(message), "sample": [self._message_to_nested_dict(value) for value in message[:16]]}
            return [self._message_to_nested_dict(value) for value in message]
        if hasattr(message, "tolist"):
            return self._message_to_nested_dict(message.tolist())
        if hasattr(message, "get_fields_and_field_types"):
            return {
                field: self._message_to_nested_dict(getattr(message, field))
                for field in message.get_fields_and_field_types().keys()
            }
        return str(message)

    def _write_json_artifact(self, filename: str, payload: Any) -> Path:
        return self._write_artifact(filename, json.dumps(payload, indent=2, sort_keys=True, default=str))

    @staticmethod
    def _write_json_path(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _write_geometry_json_path(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read_json_path(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"state": "unknown", "error": str(exc), "status_path": str(path)}
        return data if isinstance(data, dict) else {"state": "unknown", "status": data, "status_path": str(path)}

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _tail_file(path: Path, line_count: int) -> list[str]:
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:]
        except OSError:
            return []

    def _mission_deploy_status_from_record(self, record: dict[str, Any]) -> dict[str, Any]:
        process = record.get("process")
        returncode = process.poll() if process is not None else None
        data = self._read_json_path(Path(record["status_path"]))
        data.update(
            {
                "workflow_id": record["workflow_id"],
                "pid": record["pid"],
                "returncode": returncode,
                "process_running": returncode is None and self._pid_running(int(record["pid"])),
                "artifact_dir": record["artifact_dir"],
                "status_path": record["status_path"],
                "log_path": record["log_path"],
            }
        )
        return data

    def _call_service(self, client: Any, request: Any, timeout_sec: float) -> Any:
        if not client.wait_for_service(timeout_sec=timeout_sec):
            raise RuntimeError(f"service unavailable: {client.srv_name}")
        future = client.call_async(request)
        rclpy.spin_until_future_complete(self.node, future, timeout_sec=timeout_sec)
        if not future.done():
            raise TimeoutError(f"service call timed out: {client.srv_name}")
        return future.result()

    def _take_message(
        self,
        topic: str,
        message_type: Any,
        timeout_sec: float,
        *,
        required: bool = True,
        qos_profile: QoSProfile = qos_profile_sensor_data,
    ) -> Any:
        messages: list[Any] = []
        subscription = self.node.create_subscription(message_type, topic, lambda message: messages.append(message), qos_profile)
        deadline = time.monotonic() + timeout_sec
        try:
            while not messages and time.monotonic() < deadline and rclpy.ok():
                rclpy.spin_once(self.node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        finally:
            self.node.destroy_subscription(subscription)
        if messages:
            return messages[-1]
        if required:
            raise TimeoutError(f"timed out waiting for topic: {topic}")
        return None

    def _take_transient_local_message(
        self,
        topic: str,
        message_type: Any,
        timeout_sec: float,
        *,
        required: bool = True,
    ) -> Any:
        return self._take_message(
            topic,
            message_type,
            timeout_sec,
            required=required,
            qos_profile=QoSProfile(
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
            ),
        )

    def _message_to_plain_dict(self, message: Any) -> Any:
        if message is None:
            return None
        result: dict[str, Any] = {}
        field_names = list(getattr(message, "get_fields_and_field_types", lambda: {})().keys())
        if not field_names:
            field_names = [
                field_name[1:] if field_name.startswith("_") else field_name
                for field_name in getattr(message, "__slots__", [])
                if not field_name.startswith("_check_")
            ]
        for public_name in field_names:
            value = getattr(message, public_name)
            if isinstance(value, (bool, int, float, str)):
                result[public_name] = value
        return result

    def _resolve_iii_cli(self) -> str | None:
        path_cli = shutil.which("iii")
        if path_cli:
            return path_cli

        workspace_cli = self._workspace_root / "tools" / "III-Drone-CLI" / "bin" / "iii"
        if workspace_cli.exists():
            return str(workspace_cli)
        return None

    def _iii_command(self, *args: str) -> list[str]:
        if self._iii_cli_path is None:
            expected_cli = self._workspace_root / "tools" / "III-Drone-CLI" / "bin" / "iii"
            expected_setup = self._workspace_root / "setup" / "setup_dev.bash"
            raise FileNotFoundError(
                "III CLI executable was not found. Expected `iii` on PATH or "
                f"{expected_cli}. Source {expected_setup} or install tools/III-Drone-CLI."
            )
        return [self._iii_cli_path, *args]

    def _send_action(self, client: ActionClient, goal: Any, action_name: str, timeout_sec: Optional[float]) -> ToolResult:
        if not client.wait_for_server(timeout_sec=timeout_sec):
            return ToolResult(False, message=f"{action_name} action server unavailable")
        feedback_count = 0

        def on_feedback(_: Any) -> None:
            nonlocal feedback_count
            feedback_count += 1

        send_future = client.send_goal_async(goal, feedback_callback=on_feedback)
        rclpy.spin_until_future_complete(self.node, send_future, timeout_sec=timeout_sec)
        if not send_future.done():
            return ToolResult(False, message=f"timed out sending {action_name} action")
        goal_handle = send_future.result()
        if not goal_handle or not goal_handle.accepted:
            return ToolResult(False, message=f"{action_name} action rejected")
        result_future = goal_handle.get_result_async()
        while rclpy.ok() and not result_future.done():
            rclpy.spin_once(self.node, timeout_sec=0.1)
        if not result_future.done():
            return ToolResult(False, {"feedback_count": feedback_count}, f"{action_name} result unavailable before shutdown")
        return ToolResult(True, {"status": int(result_future.result().status), "feedback_count": feedback_count})

    def _run_tool_command(
        self,
        command: Sequence[str],
        *,
        timeout_sec: Optional[float] = None,
        daemon_timeout_sec: Optional[float] = None,
        check: bool = True,
    ) -> ToolResult:
        started = time.monotonic()
        env = os.environ.copy()
        env.setdefault("FASTDDS_BUILTIN_TRANSPORTS", "UDPv4")
        env.setdefault("GZ_IP", "127.0.0.1")
        if daemon_timeout_sec is not None:
            env["III_SYSTEM_DAEMON_REQUEST_TIMEOUT_SEC"] = str(float(daemon_timeout_sec))
            env["III_SYSTEM_DAEMON_CLIENT_TIMEOUT_SEC"] = str(float(daemon_timeout_sec) + 5.0)
        try:
            completed = subprocess.run(
                list(command),
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
                env=env,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            stdout = self._decode_timeout_output(exc.stdout)
            stderr = self._decode_timeout_output(exc.stderr)
            stderr = f"{stderr}\ncommand timed out after {timeout_sec}s".strip()
        success = returncode == 0 or not check
        return ToolResult(
            success,
            {
                "command": list(command),
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_sec": time.monotonic() - started,
            },
        )

    def _capture_gazebo_external_snapshot(
        self,
        *,
        filename: str,
        world: str,
        model_name: str,
        topic: str,
        pose: dict[str, float],
        width: int,
        height: int,
        horizontal_fov: float,
        update_rate: float,
        timeout_sec: float,
    ) -> Path:
        self._ensure_gazebo_external_camera(
            world=world,
            model_name=model_name,
            topic=topic,
            width=width,
            height=height,
            horizontal_fov=horizontal_fov,
            update_rate=update_rate,
            timeout_sec=timeout_sec,
        )
        self._set_gazebo_external_camera_pose(
            world=world,
            model_name=model_name,
            pose=pose,
            timeout_sec=timeout_sec,
        )

        try:
            import cv2
            import numpy as np
            from gz.msgs10.image_pb2 import Image
            from gz.transport13 import Node
        except ImportError as exc:
            raise RuntimeError("cv2, numpy, gz.msgs10, and gz.transport13 are required for Gazebo snapshots") from exc

        messages: list[Any] = []
        node = Node()
        node.subscribe(Image, topic, lambda message: messages.append(message))
        deadline = time.monotonic() + timeout_sec
        while not messages and time.monotonic() < deadline:
            time.sleep(0.05)
        if not messages:
            raise TimeoutError(f"timed out waiting for Gazebo external camera topic: {topic}")

        image = self._gazebo_image_message_to_array(messages[-1], np, cv2)
        path = self.artifact_dir / filename
        if not cv2.imwrite(str(path), image):
            raise RuntimeError(f"failed to write Gazebo image snapshot: {path}")
        return path

    def _ensure_gazebo_external_camera(
        self,
        *,
        world: str,
        model_name: str,
        topic: str,
        width: int,
        height: int,
        horizontal_fov: float,
        update_rate: float,
        timeout_sec: float,
    ) -> None:
        existing_topics = self._run_tool_command(["gz", "topic", "--list"], timeout_sec=timeout_sec, check=False)
        if topic in existing_topics.data.get("stdout", "").splitlines():
            return

        try:
            from gz.msgs10.boolean_pb2 import Boolean
            from gz.msgs10.entity_factory_pb2 import EntityFactory
            from gz.transport13 import Node
        except ImportError as exc:
            raise RuntimeError("gz.msgs10 and gz.transport13 are required for Gazebo external camera setup") from exc

        sdf = self._external_camera_sdf(
            model_name=model_name,
            topic=topic,
            width=width,
            height=height,
            horizontal_fov=horizontal_fov,
            update_rate=update_rate,
        )
        request = EntityFactory()
        request.sdf = sdf
        request.name = model_name
        request.allow_renaming = False

        ok, response = Node().request(
            f"/world/{world}/create",
            request,
            EntityFactory,
            Boolean,
            int(timeout_sec * 1000),
        )
        if not ok or not bool(response.data):
            topics_after_create = self._run_tool_command(["gz", "topic", "--list"], timeout_sec=timeout_sec, check=False)
            if topic not in topics_after_create.data.get("stdout", "").splitlines():
                raise RuntimeError(f"failed to create Gazebo external camera model: {model_name}")

    def _set_gazebo_external_camera_pose(
        self,
        *,
        world: str,
        model_name: str,
        pose: dict[str, float],
        timeout_sec: float,
    ) -> None:
        try:
            from gz.msgs10.boolean_pb2 import Boolean
            from gz.msgs10.pose_pb2 import Pose
            from gz.transport13 import Node
        except ImportError as exc:
            raise RuntimeError("gz.msgs10 and gz.transport13 are required for Gazebo external camera pose control") from exc

        request = Pose()
        request.name = model_name
        request.position.x = pose["x"]
        request.position.y = pose["y"]
        request.position.z = pose["z"]
        request.orientation.x = pose["qx"]
        request.orientation.y = pose["qy"]
        request.orientation.z = pose["qz"]
        request.orientation.w = pose["qw"]

        ok, response = Node().request(
            f"/world/{world}/set_pose",
            request,
            Pose,
            Boolean,
            int(timeout_sec * 1000),
        )
        if not ok or not bool(response.data):
            raise RuntimeError(f"failed to set Gazebo external camera pose for model: {model_name}")

    @staticmethod
    def _external_camera_sdf(
        *,
        model_name: str,
        topic: str,
        width: int,
        height: int,
        horizontal_fov: float,
        update_rate: float,
    ) -> str:
        return f"""<?xml version="1.0"?>
<sdf version="1.9">
  <model name="{model_name}">
    <static>true</static>
    <pose>0 -8 4 0 0 0</pose>
    <link name="camera_link">
      <sensor name="camera" type="camera">
        <always_on>true</always_on>
        <update_rate>{update_rate}</update_rate>
        <topic>{topic}</topic>
        <camera>
          <horizontal_fov>{horizontal_fov}</horizontal_fov>
          <image>
            <width>{width}</width>
            <height>{height}</height>
            <format>R8G8B8</format>
          </image>
          <clip>
            <near>0.1</near>
            <far>500</far>
          </clip>
        </camera>
      </sensor>
    </link>
  </model>
</sdf>"""

    @staticmethod
    def _external_camera_topic(kwargs: dict[str, Any], model_name: str) -> str:
        if "topic" in kwargs:
            return str(kwargs["topic"])
        safe_model_name = "".join(character if character.isalnum() or character in {"_", "-"} else "_" for character in model_name)
        return f"/agent/external_camera/{safe_model_name}/image"

    @staticmethod
    def _external_camera_pose_from_kwargs(kwargs: dict[str, Any]) -> dict[str, float]:
        x = float(kwargs.get("x", 0.0))
        y = float(kwargs.get("y", -8.0))
        z = float(kwargs.get("z", 4.0))
        if all(key in kwargs for key in ("qx", "qy", "qz", "qw")):
            return {
                "x": x,
                "y": y,
                "z": z,
                "qx": float(kwargs["qx"]),
                "qy": float(kwargs["qy"]),
                "qz": float(kwargs["qz"]),
                "qw": float(kwargs["qw"]),
            }
        target_x = float(kwargs.get("target_x", 0.0))
        target_y = float(kwargs.get("target_y", 0.0))
        target_z = float(kwargs.get("target_z", 1.5))
        dx = target_x - x
        dy = target_y - y
        dz = target_z - z
        yaw = math.atan2(dy, dx)
        horizontal_distance = math.hypot(dx, dy)
        pitch = math.atan2(-dz, horizontal_distance)
        qx, qy, qz, qw = DroneAgentTools._quaternion_from_euler(0.0, pitch, yaw)
        return {"x": x, "y": y, "z": z, "qx": qx, "qy": qy, "qz": qz, "qw": qw}

    @staticmethod
    def _quaternion_from_euler(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
        cy = math.cos(yaw * 0.5)
        sy = math.sin(yaw * 0.5)
        cp = math.cos(pitch * 0.5)
        sp = math.sin(pitch * 0.5)
        cr = math.cos(roll * 0.5)
        sr = math.sin(roll * 0.5)
        return (
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy,
        )

    def _lookup_world_drone_pose(self, *, timeout_sec: float) -> dict[str, float]:
        transform = self._lookup_world_drone_transform(timeout_sec=timeout_sec)
        pose = transform["pose"]
        return {"x": pose["x"], "y": pose["y"], "z": pose["z"], "yaw": pose["yaw"]}

    def _lookup_world_drone_transform(self, *, timeout_sec: float) -> dict[str, Any]:
        try:
            from rclpy.time import Time
            from tf2_ros import Buffer, TransformException, TransformListener
        except ImportError as exc:
            raise RuntimeError("tf2_ros is required for relative operation commands") from exc

        buffer = Buffer()
        TransformListener(buffer, self.node, spin_thread=False)
        deadline = time.monotonic() + timeout_sec
        last_error: Exception | None = None
        while time.monotonic() < deadline and rclpy.ok():
            try:
                transform = buffer.lookup_transform("world", "drone", Time())
                translation = transform.transform.translation
                rotation = transform.transform.rotation
                yaw = math.atan2(
                    2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
                    1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
                )
                stamp = transform.header.stamp
                return {
                    "pose": {"x": translation.x, "y": translation.y, "z": translation.z, "yaw": yaw},
                    "orientation_quaternion": {
                        "x": rotation.x,
                        "y": rotation.y,
                        "z": rotation.z,
                        "w": rotation.w,
                    },
                    "stamp": {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)},
                }
            except TransformException as exc:
                last_error = exc
                rclpy.spin_once(self.node, timeout_sec=0.1)
        raise TimeoutError(f"timed out waiting for world->drone transform: {last_error}")

    @staticmethod
    def _normalize_fixture_id(value: str) -> str:
        return normalize_fixture_id(value)

    @staticmethod
    def _normalize_angle(value: float) -> float:
        return math.atan2(math.sin(float(value)), math.cos(float(value)))

    @staticmethod
    def _gazebo_image_message_to_array(message: Any, np: Any, cv2: Any) -> Any:
        try:
            from gz.msgs10.image_pb2 import BGRA_INT8, BGR_INT8, L_INT8, RGBA_INT8, RGB_INT8
        except ImportError as exc:
            raise RuntimeError("gz.msgs10 is required for Gazebo image conversion") from exc

        channels_by_format = {
            RGB_INT8: 3,
            RGBA_INT8: 4,
            BGR_INT8: 3,
            BGRA_INT8: 4,
            L_INT8: 1,
        }
        channels = channels_by_format.get(message.pixel_format_type)
        if channels is None:
            raise RuntimeError(f"unsupported Gazebo image pixel format: {message.pixel_format_type}")
        raw = np.frombuffer(message.data, dtype=np.uint8)
        height = int(message.height)
        width = int(message.width)
        row_elements = int(message.step)
        if channels == 1:
            return raw.reshape((height, row_elements))[:, :width].copy()
        image = raw.reshape((height, row_elements // channels, channels))[:, :width, :]
        if message.pixel_format_type == RGB_INT8:
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        if message.pixel_format_type == RGBA_INT8:
            return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        if message.pixel_format_type == BGRA_INT8:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        return image.copy()

    def _capture_ros_image_snapshot(self, *, topic: str, filename: str, timeout_sec: float) -> Path:
        try:
            import cv2
            import numpy as np
            from sensor_msgs.msg import Image
        except ImportError as exc:
            raise RuntimeError("cv2, numpy, and sensor_msgs are required for image snapshots") from exc

        messages: list[Any] = []
        subscription = self.node.create_subscription(Image, topic, lambda message: messages.append(message), 10)
        deadline = time.monotonic() + timeout_sec
        try:
            while not messages and time.monotonic() < deadline:
                rclpy.spin_once(self.node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        finally:
            self.node.destroy_subscription(subscription)
        if not messages:
            raise TimeoutError(f"timed out waiting for image topic: {topic}")

        message = messages[-1]
        image = self._image_message_to_array(message, np, cv2)
        path = self.artifact_dir / filename
        if not cv2.imwrite(str(path), image):
            raise RuntimeError(f"failed to write image snapshot: {path}")
        return path

    @staticmethod
    def _image_message_to_array(message: Any, np: Any, cv2: Any) -> Any:
        encoding = message.encoding.lower()
        channels_by_encoding = {
            "rgb8": 3,
            "bgr8": 3,
            "rgba8": 4,
            "bgra8": 4,
            "mono8": 1,
            "8uc1": 1,
            "16uc1": 1,
        }
        channels = channels_by_encoding.get(encoding)
        if channels is None:
            raise RuntimeError(f"unsupported image encoding: {message.encoding}")
        dtype = np.uint16 if encoding == "16uc1" else np.uint8
        raw = np.frombuffer(message.data, dtype=dtype)
        row_elements = int(message.step) // np.dtype(dtype).itemsize
        height = int(message.height)
        width = int(message.width)
        if channels == 1:
            image = raw.reshape((height, row_elements))[:, :width]
            return image.copy()
        image = raw.reshape((height, row_elements // channels, channels))[:, :width, :]
        if encoding == "rgb8":
            return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        if encoding == "rgba8":
            return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        if encoding == "bgra8":
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        return image.copy()

    def _record_topic_info(self, **kwargs: Any) -> ToolResult:
        timeout_sec = float(kwargs.get("timeout_sec", 10.0))
        per_topic_timeout_sec = float(kwargs.get("per_topic_timeout_sec", 1.5))
        include_hidden = bool(kwargs.get("include_hidden", False))
        topic = kwargs.get("topic")
        if topic:
            topics = [str(topic)]
        else:
            list_result = self._run_tool_command(
                self._ros_topic_list_command(include_types=False, include_hidden=include_hidden),
                timeout_sec=min(timeout_sec, 5.0),
            )
            topics = [line.strip() for line in list_result.data["stdout"].splitlines() if line.strip()]
            limit = kwargs.get("limit")
            if limit is not None:
                topics = topics[: int(limit)]

        started = time.monotonic()
        sections: list[str] = []
        inspected = 0
        for topic_name in topics:
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                sections.append(f"## {topic_name}\nSKIPPED: total timeout expired\n")
                continue
            result = self._run_tool_command(
                ["ros2", "topic", "info", "-v", topic_name],
                timeout_sec=min(per_topic_timeout_sec, max(0.1, remaining)),
                check=False,
            )
            inspected += 1
            sections.append(
                f"## {topic_name}\n"
                f"returncode: {result.data['returncode']}\n"
                f"{result.data['stdout']}"
                f"{result.data['stderr']}"
            )

        artifact = self._write_artifact(
            kwargs.get("filename", f"ros_topic_info_{int(time.time() * 1000)}.txt"),
            "\n\n".join(sections),
        )
        return ToolResult(
            True,
            {
                "topic_count": len(topics),
                "inspected_count": inspected,
                "artifact_path": str(artifact),
                "preview": "\n\n".join(sections[:3]),
            },
        )

    def _record_topic_messages(self, **kwargs: Any) -> ToolResult:
        topic = kwargs["topic"]
        message_count = int(kwargs.get("message_count", kwargs.get("count", 0)))
        if message_count < 1:
            raise ValueError("message_count must be at least 1")
        timeout_sec = float(kwargs.get("timeout_sec", 10.0))
        started = time.monotonic()
        outputs: list[str] = []
        errors: list[str] = []
        returncodes: list[int] = []

        for _ in range(message_count):
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                break
            result = self._run_tool_command(
                ["ros2", "topic", "echo", "--once", topic],
                timeout_sec=max(0.1, remaining),
                check=False,
            )
            returncodes.append(int(result.data["returncode"]))
            if result.data["stdout"]:
                outputs.append(result.data["stdout"].rstrip())
            if result.data["stderr"]:
                errors.append(result.data["stderr"].rstrip())
            if result.data["returncode"] != 0:
                break

        recorded_count = sum(1 for output in outputs if output.strip())
        stdout = "\n".join(outputs) + ("\n" if outputs else "")
        stderr = "\n".join(errors) + ("\n" if errors else "")
        artifact = self._write_artifact(
            kwargs.get("filename", self._timestamped_artifact_name("ros_topic", topic, "yaml")),
            self._topic_artifact_content(
                command=["ros2", "topic", "echo", "--once", topic],
                topic=topic,
                stdout=stdout,
                stderr=stderr,
                returncode=returncodes[-1] if returncodes else -1,
                duration_sec=time.monotonic() - started,
            ),
        )
        success = recorded_count >= message_count
        return ToolResult(
            success,
            {
                "topic": topic,
                "requested_message_count": message_count,
                "recorded_message_count": recorded_count,
                "artifact_path": str(artifact),
                "returncodes": returncodes,
                "stderr": stderr,
            },
            "recorded requested message count" if success else "timed out before requested message count",
        )

    def _record_topic_for_seconds(self, **kwargs: Any) -> ToolResult:
        topic = kwargs["topic"]
        duration_sec = float(kwargs["duration_sec"])
        if duration_sec <= 0.0:
            raise ValueError("duration_sec must be positive")
        timeout_sec = float(kwargs.get("timeout_sec", duration_sec + 5.0))
        started = time.monotonic()
        deadline = started + duration_sec
        hard_deadline = started + timeout_sec
        outputs: list[str] = []
        errors: list[str] = []
        returncodes: list[int] = []

        while time.monotonic() < deadline and time.monotonic() < hard_deadline:
            remaining = min(deadline, hard_deadline) - time.monotonic()
            result = self._run_tool_command(
                ["ros2", "topic", "echo", "--once", topic],
                timeout_sec=max(0.1, remaining),
                check=False,
            )
            returncodes.append(int(result.data["returncode"]))
            if result.data["stdout"]:
                outputs.append(result.data["stdout"].rstrip())
            if result.data["stderr"]:
                errors.append(result.data["stderr"].rstrip())
            if result.data["returncode"] != 0:
                break

        stdout = "\n".join(outputs) + ("\n" if outputs else "")
        stderr = "\n".join(errors) + ("\n" if errors else "")
        message_count = sum(1 for output in outputs if output.strip())
        artifact = self._write_artifact(
            kwargs.get("filename", self._timestamped_artifact_name("ros_topic", topic, "yaml")),
            self._topic_artifact_content(
                command=["ros2", "topic", "echo", "--once", topic],
                topic=topic,
                stdout=stdout,
                stderr=stderr,
                returncode=returncodes[-1] if returncodes else -1,
                duration_sec=time.monotonic() - started,
            ),
        )
        return ToolResult(
            True,
            {
                "topic": topic,
                "duration_sec": duration_sec,
                "message_count": message_count,
                "artifact_path": str(artifact),
                "returncodes": returncodes,
                "stderr": stderr,
            },
            "recorded topic data",
        )

    def _start_rosbag_recording(self, **kwargs: Any) -> ToolResult:
        recording_id = str(kwargs.get("recording_id") or f"rosbag_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        recording_id = self._safe_name(recording_id)
        registry = self._load_rosbag_registry()
        existing = registry.get(recording_id)
        if existing and self._rosbag_process_running(existing):
            return ToolResult(False, existing, f"rosbag recording already running: {recording_id}")

        output_dir_arg = kwargs.get("output_dir")
        if output_dir_arg:
            output_dir = Path(str(output_dir_arg)).expanduser()
        else:
            output_dir = Path("/tmp/iii_drone/rosbags") / recording_id
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        if output_dir.exists():
            return ToolResult(False, {"recording_id": recording_id, "output_dir": str(output_dir)}, "rosbag output directory already exists")

        all_topics = bool(kwargs.get("all_topics", True))
        topics = [str(topic) for topic in kwargs.get("topics", [])]
        command = ["ros2", "bag", "record"]
        if all_topics:
            command.append("--all")
            if bool(kwargs.get("include_hidden_topics", True)):
                command.append("--include-hidden-topics")
        else:
            if not topics:
                raise ValueError("topics must be provided when all_topics is false")
            command.extend(topics)
        command.extend(["-o", str(output_dir)])

        log_dir = Path("/tmp/iii_drone/rosbag_recordings/logs") / recording_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "rosbag_stdout.log"
        stderr_path = log_dir / "rosbag_stderr.log"
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                text=False,
                start_new_session=True,
            )
        finally:
            stdout.close()
            stderr.close()

        record = {
            "recording_id": recording_id,
            "state": "running",
            "pid": process.pid,
            "pgid": process.pid,
            "command": command,
            "output_dir": str(output_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": None,
            "all_topics": all_topics,
            "topics": topics,
            "include_hidden_topics": bool(kwargs.get("include_hidden_topics", True)),
        }
        time.sleep(float(kwargs.get("startup_grace_sec", 0.5)))
        if process.poll() is not None:
            record["state"] = "exited"
            record["returncode"] = process.returncode
            registry[recording_id] = record
            self._save_rosbag_registry(registry)
            return ToolResult(False, self._rosbag_recording_view(record), f"rosbag recording exited immediately: {recording_id}")

        registry[recording_id] = record
        self._save_rosbag_registry(registry)
        return ToolResult(True, self._rosbag_recording_view(record), f"rosbag recording started: {recording_id}")

    def _stop_rosbag_recording(self, **kwargs: Any) -> ToolResult:
        registry = self._load_rosbag_registry()
        record = self._select_rosbag_recording(registry, kwargs.get("recording_id"))
        if record is None:
            return ToolResult(False, {"recordings": list(registry.values())}, "no rosbag recording found")

        recording_id = str(record["recording_id"])
        timeout_sec = float(kwargs.get("timeout_sec", 10.0))
        if self._rosbag_process_running(record):
            pgid = int(record.get("pgid") or record["pid"])
            try:
                os.killpg(pgid, signal.SIGINT)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline and self._rosbag_process_running(record):
                time.sleep(0.2)
            if self._rosbag_process_running(record):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and self._rosbag_process_running(record):
                    time.sleep(0.2)
            if self._rosbag_process_running(record):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        record["state"] = "stopped" if not self._rosbag_process_running(record) else "stopping_failed"
        record["stopped_at"] = datetime.now(timezone.utc).isoformat()
        registry[recording_id] = record
        self._save_rosbag_registry(registry)
        return ToolResult(record["state"] == "stopped", self._rosbag_recording_view(record), f"rosbag recording {record['state']}: {recording_id}")

    def _rosbag_recording_status(self, **kwargs: Any) -> ToolResult:
        registry = self._load_rosbag_registry()
        recording_id = kwargs.get("recording_id")
        if recording_id:
            record = registry.get(str(recording_id))
            if record is None:
                return ToolResult(False, {"recording_id": recording_id}, "rosbag recording not found")
            self._refresh_rosbag_record(record)
            registry[str(record["recording_id"])] = record
            self._save_rosbag_registry(registry)
            return ToolResult(True, self._rosbag_recording_view(record), f"rosbag recording status: {record['state']}")

        for record in registry.values():
            self._refresh_rosbag_record(record)
        self._save_rosbag_registry(registry)
        views = [self._rosbag_recording_view(record) for record in registry.values()]
        active = [record for record in views if record.get("state") == "running"]
        return ToolResult(True, {"active_recordings": active, "recordings": views}, f"{len(active)} active rosbag recording(s)")

    @staticmethod
    def _rosbag_registry_path() -> Path:
        path = Path("/tmp/iii_drone/rosbag_recordings")
        path.mkdir(parents=True, exist_ok=True)
        return path / "registry.json"

    def _load_rosbag_registry(self) -> dict[str, dict[str, Any]]:
        path = self._rosbag_registry_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = path.with_suffix(f".corrupt_{int(time.time())}.json")
            path.rename(backup)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def _save_rosbag_registry(self, registry: dict[str, dict[str, Any]]) -> None:
        path = self._rosbag_registry_path()
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def _select_rosbag_recording(
        self,
        registry: dict[str, dict[str, Any]],
        recording_id: Any,
    ) -> dict[str, Any] | None:
        if recording_id:
            return registry.get(str(recording_id))
        running = [record for record in registry.values() if self._rosbag_process_running(record)]
        if running:
            return sorted(running, key=lambda item: str(item.get("started_at", "")))[-1]
        if not registry:
            return None
        return sorted(registry.values(), key=lambda item: str(item.get("started_at", "")))[-1]

    def _refresh_rosbag_record(self, record: dict[str, Any]) -> None:
        if record.get("state") in {"running", "starting"} and not self._rosbag_process_running(record):
            record["state"] = "exited"
            record.setdefault("stopped_at", datetime.now(timezone.utc).isoformat())

    def _rosbag_process_running(self, record: dict[str, Any]) -> bool:
        pid = record.get("pid")
        if pid is None:
            return False
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return False
        stat_path = Path(f"/proc/{pid_int}/stat")
        try:
            state = stat_path.read_text(encoding="utf-8").split()[2]
            if state == "Z":
                return False
        except (FileNotFoundError, IndexError, OSError):
            return False
        try:
            os.kill(pid_int, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _rosbag_recording_view(self, record: dict[str, Any]) -> dict[str, Any]:
        view = dict(record)
        output_dir = Path(str(record.get("output_dir", "")))
        if output_dir.exists():
            files = [path for path in output_dir.rglob("*") if path.is_file()]
            view["file_count"] = len(files)
            view["size_bytes"] = sum(path.stat().st_size for path in files)
            view["files"] = [str(path) for path in sorted(files)[:50]]
        else:
            view["file_count"] = 0
            view["size_bytes"] = 0
            view["files"] = []
        return view

    def _target_from_dict(self, data: dict[str, Any]) -> Target:
        target = Target()
        target.target_type = int(data.get("target_type", Target.TARGET_TYPE_CABLE))
        target.target_id = int(data["target_id"])
        target.reference_frame_id = data.get("reference_frame_id", "world")
        transform = data.get("target_transform")
        if transform is not None:
            target.target_transform = self._transform_from_dict(transform)
        return target

    def _transform_from_dict(self, data: dict[str, Any]) -> Transform:
        transform = Transform()
        translation = data.get("translation", {})
        rotation = data.get("rotation", {})
        transform.translation.x = float(translation.get("x", 0.0))
        transform.translation.y = float(translation.get("y", 0.0))
        transform.translation.z = float(translation.get("z", 0.0))
        transform.rotation.x = float(rotation.get("x", 0.0))
        transform.rotation.y = float(rotation.get("y", 0.0))
        transform.rotation.z = float(rotation.get("z", 0.0))
        transform.rotation.w = float(rotation.get("w", 1.0))
        return transform

    def _write_artifact(self, filename: str, content: str | bytes) -> Path:
        path = self.artifact_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            if isinstance(content, bytes):
                temporary_path.write_bytes(content)
            else:
                temporary_path.write_text(content, encoding="utf-8")
            # Replace through the writable artifact directory instead of
            # truncating the existing inode. This remains usable when an
            # earlier root-run MCP process created the named artifact.
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def _plot_path_echo(self, echo_text: str, filename: str) -> Path:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import yaml
        except ImportError as exc:
            raise RuntimeError("matplotlib and PyYAML are required for path plot artifacts") from exc

        data = yaml.safe_load(echo_text)
        poses = data.get("poses", []) if isinstance(data, dict) else []
        x_values = [float(item["pose"]["position"]["x"]) for item in poses]
        y_values = [float(item["pose"]["position"]["y"]) for item in poses]
        z_values = [float(item["pose"]["position"]["z"]) for item in poses]
        if not x_values:
            raise RuntimeError("path topic snapshot did not contain poses")

        path = self.artifact_dir / filename
        fig = plt.figure(figsize=(8, 6))
        ax = fig.add_subplot(111, projection="3d")
        ax.plot(x_values, y_values, z_values, marker="o")
        ax.set_xlabel("x")
        ax.set_ylabel("y")
        ax.set_zlabel("z")
        ax.set_title("ROS Path")
        fig.tight_layout()
        fig.savefig(path)
        plt.close(fig)
        return path

    @staticmethod
    def _safe_name(value: str) -> str:
        return value.strip("/").replace("/", "_").replace(" ", "_") or "root"

    @staticmethod
    def _resolve_artifact_dir(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            fallback = Path("/tmp/iii_drone") / f"iii_drone_agent_artifacts_{os.getuid()}"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    @staticmethod
    def _decode_timeout_output(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode(errors="replace")
        return output

    @staticmethod
    def _ros_topic_list_command(*, include_types: bool, include_hidden: bool) -> list[str]:
        command = ["ros2", "topic", "list"]
        if include_hidden:
            command.append("-a")
        if include_types:
            command.append("-t")
        return command

    @classmethod
    def _timestamped_artifact_name(cls, prefix: str, topic: str, suffix: str) -> str:
        return f"{prefix}_{cls._safe_name(topic)}_{int(time.time() * 1000)}.{suffix}"

    @staticmethod
    def _count_ros_echo_messages(stdout: str) -> int:
        if not stdout.strip():
            return 0
        delimiter_count = sum(1 for line in stdout.splitlines() if line.strip() == "---")
        return delimiter_count or 1

    @staticmethod
    def _topic_artifact_content(
        *,
        command: Sequence[str],
        topic: str,
        stdout: str,
        stderr: str,
        returncode: int,
        duration_sec: float,
    ) -> str:
        metadata = {
            "command": list(command),
            "topic": topic,
            "returncode": returncode,
            "duration_sec": duration_sec,
        }
        return (
            "# iii-drone topic capture\n"
            f"{json.dumps(metadata, indent=2, sort_keys=True)}\n"
            "# stdout\n"
            f"{stdout}"
            "\n# stderr\n"
            f"{stderr}"
        )


def result_to_json(result: ToolResult) -> dict[str, Any]:
    return {"success": result.success, "data": result.data, "message": result.message}


def result_to_text(result: ToolResult) -> str:
    return json.dumps(result_to_json(result), indent=2, sort_keys=True)
