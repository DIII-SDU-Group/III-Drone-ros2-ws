from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, Callable

from iii_drone_mcp.agent_tools import DroneAgentTools, ToolResult
from iii_drone_mcp.fixture_ids import canonical_fixture_id, fixture_id_suggestions, normalize_fixture_id
from iii_drone_mcp.simulation_frames import (
    GAZEBO_TO_ROS_POSITION_YAW_RAD,
    rotate_gazebo_xy_delta_to_ros,
)


DEFAULT_GEOMETRY_PATH = Path(__file__).resolve().parents[1] / "config" / "hca_full_pylon_setup_geometry.json"
DEFAULT_POSITION_ID = "mid_corridor_taken_off_conductors_visible"
DEFAULT_INSPECTION_MISSION_START_POSITION_ID = "low_entry_side"
DEFAULT_GAZEBO_WORLD = "hca_full_pylon_setup"
DEFAULT_GAZEBO_DRONE_MODEL = "d4s_dc_drone_0"
GEOMETRY_POSITION_SECTIONS = ("mission_start_positions", "drone_positions", "demo_overview_positions")
DEMO_POS_OVER_CORRIDOR_ID = "pos_over_corridor"
DEMO_POS_PYLON_1_ID = "pos_pylon_1"
DEMO_POS_PYLON_2_ID = "pos_pylon_2"
DEFAULT_TARGET = {
    "position_id": "mid_corridor_taken_off_conductors_visible",
    "frame_id": "world",
    "x": -0.02959701605141163,
    "y": 0.0024893830996006727,
    "z": 1.35,
    "yaw": -1.236608440103837,
}


class WorkflowToolTimeout(RuntimeError):
    pass


class MissionDeployWorkflow:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.artifact_dir = Path(args.artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = Path(args.status_path)
        self.steps: list[dict[str, Any]] = []
        self.cancel_requested = False
        self._manual_input_mode_restore: dict[str, Any] | None = None
        self._mission_specification_overridden = False
        self._last_cable_aware_fallback_reason: dict[str, str] | None = None

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._request_cancel)
        signal.signal(signal.SIGINT, self._request_cancel)
        self._write_status("running", "workflow starting")
        tools = DroneAgentTools(artifact_dir=self.artifact_dir / "tools")
        try:
            target = self._target(tools)
            self._write_status("running", "target resolved", target=target)

            status = self._step("px4_status_before", lambda: self._px4_status_resilient(tools))
            telemetry = status.data if isinstance(status.data, dict) else {}
            self._configure_px4_automation_input(tools)
            self._override_mission_specification_if_requested(tools)
            requires_pylon_overview = self._requires_pylon_overview()

            overview = self._step(
                "check_stored_powerline_overview",
                lambda: tools.get_powerline_overview(
                    min_lines=self.args.min_powerline_lines,
                    timeout_sec=self.args.overview_query_timeout_sec,
                    filename=(
                        "stored_powerline_overview_before_refresh.json"
                        if self.args.force_update_overview
                        else "stored_powerline_overview_before.json"
                    ),
                ),
                required=False,
            )
            if self.args.force_update_overview and overview.success:
                self._append_step(
                    "reuse_stored_powerline_overview_for_refresh_staging_route",
                    "succeeded",
                    {
                        "reason": (
                            "force_update_overview requested; existing overview will still be used "
                            "only for safe cable-aware transit to the overview staging pose"
                        ),
                        "data": overview.data,
                    },
                )
            pylon_overview = self._check_stored_pylon_overview(tools, required=False) if requires_pylon_overview else None
            overview_refresh_required = (
                bool(self.args.force_update_overview)
                or not overview.success
                or (requires_pylon_overview and (pylon_overview is None or not pylon_overview.success))
            )
            if not overview_refresh_required:
                self._append_step(
                    "prepare_powerline_overview",
                    "skipped",
                    {"reason": "stored overview already available", "data": overview.data},
                )
                if requires_pylon_overview:
                    self._append_step(
                        "prepare_pylon_overview",
                        "skipped",
                        {"reason": "stored pylon overview already available", "data": pylon_overview.data if pylon_overview else None},
                    )
                self._prepare_custom_operation_slot(tools)
                self._takeoff_if_needed(tools, telemetry)
                self._move_to_mission_start_if_requested(tools)
                self._prepare_mission_executor_slot(tools)
                self._activate_mission(tools)
                self._write_status("succeeded", "mission deployment workflow complete; reused stored overview", target=target)
                return 0
            if overview_refresh_required:
                self._append_step(
                    "prepare_powerline_overview",
                    "running",
                    {"reason": self._overview_refresh_reason(overview, pylon_overview, requires_pylon_overview)},
                )

            self._prepare_custom_operation_slot(tools)
            self._takeoff_if_needed(tools, telemetry)
            self._activate_custom_operation_for_staging(tools)
            target = self._adjust_target_altitude_for_ground_estimate(tools, target, label="staging")
            self._fly_to_target(
                tools,
                target,
                operation_name="cable_aware_fly_to_position" if overview.success else "fly_to_position",
                start_step_name=(
                    "start_cable_aware_fly_to_staging_position"
                    if overview.success
                    else "start_fly_to_staging_position"
                ),
                wait_step_name=(
                    "wait_cable_aware_fly_to_staging_position"
                    if overview.success
                    else "wait_fly_to_staging_position"
                ),
                pose_step_name="wait_pose_at_staging_position",
                fallback_operation_name="fly_to_position" if overview.success else None,
                ignore_altitude=True,
            )

            self._step("start_pl_mapper", lambda: tools.pl_mapper("start", reset=True, timeout_sec=3.0))
            self._step(
                "wait_powerline_lines",
                lambda: self._wait_powerline_lines_with_retries(tools),
            )
            self._step(
                "store_powerline_overview",
                lambda: self._store_powerline_overview_with_retries(tools),
            )

            if requires_pylon_overview:
                self._store_demo_pylon_overviews(tools)

            self._move_to_mission_start_if_requested(tools)
            self._validate_pylon_overview_if_required(tools)
            self._prepare_mission_executor_slot(tools)
            self._activate_mission(tools)
            self._write_status("succeeded", "mission deployment workflow complete", target=target)
            return 0
        except KeyboardInterrupt:
            self._write_status("cancelled", "workflow interrupted")
            return 130
        except Exception as exc:
            self._write_status("failed", str(exc), error=repr(exc))
            return 1
        finally:
            self._restore_px4_manual_input_mode(tools)
            tools.close()

    def _request_cancel(self, *_: Any) -> None:
        self.cancel_requested = True
        self._write_status("cancelled", "workflow cancellation requested")
        raise KeyboardInterrupt

    def _takeoff_if_needed(self, tools: DroneAgentTools, telemetry: dict[str, Any]) -> None:
        if not bool(telemetry.get("in_air")):
            if not bool(telemetry.get("armed")):
                self._step(
                    "restore_hold_before_arm",
                    lambda: self._restore_hold_before_arm(tools),
                )
            if not bool(telemetry.get("armed")):
                self._step(
                    "verify_px4_safe_before_arm",
                    lambda: tools.px4_health(
                        timeout_sec=self.args.px4_timeout_sec,
                        stable_sec=self.args.arm_health_stable_sec,
                    ),
                )
                self._step(
                    "arm_if_needed",
                    lambda: self._px4_command_with_retries(
                        tools,
                        "arm",
                        timeout_sec=self.args.px4_timeout_sec,
                        postcondition_timeout_sec=self.args.px4_timeout_sec,
                        health_stable_sec=self.args.arm_health_stable_sec,
                    ),
                )
            self._step(
                "takeoff_if_needed",
                lambda: self._px4_command_with_retries(
                    tools,
                    "takeoff",
                    timeout_sec=self.args.px4_timeout_sec,
                    postcondition_timeout_sec=self.args.px4_timeout_sec,
                    min_altitude_m=0.0,
                ),
            )
            self._step("wait_takeoff_mode_exit", lambda: self._wait_takeoff_mode_exit(tools))
            self._step(
                "verify_px4_safe_after_takeoff",
                lambda: tools.px4_health(
                    timeout_sec=self.args.px4_timeout_sec,
                    stable_sec=self.args.post_takeoff_health_stable_sec,
                ),
            )
        else:
            self._append_step("takeoff_if_needed", "skipped", {"reason": "PX4 already reports in_air"})

    def _px4_command_with_retries(self, tools: DroneAgentTools, command: str, **kwargs: Any) -> ToolResult:
        attempts: list[dict[str, Any]] = []
        last_error: str | None = None
        for attempt in range(1, self.args.px4_command_attempts + 1):
            started = time.monotonic()
            try:
                result = tools.px4(command, **kwargs)
                attempts.append(
                    {
                        "attempt": attempt,
                        "duration_sec": time.monotonic() - started,
                        "success": bool(result.success),
                        "message": result.message,
                        "data": result.data,
                    }
                )
                if result.success:
                    data = result.data if isinstance(result.data, dict) else {}
                    data = {**data, "attempts": attempts}
                    return ToolResult(True, data, result.message)
                last_error = result.message
            except Exception as exc:
                last_error = repr(exc)
                safety = tools.px4_safety(timeout_sec=self.args.px4_timeout_sec)
                attempts.append(
                    {
                        "attempt": attempt,
                        "duration_sec": time.monotonic() - started,
                        "success": False,
                        "exception": repr(exc),
                        "safety": safety.data,
                        "safety_success": bool(safety.success),
                    }
                )
                if self._px4_command_postcondition_met(command, safety):
                    return ToolResult(
                        True,
                        {"attempts": attempts, "accepted_fallback": "ROS postcondition met after PX4 command exception"},
                        f"{command} command postcondition met after transient PX4 command exception",
                    )
                if not self._px4_retry_state_is_safe(safety):
                    return ToolResult(
                        False,
                        {"attempts": attempts, "last_error": last_error},
                        f"{command} command failed and PX4 is not safe for retry: {last_error}",
                    )
            if attempt < self.args.px4_command_attempts:
                time.sleep(self.args.px4_command_retry_delay_sec)
        return ToolResult(False, {"attempts": attempts, "last_error": last_error}, f"{command} command failed after retries")

    def _px4_status_resilient(self, tools: DroneAgentTools) -> ToolResult:
        try:
            status = tools.px4("status", timeout_sec=self.args.px4_timeout_sec)
            if status.success:
                return status
            direct_failure: dict[str, Any] = {
                "success": False,
                "message": status.message,
                "data": status.data,
            }
        except Exception as exc:
            direct_failure = {"success": False, "exception": repr(exc)}

        safety = tools.px4_safety(timeout_sec=self.args.px4_timeout_sec)
        if not safety.success:
            return ToolResult(
                False,
                {"direct_status": direct_failure, "safety": safety.data},
                f"PX4 status unavailable and safety fallback failed: {safety.message}",
            )

        data = safety.data if isinstance(safety.data, dict) else {}
        derived = data.get("derived") if isinstance(data.get("derived"), dict) else {}
        telemetry = {
            "armed": bool(derived.get("armed", False)),
            "in_air": bool(derived.get("in_air", False)),
            "flight_mode": derived.get("flight_mode"),
            "nav_state": derived.get("nav_state"),
            "failsafe": bool(derived.get("failsafe", False)),
            "unexpected_recovery": bool(derived.get("unexpected_recovery", False)),
            "source": "px4_safety_fallback",
            "direct_status": direct_failure,
            "safety": safety.data,
        }
        if telemetry["failsafe"] or telemetry["unexpected_recovery"]:
            return ToolResult(False, telemetry, "PX4 status fallback reports unsafe state")
        return ToolResult(True, telemetry, "PX4 status derived from safety fallback")

    def _px4_command_postcondition_met(self, command: str, safety: ToolResult) -> bool:
        if not safety.success:
            return False
        data = safety.data if isinstance(safety.data, dict) else {}
        derived = data.get("derived") if isinstance(data.get("derived"), dict) else {}
        if command == "arm":
            return bool(derived.get("armed", False))
        if command == "takeoff":
            return bool(derived.get("in_air", False))
        return False

    def _px4_retry_state_is_safe(self, safety: ToolResult) -> bool:
        if not safety.success:
            return False
        data = safety.data if isinstance(safety.data, dict) else {}
        derived = data.get("derived") if isinstance(data.get("derived"), dict) else {}
        return not bool(derived.get("failsafe", False)) and not bool(derived.get("unexpected_recovery", False))

    def _restore_hold_before_arm(self, tools: DroneAgentTools) -> ToolResult:
        try:
            return tools.px4("hold", timeout_sec=self.args.px4_timeout_sec)
        except Exception as exc:
            safety = tools.px4_safety(timeout_sec=self.args.px4_timeout_sec)
            if not safety.success:
                return ToolResult(
                    False,
                    {"hold_error": repr(exc), "safety": safety.data},
                    f"pre-arm Hold command failed and PX4 safety could not be verified: {safety.message}",
                )

            data = safety.data if isinstance(safety.data, dict) else {}
            derived = data.get("derived") if isinstance(data.get("derived"), dict) else {}
            ros = data.get("ros") if isinstance(data.get("ros"), dict) else {}
            vehicle_status = ros.get("vehicle_status") if isinstance(ros.get("vehicle_status"), dict) else {}
            nav_state = vehicle_status.get("nav_state", derived.get("nav_state"))
            armed = bool(derived.get("armed", False))
            failsafe = bool(derived.get("failsafe", False))
            external_mode = isinstance(nav_state, int) and 23 <= nav_state <= 30
            if not armed and not failsafe and not external_mode:
                return ToolResult(
                    True,
                    {
                        "hold_error": repr(exc),
                        "safety": safety.data,
                        "accepted_fallback": "vehicle is disarmed, not failsafe, and not in an external mode",
                    },
                    "pre-arm Hold command reported an error, but PX4 is safe to arm",
                )

            return ToolResult(
                False,
                {
                    "hold_error": repr(exc),
                    "safety": safety.data,
                    "armed": armed,
                    "failsafe": failsafe,
                    "nav_state": nav_state,
                    "external_mode": external_mode,
                },
                "pre-arm Hold command failed and PX4 is not in a safe fallback state",
            )

    def _configure_px4_automation_input(self, tools: DroneAgentTools) -> None:
        if not self.args.disable_manual_input_requirement:
            self._append_step(
                "configure_px4_automation_manual_input",
                "skipped",
                {"reason": "manual input requirement kept by request"},
            )
            return

        current = self._step(
            "read_px4_manual_input_mode_before_automation",
            lambda: tools.px4(
                "get_param",
                param_name="COM_RC_IN_MODE",
                timeout_sec=self.args.px4_timeout_sec,
            ),
        )
        data = current.data if isinstance(current.data, dict) else {}
        current_value = int(round(float(data.get("param_value", -1.0))))
        if current_value == 4:
            self._append_step(
                "disable_px4_manual_input_requirement_for_automation",
                "skipped",
                {
                    "reason": "COM_RC_IN_MODE already disables manual input requirement for automation",
                    "COM_RC_IN_MODE": data,
                },
            )
            return

        self._manual_input_mode_restore = {
            "param_name": "COM_RC_IN_MODE",
            "param_value": current_value,
            "param_type": int(data["param_type"]) if "param_type" in data else None,
        }
        self._step(
            "disable_px4_manual_input_requirement_for_automation",
            lambda: tools.px4(
                "set_param",
                param_name="COM_RC_IN_MODE",
                param_value=4,
                param_type=int(data["param_type"]) if "param_type" in data else None,
                timeout_sec=self.args.px4_timeout_sec,
            ),
        )

    def _restore_px4_manual_input_mode(self, tools: DroneAgentTools) -> None:
        restore = self._manual_input_mode_restore
        if not restore:
            return
        self._manual_input_mode_restore = None
        started = time.monotonic()
        try:
            safety = tools.px4_safety(timeout_sec=self.args.px4_timeout_sec)
            if safety.success:
                safety_data = safety.data if isinstance(safety.data, dict) else {}
                derived = safety_data.get("derived") if isinstance(safety_data.get("derived"), dict) else {}
                nav_state = derived.get("nav_state")
                armed = bool(derived.get("armed", False))
                in_external_mode = isinstance(nav_state, int) and 23 <= nav_state <= 30
                if armed and in_external_mode:
                    self._append_step(
                        "restore_px4_manual_input_mode_after_automation",
                        "deferred",
                        {
                            "success": True,
                            "message": (
                                "manual input mode restore deferred because PX4 is armed in an "
                                "external mode; restoring now can trigger a No manual control input failsafe"
                            ),
                            "restore": restore,
                            "safety": safety.data,
                            "duration_sec": time.monotonic() - started,
                        },
                    )
                    self._rewrite_status_with_current_steps()
                    return
            result = tools.px4(
                "set_param",
                param_name=str(restore["param_name"]),
                param_value=float(restore["param_value"]),
                param_type=restore.get("param_type"),
                timeout_sec=self.args.px4_timeout_sec,
            )
            self._append_step(
                "restore_px4_manual_input_mode_after_automation",
                "succeeded" if result.success else "failed",
                {
                    "success": bool(result.success),
                    "message": result.message,
                    "data": result.data,
                    "restore": restore,
                    "duration_sec": time.monotonic() - started,
                },
            )
        except Exception as exc:
            self._append_step(
                "restore_px4_manual_input_mode_after_automation",
                "failed",
                {
                    "success": False,
                    "message": str(exc),
                    "restore": restore,
                    "duration_sec": time.monotonic() - started,
                },
            )
        self._rewrite_status_with_current_steps()

    def _rewrite_status_with_current_steps(self) -> None:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8")) if self.status_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload.setdefault("workflow_id", self.args.workflow_id)
        payload.setdefault("state", "running")
        payload.setdefault("message", "workflow status updated")
        payload["updated_at"] = self._now_iso()
        payload["pid"] = self.args.pid
        payload["artifact_dir"] = str(self.artifact_dir)
        payload["status_path"] = str(self.status_path)
        payload["log_path"] = str(self.artifact_dir / "workflow.log")
        payload["steps"] = self.steps
        temporary = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        temporary.replace(self.status_path)

    def _activate_custom_operation_for_staging(self, tools: DroneAgentTools) -> None:
        self._ensure_custom_operation_mode_settable(tools, "staging")
        self._step(
            "activate_custom_operation",
            lambda: tools.activate_custom_operation(
                timeout_sec=self.args.custom_mode_timeout_sec,
                postcondition_timeout_sec=self.args.custom_mode_timeout_sec,
            ),
        )

    def _move_to_mission_start_if_requested(self, tools: DroneAgentTools) -> None:
        start = self._mission_start_target(tools)
        if start is None:
            self._append_step(
                "fly_to_mission_start_position",
                "skipped",
                {"reason": "no mission_start_position_id or mission_start pose supplied"},
            )
            return
        self._activate_custom_operation_for_mission_start(tools)
        start = self._adjust_target_altitude_for_ground_estimate(tools, start, label="mission_start")
        operation_name, start_step_name, wait_step_name, fallback_operation_name = self._mission_start_fly_operation()
        self._fly_to_target(
            tools,
            start,
            operation_name=operation_name,
            start_step_name=start_step_name,
            wait_step_name=wait_step_name,
            pose_step_name="wait_pose_at_mission_start_position",
            fallback_operation_name=fallback_operation_name,
            ignore_altitude=True,
        )

    def _mission_start_fly_operation(self) -> tuple[str, str, str, str | None]:
        mission_mode = self._normalize_mission_mode_key(str(getattr(self.args, "mission_mode", "") or ""))
        if mission_mode == "inspection_demo":
            return (
                "cable_aware_fly_to_position",
                "start_cable_aware_fly_to_mission_start_position",
                "wait_cable_aware_fly_to_mission_start_position",
                None,
            )
        return (
            "cable_aware_fly_to_position",
            "start_cable_aware_fly_to_mission_start_position",
            "wait_cable_aware_fly_to_mission_start_position",
            "fly_to_position",
        )

    def _activate_custom_operation_for_mission_start(self, tools: DroneAgentTools) -> None:
        self._ensure_custom_operation_mode_settable(tools, "mission_start")
        self._step(
            "activate_custom_operation_for_mission_start",
            lambda: tools.activate_custom_operation(
                timeout_sec=self.args.custom_mode_timeout_sec,
                postcondition_timeout_sec=self.args.custom_mode_timeout_sec,
            ),
        )

    def _prepare_custom_operation_slot(self, tools: DroneAgentTools) -> None:
        if self._mission_specification_overridden:
            self._append_step(
                "restart_custom_operation_after_mission_specification_override",
                "skipped",
                {
                    "reason": (
                        "CustomOperation is standalone and remains registered across mission specification overrides; "
                        "restarting it can leave stale PX4 arming-check components"
                    )
                },
            )
            self._mission_specification_overridden = False
        self._step(
            "ensure_custom_operation_started_for_external_mode_replies",
            lambda: tools.system("start", entity_id="custom_operation", include_dependencies=False, timeout_sec=180.0),
        )

    def _ensure_custom_operation_mode_settable(self, tools: DroneAgentTools, label: str) -> None:
        step_name = f"ensure_custom_operation_mode_settable_{label}"

        def check_and_recover() -> ToolResult:
            deadline = time.monotonic() + max(10.0, float(self.args.custom_mode_timeout_sec))
            attempts: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                status = tools.operation_status(timeout_sec=3.0)
                mode_id = self._custom_operation_mode_id_from_status(status)
                safety = tools.px4_safety(timeout_sec=min(5.0, float(self.args.px4_timeout_sec)))
                mask = self._can_set_nav_states_mask(safety)
                active_nav_state = self._active_nav_state(safety)
                settable = mode_id is not None and mask is not None and bool(mask & (1 << mode_id))
                active = mode_id is not None and active_nav_state == mode_id
                attempts.append(
                    {
                        "mode_id": mode_id,
                        "can_set_nav_states_mask": mask,
                        "active_nav_state": active_nav_state,
                        "settable": settable,
                        "active": active,
                        "operation_status_success": bool(status.success),
                        "px4_safety_success": bool(safety.success),
                    }
                )
                if active or settable:
                    return ToolResult(
                        True,
                        {
                            "attempts": attempts,
                            "mode_id": mode_id,
                            "can_set_nav_states_mask": mask,
                            "active_nav_state": active_nav_state,
                        },
                        "CustomOperation mode is settable",
                    )

                time.sleep(1.0)

            return ToolResult(
                False,
                {"attempts": attempts},
                "CustomOperation mode did not become settable in PX4 before activation",
            )

        self._step(step_name, check_and_recover)

    @staticmethod
    def _custom_operation_mode_id_from_status(status: ToolResult) -> int | None:
        if not status.success or not isinstance(status.data, dict):
            return None
        status_data = status.data.get("status") if isinstance(status.data.get("status"), dict) else {}
        stdout = str(status_data.get("stdout") or "")
        match = re.search(r"data:\s*'([^']+)'", stdout)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
            return int(payload["mode_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _can_set_nav_states_mask(safety: ToolResult) -> int | None:
        if not safety.success or not isinstance(safety.data, dict):
            return None
        ros_data = safety.data.get("ros") if isinstance(safety.data.get("ros"), dict) else {}
        vehicle_status = ros_data.get("vehicle_status") if isinstance(ros_data.get("vehicle_status"), dict) else {}
        value = vehicle_status.get("can_set_nav_states_mask")
        return int(value) if value is not None else None

    @staticmethod
    def _active_nav_state(safety: ToolResult) -> int | None:
        if not safety.success or not isinstance(safety.data, dict):
            return None
        ros_data = safety.data.get("ros") if isinstance(safety.data.get("ros"), dict) else {}
        vehicle_status = ros_data.get("vehicle_status") if isinstance(ros_data.get("vehicle_status"), dict) else {}
        value = vehicle_status.get("nav_state")
        return int(value) if value is not None else None

    def _override_mission_specification_if_requested(self, tools: DroneAgentTools) -> None:
        requested_file = str(getattr(self.args, "mission_specification_file", "") or "")
        use_default = bool(getattr(self.args, "use_default_mission_specification", False))
        explicit_override = bool(requested_file) or use_default
        if not explicit_override and self.args.skip_mission_activation:
            self._append_step(
                "override_mission_specification",
                "skipped",
                {"reason": "skip_mission_activation requested and no explicit mission specification override was supplied"},
            )
            return
        if not explicit_override:
            mission_mode = self._normalize_mission_mode_key(str(getattr(self.args, "mission_mode", "") or ""))
            if mission_mode == "reach_cable":
                requested_file = "mission_specification_reach_charge_leave.yaml"
            elif mission_mode == "inspection_demo":
                requested_file = "mission_specification.yaml"
        if not requested_file and not use_default:
            self._append_step(
                "override_mission_specification",
                "skipped",
                {"reason": f"no mission specification override configured for mission_mode={self.args.mission_mode!r}"},
            )
            return

        self._step(
            "ensure_mission_executor_started_for_mission_specification_override",
            lambda: tools.system("start", entity_id="mission_executor", include_dependencies=False, timeout_sec=180.0),
        )
        if not use_default:
            status = self._step(
                "check_active_mission_specification_before_override",
                lambda: tools.mission_status(timeout_sec=3.0),
                required=False,
            )
            if self._active_mission_specification_matches(status, requested_file):
                self._append_step(
                    "override_mission_specification",
                    "skipped",
                    {
                        "reason": (
                            "requested mission specification already active; skipping service call to preserve "
                            "PX4 external-mode registrations"
                        ),
                        "requested_file": requested_file,
                        "mission_status": status.data,
                    },
                )
                return
        self._step(
            "override_mission_specification",
            lambda: tools.override_mission_specification(
                mission_specification_file=requested_file,
                use_default=use_default,
                timeout_sec=10.0,
            ),
        )
        self._mission_specification_overridden = True

    @staticmethod
    def _active_mission_specification_matches(status: ToolResult, requested_file: str) -> bool:
        if not status.success or not isinstance(status.data, dict):
            return False
        active = str(status.data.get("active_mission_specification") or "")
        requested = str(requested_file or "")
        if not active or not requested:
            return False
        if active == requested:
            return True
        return Path(active).name == Path(requested).name

    @staticmethod
    def _normalize_mission_mode_key(value: str) -> str:
        normalized = re.sub(r"[^0-9A-Za-z]+", "_", value.strip()).strip("_").lower()
        return re.sub(r"_+", "_", normalized)

    def _prepare_mission_executor_slot(self, tools: DroneAgentTools) -> None:
        self._step(
            "ensure_mission_executor_started_for_mission_activation",
            lambda: tools.system("start", entity_id="mission_executor", include_dependencies=False, timeout_sec=180.0),
        )

    def _activate_mission(self, tools: DroneAgentTools) -> None:
        if self.args.skip_mission_activation:
            self._append_step("activate_mission_mode", "skipped", {"reason": "skip_mission_activation requested"})
            return
        self._step(
            "activate_mission_mode",
            lambda: tools.activate_mission_mode(
                mode_key=self.args.mission_mode,
                timeout_sec=5.0,
                postcondition_timeout_sec=10.0,
            ),
        )

    def _requires_pylon_overview(self) -> bool:
        return bool(self.args.require_pylon_overview) or self._normalize_mission_mode_key(self.args.mission_mode) == "inspection_demo"

    def _overview_refresh_reason(
        self,
        powerline_overview: ToolResult,
        pylon_overview: ToolResult | None,
        requires_pylon_overview: bool,
    ) -> str:
        reasons: list[str] = []
        if self.args.force_update_overview:
            reasons.append("force_update_overview requested")
        if not powerline_overview.success:
            reasons.append("stored powerline overview unavailable or incomplete")
        if requires_pylon_overview and (pylon_overview is None or not pylon_overview.success):
            reasons.append("stored pylon overview unavailable or incomplete")
        if not reasons:
            reasons.append("overview refresh requested")
        suffix = "; refreshing powerline and pylon overviews together" if requires_pylon_overview else "; refreshing powerline overview"
        return ", ".join(reasons) + suffix

    def _check_stored_pylon_overview(self, tools: DroneAgentTools, *, required: bool) -> ToolResult:
        if not self._requires_pylon_overview():
            self._append_step(
                "check_stored_pylon_overview",
                "skipped",
                {"reason": f"mission_mode={self.args.mission_mode!r} does not require pylon overview"},
            )
            return ToolResult(True, {"required": False}, "pylon overview not required")
        return self._step(
            "check_stored_pylon_overview",
            lambda: tools.get_pylon_overview(
                min_pylons=self.args.min_pylons,
                timeout_sec=self.args.pylon_overview_timeout_sec,
                filename="stored_pylon_overview_before_mission.json",
            ),
            required=required,
        )

    def _validate_pylon_overview_if_required(self, tools: DroneAgentTools) -> None:
        self._check_stored_pylon_overview(tools, required=True)

    def _store_demo_pylon_overviews(self, tools: DroneAgentTools) -> None:
        self._append_step(
            "prepare_pylon_overview",
            "running",
            {
                "reason": "inspection demo requires pylon overview refresh after powerline overview refresh",
                "sequence": [
                    self.args.demo_pos_over_corridor_id,
                    self.args.demo_pos_pylon_1_id,
                    self.args.demo_pos_pylon_2_id,
                    self.args.demo_pos_over_corridor_id,
                ],
            },
        )
        over_corridor = self._required_geometry_target(self.args.demo_pos_over_corridor_id, tools)
        pylon_1 = self._required_geometry_target(self.args.demo_pos_pylon_1_id, tools)
        pylon_2 = self._required_geometry_target(self.args.demo_pos_pylon_2_id, tools)

        self._fly_to_target(
            tools,
            over_corridor,
            operation_name="cable_aware_fly_to_position",
            start_step_name="start_cable_aware_fly_to_demo_over_corridor",
            wait_step_name="wait_cable_aware_fly_to_demo_over_corridor",
            pose_step_name="wait_pose_at_demo_over_corridor",
            fallback_operation_name="fly_to_position",
            ignore_altitude=True,
        )
        self._fly_to_target(
            tools,
            pylon_1,
            operation_name="fly_to_position",
            start_step_name="start_fly_to_demo_pylon_1",
            wait_step_name="wait_fly_to_demo_pylon_1",
            pose_step_name="wait_pose_at_demo_pylon_1",
            verify_pose=False,
            ignore_altitude=True,
        )
        self._fly_to_target(
            tools,
            pylon_2,
            operation_name="fly_to_position",
            start_step_name="start_fly_to_demo_pylon_2",
            wait_step_name="wait_fly_to_demo_pylon_2",
            pose_step_name="wait_pose_at_demo_pylon_2",
            verify_pose=False,
            ignore_altitude=True,
        )
        self._fly_to_target(
            tools,
            over_corridor,
            operation_name="fly_to_position",
            start_step_name="start_fly_to_demo_over_corridor_after_pylons",
            wait_step_name="wait_fly_to_demo_over_corridor_after_pylons",
            pose_step_name="wait_pose_at_demo_over_corridor_after_pylons",
            ignore_altitude=True,
        )
        self._step("replace_demo_pylon_overview", lambda: self._replace_demo_pylon_overview(tools, pylon_1, pylon_2))
        self._append_step(
            "prepare_pylon_overview",
            "succeeded",
            {"message": "stored demo pylon overview from fixture positions"},
        )

    def _replace_demo_pylon_overview(
        self,
        tools: DroneAgentTools,
        pylon_1: dict[str, Any],
        pylon_2: dict[str, Any],
    ) -> ToolResult:
        results: list[dict[str, Any]] = []
        clear = tools.clear_pylon_overview(timeout_sec=3.0)
        results.append({"phase": "clear", "success": clear.success, "message": clear.message, "data": clear.data})
        if not clear.success:
            return ToolResult(False, {"attempts": results}, "failed to clear existing pylon overview")

        for pylon_id, target in ((1, pylon_1), (2, pylon_2)):
            store = tools.store_pylon_overview(
                pylon_id=pylon_id,
                x=target["x"],
                y=target["y"],
                frame_id=target.get("frame_id", "world"),
                timeout_sec=3.0,
                filename=f"stored_pylon_{pylon_id}_overview.json",
            )
            results.append(
                {
                    "phase": f"store_pylon_{pylon_id}",
                    "success": store.success,
                    "message": store.message,
                    "data": store.data,
                    "target": target,
                }
            )
            if not store.success:
                cleanup = tools.clear_pylon_overview(timeout_sec=3.0)
                results.append(
                    {
                        "phase": "cleanup_clear_after_failed_store",
                        "success": cleanup.success,
                        "message": cleanup.message,
                        "data": cleanup.data,
                    }
                )
                return ToolResult(False, {"attempts": results}, f"failed to store pylon {pylon_id}; cleared pylon overview")

        verify = tools.get_pylon_overview(
            min_pylons=self.args.min_pylons,
            timeout_sec=self.args.pylon_overview_timeout_sec,
            filename="stored_pylon_overview_after_refresh.json",
        )
        results.append({"phase": "verify", "success": verify.success, "message": verify.message, "data": verify.data})
        return ToolResult(
            bool(verify.success),
            {"attempts": results},
            "stored complete demo pylon overview" if verify.success else "stored pylon overview did not verify",
        )

    def _required_geometry_target(self, position_id: str, tools: DroneAgentTools) -> dict[str, Any]:
        target = self._target_from_geometry(position_id, tools=tools)
        if target is None:
            raise RuntimeError(f"required geometry position not found or missing pose: {position_id}")
        missing = {"x", "y", "z", "yaw"} - set(target.keys())
        if missing:
            raise RuntimeError(f"required geometry position {position_id!r} is missing fields: {sorted(missing)}")
        return target

    def _step(self, name: str, callback: Callable[[], ToolResult], *, required: bool = True) -> ToolResult:
        if self.cancel_requested:
            raise KeyboardInterrupt
        self._append_step(name, "running", {})
        self._write_status("running", f"{name} running")
        started = time.monotonic()
        result = callback()
        payload = {
            "success": bool(result.success),
            "message": result.message,
            "data": result.data,
            "duration_sec": time.monotonic() - started,
        }
        final_state = "succeeded" if result.success else ("failed" if required else "unavailable")
        self._append_step(name, final_state, payload)
        if required and not result.success:
            raise RuntimeError(f"{name} failed: {result.message}")
        self._write_status("running", f"{name} complete")
        return result

    def _append_step(self, name: str, state: str, payload: dict[str, Any]) -> None:
        if self.steps and self.steps[-1]["name"] == name and self.steps[-1]["state"] == "running":
            self.steps[-1].update({"state": state, "finished_at": self._now_iso(), **payload})
            return
        record = {"name": name, "state": state, "started_at": self._now_iso()}
        if state not in {"running"}:
            record["finished_at"] = record["started_at"]
        record.update(payload)
        self.steps.append(record)

    def _write_status(self, state: str, message: str, **extra: Any) -> None:
        payload = {
            "workflow_id": self.args.workflow_id,
            "state": state,
            "message": message,
            "updated_at": self._now_iso(),
            "pid": self.args.pid,
            "artifact_dir": str(self.artifact_dir),
            "status_path": str(self.status_path),
            "log_path": str(self.artifact_dir / "workflow.log"),
            "steps": self.steps,
        }
        payload.update(extra)
        temporary = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
        temporary.replace(self.status_path)
        print(json.dumps({"state": state, "message": message, **extra}, default=str), flush=True)

    def _target(self, tools: DroneAgentTools) -> dict[str, Any]:
        target = self._target_from_geometry(self.args.position_id, tools=tools) or dict(DEFAULT_TARGET)
        target["position_id"] = self.args.position_id or target.get("position_id") or DEFAULT_TARGET["position_id"]
        target["frame_id"] = self.args.frame_id or target.get("frame_id") or "world"
        for key in ("x", "y", "z", "yaw"):
            value = getattr(self.args, key)
            if value is not None:
                target[key] = float(value)
        if (
            not isinstance(target.get("gazebo_ground_truth_pose"), dict)
            and target["z"] < self.args.minimum_staging_z
        ):
            target["z"] = self.args.minimum_staging_z
            target["z_adjusted_to_minimum"] = True
        return target

    def _mission_start_target(self, tools: DroneAgentTools) -> dict[str, Any] | None:
        has_direct_pose = any(
            value is not None
            for value in (
                self.args.mission_start_x,
                self.args.mission_start_y,
                self.args.mission_start_z,
                self.args.mission_start_yaw,
            )
        )
        mission_start_position_id = self.args.mission_start_position_id
        mission_mode = self._normalize_mission_mode_key(
            str(getattr(self.args, "mission_mode", "") or "")
        )
        if not mission_start_position_id and not has_direct_pose and mission_mode == "inspection_demo":
            mission_start_position_id = DEFAULT_INSPECTION_MISSION_START_POSITION_ID
        if not mission_start_position_id and not has_direct_pose:
            return None
        target = (
            self._target_from_geometry(mission_start_position_id, tools=tools)
            if mission_start_position_id
            else None
        )
        if target is None:
            if not has_direct_pose:
                raise RuntimeError(self._missing_geometry_position_message("mission start position", mission_start_position_id))
            target = {
                "position_id": mission_start_position_id or "direct_mission_start_pose",
                "frame_id": self.args.mission_start_frame_id or "world",
                "x": 0.0,
                "y": 0.0,
                "z": self.args.takeoff_altitude,
                "yaw": 0.0,
            }
        target["position_id"] = mission_start_position_id or target.get("position_id") or "direct_mission_start_pose"
        target["frame_id"] = self.args.mission_start_frame_id or target.get("frame_id") or "world"
        overrides = {
            "x": self.args.mission_start_x,
            "y": self.args.mission_start_y,
            "z": self.args.mission_start_z,
            "yaw": self.args.mission_start_yaw,
        }
        for key, value in overrides.items():
            if value is not None:
                target[key] = float(value)
        missing = {"x", "y", "z", "yaw"} - set(target.keys())
        if missing:
            raise RuntimeError(f"mission start target is missing fields: {sorted(missing)}")
        self._write_status("running", "mission start target resolved", mission_start=target)
        return target

    def _fly_to_target(
        self,
        tools: DroneAgentTools,
        target: dict[str, Any],
        *,
        operation_name: str,
        start_step_name: str,
        wait_step_name: str,
        pose_step_name: str,
        ignore_altitude: bool,
        fallback_operation_name: str | None = None,
        verify_pose: bool = True,
    ) -> None:
        if operation_name == "cable_aware_fly_to_position" and fallback_operation_name is not None:
            if self._try_cable_aware_fly_to_target_with_retries(
                tools,
                target,
                start_step_name=start_step_name,
                wait_step_name=wait_step_name,
                ignore_altitude=ignore_altitude,
            ):
                self._verify_or_skip_pose(tools, target, pose_step_name, verify_pose)
                return
            self._run_fly_to_target_fallback(
                tools,
                target,
                failed_operation_name=operation_name,
                fallback_operation_name=fallback_operation_name,
                wait_step_name=wait_step_name,
                pose_step_name=pose_step_name,
                verify_pose=verify_pose,
                ignore_altitude=ignore_altitude,
                failed_result=self._last_cable_aware_fallback_reason
                or {"reason": "cable-aware fly-to-position failed before goal acceptance/completion"},
            )
            return

        fly = self._step(
            start_step_name,
            lambda: tools.start_operation(
                operation_name,
                frame_id=target["frame_id"],
                x=target["x"],
                y=target["y"],
                z=target["z"],
                yaw=target["yaw"],
                ignore_altitude=ignore_altitude,
                send_timeout_sec=self.args.fly_send_timeout_sec,
                clear_queue=False,
                cancel_existing=True,
            ),
        )
        goal_id = (fly.data or {}).get("goal_id")
        if not goal_id:
            raise RuntimeError(f"{operation_name} did not return a goal_id")
        wait_result = self._step(
            wait_step_name,
            lambda: self._wait_fly_goal_resilient(tools, goal_id, target),
            required=fallback_operation_name is None,
        )
        if not wait_result.success:
            if fallback_operation_name is None:
                raise RuntimeError(f"{wait_step_name} failed: {wait_result.message}")
            self._run_fly_to_target_fallback(
                tools,
                target,
                failed_operation_name=operation_name,
                fallback_operation_name=fallback_operation_name,
                wait_step_name=wait_step_name,
                pose_step_name=pose_step_name,
                verify_pose=verify_pose,
                ignore_altitude=ignore_altitude,
                failed_result=wait_result.data,
            )
            return
        self._verify_or_skip_pose(tools, target, pose_step_name, verify_pose)

    def _run_fly_to_target_fallback(
        self,
        tools: DroneAgentTools,
        target: dict[str, Any],
        *,
        failed_operation_name: str,
        fallback_operation_name: str,
        wait_step_name: str,
        pose_step_name: str,
        verify_pose: bool,
        ignore_altitude: bool,
        failed_result: Any,
    ) -> None:
        self._step(
            f"wait_idle_before_{fallback_operation_name}_fallback",
            lambda: self._wait_maneuver_idle(
                tools,
                timeout_sec=self.args.fly_fallback_idle_timeout_sec,
                stable_sec=self.args.fly_fallback_idle_stable_sec,
            ),
        )
        self._append_step(
            f"{wait_step_name}_fallback",
            "succeeded",
            {
                "reason": (
                    f"{failed_operation_name} failed during scenario setup; falling back to "
                    f"{fallback_operation_name} so intentionally close or stale-overview "
                    "simulation fixtures can still be reached"
                ),
                "failed_operation": failed_operation_name,
                "fallback_operation": fallback_operation_name,
                "failed_result": failed_result,
            },
        )
        fallback_status = self._step(
            f"px4_status_before_{fallback_operation_name}_fallback",
            lambda: self._px4_status_resilient(tools),
        )
        fallback_telemetry = fallback_status.data if isinstance(fallback_status.data, dict) else {}
        self._takeoff_if_needed(tools, fallback_telemetry)
        self._step(
            f"reactivate_custom_operation_before_{fallback_operation_name}_fallback",
            lambda: tools.activate_custom_operation(
                timeout_sec=self.args.custom_mode_timeout_sec,
                postcondition_timeout_sec=self.args.custom_mode_timeout_sec,
            ),
        )
        self._fly_to_target(
            tools,
            target,
            operation_name=fallback_operation_name,
            start_step_name=f"start_{fallback_operation_name}_fallback",
            wait_step_name=f"wait_{fallback_operation_name}_fallback",
            pose_step_name=pose_step_name,
            fallback_operation_name=None,
            verify_pose=verify_pose,
            ignore_altitude=ignore_altitude,
        )

    def _verify_or_skip_pose(
        self,
        tools: DroneAgentTools,
        target: dict[str, Any],
        pose_step_name: str,
        verify_pose: bool,
    ) -> None:
        if verify_pose:
            self._step(pose_step_name, lambda: self._wait_pose_at_target(tools, target))
            return
        latest_pose = None
        try:
            latest_pose = tools._lookup_world_drone_pose(timeout_sec=1.0)
        except Exception as exc:
            latest_pose = {"error": str(exc)}
        self._append_step(
            pose_step_name,
            "skipped",
            {
                "reason": "pose verification disabled for pylon overview capture; completed fly_to_position result is sufficient and pylon storage uses mapped fixture XY",
                "target": target,
                "pose": latest_pose,
            },
        )

    def _try_cable_aware_fly_to_target_with_retries(
        self,
        tools: DroneAgentTools,
        target: dict[str, Any],
        *,
        start_step_name: str,
        wait_step_name: str,
        ignore_altitude: bool,
    ) -> bool:
        attempts: list[dict[str, Any]] = []
        max_attempts = max(1, int(self.args.cable_aware_fly_attempts))

        for attempt_index in range(1, max_attempts + 1):
            if self.cancel_requested:
                raise KeyboardInterrupt
            started = time.monotonic()
            validation = self._step(
                f"{start_step_name}_validate_powerline_overview_attempt_{attempt_index}",
                lambda: self._tool_call_with_deadline(
                    lambda: tools.validate_stored_powerline_overview_against_sim_geometry(
                        max_line_error_m=1.5,
                        timeout_sec=self.args.overview_query_timeout_sec,
                    ),
                    timeout_sec=max(2.0, self.args.overview_query_timeout_sec + 3.0),
                    timeout_message="stored powerline overview validation timed out before CAFTP start",
                ),
                required=False,
            )
            if not validation.success:
                attempts.append(
                    {
                        "attempt": attempt_index,
                        "validation_success": False,
                        "validation_message": validation.message,
                        "validation_data": validation.data,
                        "duration_sec": time.monotonic() - started,
                    }
                )
                break
            target_clearance = self._step(
                f"{start_step_name}_validate_target_clearance_attempt_{attempt_index}",
                lambda: self._tool_call_with_deadline(
                    lambda: tools.validate_cable_aware_target_clearance(
                        frame_id=target["frame_id"],
                        x=target["x"],
                        y=target["y"],
                        z=target["z"],
                        timeout_sec=self.args.overview_query_timeout_sec,
                    ),
                    timeout_sec=max(2.0, self.args.overview_query_timeout_sec + 3.0),
                    timeout_message="stored powerline target clearance validation timed out before CAFTP start",
                ),
                required=False,
            )
            if not target_clearance.success:
                attempts.append(
                    {
                        "attempt": attempt_index,
                        "validation_success": True,
                        "validation_message": validation.message,
                        "validation_data": validation.data,
                        "target_clearance_success": False,
                        "target_clearance_message": target_clearance.message,
                        "target_clearance_data": target_clearance.data,
                        "duration_sec": time.monotonic() - started,
                    }
                )
                break
            fly = tools.start_operation(
                "cable_aware_fly_to_position",
                frame_id=target["frame_id"],
                x=target["x"],
                y=target["y"],
                z=target["z"],
                yaw=target["yaw"],
                ignore_altitude=ignore_altitude,
                send_timeout_sec=self.args.fly_send_timeout_sec,
                clear_queue=False,
                cancel_existing=True,
                validate_sim_powerline_overview=False,
            )
            attempt_record: dict[str, Any] = {
                "attempt": attempt_index,
                "validation_success": True,
                "validation_message": validation.message,
                "validation_data": validation.data,
                "target_clearance_success": True,
                "target_clearance_message": target_clearance.message,
                "target_clearance_data": target_clearance.data,
                "start_success": bool(fly.success),
                "start_message": fly.message,
                "start_data": fly.data,
                "duration_sec": time.monotonic() - started,
            }
            attempts.append(attempt_record)
            goal_id = (fly.data or {}).get("goal_id") if isinstance(fly.data, dict) else None
            if fly.success and goal_id:
                wait = self._wait_fly_goal_resilient(tools, goal_id, target)
                attempt_record.update(
                    {
                        "wait_success": bool(wait.success),
                        "wait_message": wait.message,
                        "wait_data": wait.data,
                        "duration_sec": time.monotonic() - started,
                    }
                )
                if wait.success:
                    self._append_step(
                        start_step_name,
                        "succeeded",
                        {
                            "success": True,
                            "message": fly.message,
                            "data": fly.data,
                            "attempt_count": attempt_index,
                            "internal_attempts": attempts,
                        },
                    )
                    self._append_step(
                        wait_step_name,
                        "succeeded",
                        {
                            "success": True,
                            "message": wait.message,
                            "data": wait.data,
                            "attempt_count": attempt_index,
                            "internal_attempts": attempts,
                        },
                    )
                    return True
                cleanup = self._cancel_failed_fly_attempt(tools, goal_id, attempt_index)
                attempt_record["cleanup"] = cleanup.data
            elif fly.success:
                cleanup = self._cancel_failed_fly_attempt(tools, None, attempt_index)
                attempt_record["cleanup"] = cleanup.data

            if attempt_index < max_attempts and not self._cable_aware_failure_should_fallback_immediately(attempt_record):
                self._wait_maneuver_idle(
                    tools,
                    timeout_sec=self.args.fly_fallback_idle_timeout_sec,
                    stable_sec=self.args.fly_fallback_idle_stable_sec,
                )
                tools.activate_custom_operation(
                    timeout_sec=self.args.custom_mode_timeout_sec,
                    postcondition_timeout_sec=self.args.custom_mode_timeout_sec,
                )
                time.sleep(self.args.cable_aware_fly_retry_delay_sec)
            else:
                break

        fallback_reason = self._cable_aware_fallback_reason(attempts)
        self._last_cable_aware_fallback_reason = {"reason": fallback_reason["message"]}
        self._append_step(
            f"{wait_step_name}_{fallback_reason['step_suffix']}",
            "unavailable",
            {
                "success": False,
                "message": fallback_reason["message"],
                "attempt_count": len(attempts),
                "internal_attempts": attempts,
            },
        )
        return False

    @staticmethod
    def _cable_aware_fallback_reason(attempts: list[dict[str, Any]]) -> dict[str, str]:
        last = attempts[-1] if attempts else {}
        if last.get("target_clearance_success") is False:
            return {
                "step_suffix": "cable_aware_target_clearance_fallback",
                "message": "cable-aware fly-to-position skipped by target clearance precheck; fallback required",
            }
        if last.get("validation_success") is False:
            return {
                "step_suffix": "cable_aware_overview_validation_fallback",
                "message": "cable-aware fly-to-position skipped by overview validation; fallback required",
            }
        return {
            "step_suffix": "cable_aware_retries_exhausted",
            "message": "cable-aware fly-to-position retries exhausted; fallback required",
        }

    @staticmethod
    def _cable_aware_failure_should_fallback_immediately(attempt_record: dict[str, Any]) -> bool:
        text = json.dumps(attempt_record, default=str).lower()
        planner_failure_markers = (
            "a* failed",
            "trajectory generation failed",
            "failed to find a cable-safe path",
            "cableawaretrajectoryplanner",
            "stored powerline overview does not match simulation geometry",
            "stored powerline overview validation timed out",
            "cable-aware target violates stored powerline clearance",
            "stored powerline target clearance validation timed out",
        )
        return any(marker in text for marker in planner_failure_markers)

    @staticmethod
    def _tool_call_with_deadline(
        callback: Callable[[], ToolResult],
        *,
        timeout_sec: float,
        timeout_message: str,
    ) -> ToolResult:
        def _raise_timeout(_signum: int, _frame: Any) -> None:
            raise WorkflowToolTimeout(timeout_message)

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _raise_timeout)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, max(0.1, float(timeout_sec)))
        try:
            return callback()
        except WorkflowToolTimeout as exc:
            return ToolResult(False, {"timeout_sec": float(timeout_sec)}, str(exc))
        except Exception as exc:
            return ToolResult(False, {"error": repr(exc)}, "tool call failed")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0.0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    def _cancel_failed_fly_attempt(self, tools: DroneAgentTools, goal_id: str | None, attempt_index: int) -> ToolResult:
        if goal_id:
            cancel = tools.cancel_operation_goal(goal_id, timeout_sec=2.0)
        else:
            cancel = tools.cancel_all_operation_goals(
                timeout_sec=2.0,
                reason=f"cleanup failed cable-aware fly attempt {attempt_index}",
            )
        cancel_all = tools.cancel_all_operation_goals(
            timeout_sec=2.0,
            reason=f"cleanup failed cable-aware fly attempt {attempt_index}",
        )
        idle = self._wait_maneuver_idle(
            tools,
            timeout_sec=self.args.fly_fallback_idle_timeout_sec,
            stable_sec=max(self.args.fly_fallback_idle_stable_sec, 0.3),
        )
        result = ToolResult(
            cancel.success and cancel_all.success and idle.success,
            {
                "cancel_goal": {"success": cancel.success, "message": cancel.message, "data": cancel.data},
                "cancel_all": {"success": cancel_all.success, "message": cancel_all.message, "data": cancel_all.data},
                "idle": {"success": idle.success, "message": idle.message, "data": idle.data},
            },
            "failed fly attempt cleaned up" if cancel.success and cancel_all.success and idle.success else "failed fly attempt cleanup incomplete",
        )
        self._append_step(
            f"cleanup_failed_cable_aware_fly_attempt_{attempt_index}",
            "succeeded" if result.success else "failed",
            result.data,
        )
        return result

    def _wait_fly_goal_resilient(self, tools: DroneAgentTools, goal_id: str, target: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        max_wait_sec = self.args.fly_wait_timeout_sec if self.args.fly_wait_timeout_sec > 0.0 else None
        last_feedback_at: float | None = None
        latest_wait: ToolResult | None = None
        latest_pose: ToolResult | None = None
        latest_idle: ToolResult | None = None

        while True:
            wait = tools.operation_goal_status(goal_id)
            latest_wait = wait
            data = wait.data if isinstance(wait.data, dict) else {}
            state = str(data.get("state") or wait.message or "")
            if state in {"succeeded", "failed", "cancelled", "rejected"}:
                return ToolResult(state == "succeeded", data, state)

            now = time.monotonic()
            if max_wait_sec is not None and now - started >= float(max_wait_sec):
                return ToolResult(False, data, "goal still active after wait timeout")

            feedback_at = data.get("last_feedback_at")
            if isinstance(feedback_at, (int, float)):
                last_feedback_at = float(feedback_at)
            feedback_stale = (
                last_feedback_at is not None
                and time.time() - last_feedback_at > float(self.args.fly_feedback_stale_timeout_sec)
            )

            latest_idle = self._wait_maneuver_idle(
                tools,
                timeout_sec=0.2,
                stable_sec=0.05,
            )
            if latest_idle.success:
                latest_pose = self._check_pose_at_target_once(tools, target)
                if latest_pose.success:
                    cleanup = self._cleanup_recovered_operation_goal(tools, goal_id)
                    return ToolResult(
                        True,
                        {
                            "wait": data,
                            "idle": latest_idle.data,
                            "pose": latest_pose.data,
                            "cleanup": cleanup.data,
                            "recovered_from": "maneuver idle while operation action remained active",
                        },
                        "maneuver idle and target pose reached while operation action remained active",
                    )

            if feedback_stale:
                return ToolResult(
                    False,
                    {
                        "wait": data,
                        "idle": latest_idle.data if latest_idle else None,
                        "pose": latest_pose.data if latest_pose else None,
                    },
                    "goal feedback stale",
                )
            time.sleep(0.1)

    def _cleanup_recovered_operation_goal(self, tools: DroneAgentTools, goal_id: str) -> ToolResult:
        cancel = tools.cancel_operation_goal(goal_id, timeout_sec=2.0)
        idle = self._wait_maneuver_idle(
            tools,
            timeout_sec=self.args.fly_fallback_idle_timeout_sec,
            stable_sec=max(self.args.fly_fallback_idle_stable_sec, 0.3),
        )
        result = ToolResult(
            cancel.success and idle.success,
            {
                "cancel": {"success": cancel.success, "message": cancel.message, "data": cancel.data},
                "idle": {"success": idle.success, "message": idle.message, "data": idle.data},
            },
            "recovered operation goal cleaned up" if cancel.success and idle.success else "recovered operation goal cleanup incomplete",
        )
        self._append_step(
            f"cleanup_recovered_operation_goal_{goal_id}",
            "succeeded" if result.success else "failed",
            result.data,
        )
        return result

    def _check_pose_at_target_once(self, tools: DroneAgentTools, target: dict[str, Any]) -> ToolResult:
        latest_pose = tools._lookup_world_drone_pose(timeout_sec=1.0)
        latest_error = (
            (latest_pose["x"] - target["x"]) ** 2
            + (latest_pose["y"] - target["y"]) ** 2
            + (latest_pose["z"] - target["z"]) ** 2
        ) ** 0.5
        latest_gazebo_pose: dict[str, float] | None = None
        latest_gazebo_xy_error: float | None = None
        latest_gazebo_error: float | None = None
        gazebo_ok = True
        gazebo_target = target.get("gazebo_ground_truth_pose") if isinstance(target.get("gazebo_ground_truth_pose"), dict) else None
        if gazebo_target is not None:
            try:
                latest_gazebo_pose = self._current_gazebo_drone_pose(tools)
                latest_gazebo_xy_error = (
                    (latest_gazebo_pose["x"] - float(gazebo_target["x"])) ** 2
                    + (latest_gazebo_pose["y"] - float(gazebo_target["y"])) ** 2
                ) ** 0.5
                latest_gazebo_error = (
                    latest_gazebo_xy_error**2
                    + (latest_gazebo_pose["z"] - float(gazebo_target["z"])) ** 2
                ) ** 0.5
                gazebo_ok = latest_gazebo_error <= self.args.gazebo_position_tolerance_m
            except Exception as exc:
                latest_gazebo_pose = {"error": str(exc)}
                gazebo_ok = False
        success = latest_error <= self.args.position_tolerance_m and gazebo_ok
        return ToolResult(
            success,
            {
                "pose": latest_pose,
                "gazebo_pose": latest_gazebo_pose,
                "target": target,
                "position_error_m": latest_error,
                "gazebo_position_xy_error_m": latest_gazebo_xy_error,
                "gazebo_position_error_m": latest_gazebo_error,
                "tolerance_m": self.args.position_tolerance_m,
                "gazebo_tolerance_m": self.args.gazebo_position_tolerance_m if gazebo_target is not None else None,
            },
            "target pose reached" if success else "target pose not reached",
        )


    def _wait_maneuver_idle(self, tools: DroneAgentTools, *, timeout_sec: float, stable_sec: float) -> ToolResult:
        idle = tools._wait_operation_idle(timeout_sec=timeout_sec, stable_sec=stable_sec, require_reference_idle=False)
        return ToolResult(
            idle,
            {"timeout_sec": timeout_sec, "stable_sec": stable_sec, "require_reference_idle": False},
            "maneuver execution idle" if idle else "maneuver execution did not become idle",
        )

    def _adjust_target_altitude_for_ground_estimate(
        self,
        tools: DroneAgentTools,
        target: dict[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        step_name = f"adjust_{label}_altitude_for_ground_estimate"
        if isinstance(target.get("gazebo_ground_truth_pose"), dict):
            self._append_step(
                step_name,
                "skipped",
                {
                    "reason": (
                        "target is an authoritative Gazebo fixture remapped into the live ROS world; "
                        "the ROS ground estimate uses a different vertical datum"
                    ),
                    "target": target,
                },
            )
            return target

        try:
            from iii_drone_interfaces.msg import CombinedDroneAwareness
        except ImportError as exc:
            self._append_step(
                step_name,
                "skipped",
                {"reason": f"iii_drone_interfaces unavailable: {exc!r}", "target": target},
            )
            return target

        msg = tools._take_message(
            "/control/maneuver_controller/combined_drone_awareness",
            CombinedDroneAwareness,
            self.args.ground_estimate_timeout_sec,
            required=False,
        )
        if msg is None:
            self._append_step(
                step_name,
                "skipped",
                {
                    "reason": "combined drone awareness unavailable",
                    "timeout_sec": self.args.ground_estimate_timeout_sec,
                    "target": target,
                },
            )
            return target

        minimum_z = (
            float(msg.ground_altitude_estimate)
            + self.args.minimum_staging_above_ground
            + self.args.staging_ground_clearance_margin
        )
        adjusted = dict(target)
        if adjusted["z"] < minimum_z:
            adjusted["z"] = minimum_z
            adjusted["z_adjusted_to_ground_estimate"] = True
        self._append_step(
            step_name,
            "succeeded",
            {
                "ground_altitude_estimate": float(msg.ground_altitude_estimate),
                "minimum_staging_above_ground": self.args.minimum_staging_above_ground,
                "staging_ground_clearance_margin": self.args.staging_ground_clearance_margin,
                "minimum_z": minimum_z,
                "target_before": target,
                "target_after": adjusted,
            },
        )
        self._write_status("running", f"{label} target altitude adjusted", target=adjusted)
        return adjusted

    def _store_powerline_overview_with_retries(self, tools: DroneAgentTools) -> ToolResult:
        attempts = max(1, int(self.args.overview_store_attempts))
        results: list[dict[str, Any]] = []
        for attempt_index in range(1, attempts + 1):
            if attempt_index > 1:
                self._append_step(
                    f"store_powerline_overview_retry_{attempt_index}_restart_mapper",
                    "running",
                    {"attempt": attempt_index, "attempts": attempts},
                )
                restart = tools.pl_mapper("start", reset=True, timeout_sec=3.0)
                results.append(
                    {
                        "attempt": attempt_index,
                        "phase": "restart_mapper",
                        "success": restart.success,
                        "message": restart.message,
                        "data": restart.data,
                    }
                )
                if not restart.success:
                    return ToolResult(False, {"attempts": results}, f"pl mapper restart failed before overview attempt {attempt_index}")
                wait = tools.wait_powerline_lines(
                    min_lines=self.args.min_powerline_lines,
                    timeout_sec=self.args.powerline_timeout_sec,
                    filename=f"powerline_latest_retry_{attempt_index}.json",
                )
                results.append(
                    {
                        "attempt": attempt_index,
                        "phase": "wait_powerline_lines",
                        "success": wait.success,
                        "message": wait.message,
                        "data": wait.data,
                    }
                )
                if not wait.success:
                    return ToolResult(False, {"attempts": results}, f"powerline lines unavailable before overview attempt {attempt_index}")

            update = tools.update_powerline_overview(
                timeout_s=self.args.overview_timeout_s,
                service_timeout_sec=self.args.overview_service_timeout_sec,
            )
            results.append(
                {
                    "attempt": attempt_index,
                    "phase": "update_powerline_overview",
                    "success": update.success,
                    "message": update.message,
                    "data": update.data,
                }
            )
            if update.success:
                return ToolResult(
                    True,
                    {"attempts": results, "successful_attempt": attempt_index},
                    f"stored powerline overview on attempt {attempt_index}",
                )
            time.sleep(self.args.overview_retry_delay_sec)

        return ToolResult(
            False,
            {"attempts": results},
            f"store_powerline_overview failed after {attempts} attempt(s)",
        )

    def _wait_powerline_lines_with_retries(self, tools: DroneAgentTools) -> ToolResult:
        attempts = max(1, int(self.args.overview_store_attempts))
        results: list[dict[str, Any]] = []
        for attempt_index in range(1, attempts + 1):
            if attempt_index > 1:
                restart = tools.pl_mapper("start", reset=True, timeout_sec=3.0)
                results.append(
                    {
                        "attempt": attempt_index,
                        "phase": "restart_mapper",
                        "success": restart.success,
                        "message": restart.message,
                        "data": restart.data,
                    }
                )
                if not restart.success:
                    return ToolResult(False, {"attempts": results}, f"pl mapper restart failed before line wait attempt {attempt_index}")
                time.sleep(self.args.overview_retry_delay_sec)

            wait = tools.wait_powerline_lines(
                min_lines=self.args.min_powerline_lines,
                timeout_sec=self.args.powerline_timeout_sec,
                filename="powerline_latest.json" if attempt_index == 1 else f"powerline_latest_wait_retry_{attempt_index}.json",
            )
            results.append(
                {
                    "attempt": attempt_index,
                    "phase": "wait_powerline_lines",
                    "success": wait.success,
                    "message": wait.message,
                    "data": wait.data,
                }
            )
            if wait.success:
                return ToolResult(
                    True,
                    {"attempts": results, "successful_attempt": attempt_index, "latest": wait.data},
                    f"observed required powerline lines on attempt {attempt_index}",
                )

        return ToolResult(
            False,
            {"attempts": results},
            f"powerline lines unavailable after {attempts} attempt(s)",
        )

    def _target_from_geometry(self, position_id: str, *, tools: DroneAgentTools | None = None) -> dict[str, Any] | None:
        if not position_id:
            return None
        target_id = canonical_fixture_id(position_id)
        try:
            fixture = json.loads(DEFAULT_GEOMETRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for section in GEOMETRY_POSITION_SECTIONS:
            for position in fixture.get(section, []):
                if canonical_fixture_id(str(position.get("id") or "")) != target_id:
                    continue
                pose = dict(position.get("pose") or {})
                gazebo_pose = self._fixture_gazebo_pose(position)
                if tools is not None and gazebo_pose is not None:
                    mapped = self._map_gazebo_pose_to_live_ros_world(tools, gazebo_pose)
                    mapped.update(
                        {
                            "position_id": position_id,
                            "canonical_position_id": target_id,
                            "frame_id": position.get("frame_id") or "world",
                            "fixture_label": position.get("label"),
                            "fixture_section": section,
                            "target_source": "gazebo_ground_truth_mapped_to_live_ros_world",
                        }
                    )
                    return mapped
                if {"x", "y", "z", "yaw"}.issubset(pose.keys()):
                    return {
                        "position_id": position_id,
                        "canonical_position_id": target_id,
                        "frame_id": position.get("frame_id") or "world",
                        "x": float(pose["x"]),
                        "y": float(pose["y"]),
                        "z": float(pose["z"]),
                        "yaw": float(pose["yaw"]),
                        "fixture_label": position.get("label"),
                        "fixture_section": section,
                    }
        return None

    def _missing_geometry_position_message(self, label: str, position_id: str) -> str:
        candidates = self._geometry_position_ids()
        suggestions = fixture_id_suggestions(position_id, candidates)
        suffix = f"; did you mean one of {suggestions}?" if suggestions else ""
        canonical = canonical_fixture_id(position_id)
        alias_note = f" (canonical alias: {canonical})" if canonical != self._normalize_position_id(position_id) else ""
        return f"{label} not found or missing pose: {position_id}{alias_note}{suffix}"

    def _geometry_position_ids(self) -> list[str]:
        try:
            fixture = json.loads(DEFAULT_GEOMETRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        ids: list[str] = []
        for section in GEOMETRY_POSITION_SECTIONS:
            for position in fixture.get(section, []):
                if isinstance(position, dict) and position.get("id"):
                    ids.append(str(position["id"]))
        return ids

    def _fixture_gazebo_pose(self, position: dict[str, Any]) -> dict[str, float] | None:
        pose = position.get("gazebo_ground_truth_pose")
        if not isinstance(pose, dict):
            pose = (position.get("recorded_from") or {}).get("gazebo_model_pose")
        if not isinstance(pose, dict):
            return None
        source = pose.get("position") if isinstance(pose.get("position"), dict) else pose
        if not isinstance(source, dict):
            return None
        if not {"x", "y", "z"}.issubset(source.keys()):
            return None
        yaw = pose.get("yaw")
        orientation = pose.get("orientation") if isinstance(pose.get("orientation"), dict) else None
        if yaw is None and orientation is not None:
            yaw = self._yaw_from_quaternion(orientation)
        if yaw is None:
            fallback = position.get("pose") if isinstance(position.get("pose"), dict) else {}
            yaw = fallback.get("yaw", 0.0)
        return {
            "x": float(source["x"]),
            "y": float(source["y"]),
            "z": float(source["z"]),
            "yaw": float(yaw),
        }

    def _map_gazebo_pose_to_live_ros_world(
        self,
        tools: DroneAgentTools,
        gazebo_pose: dict[str, float],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 30.0
        attempt = 0
        last_error = ""
        live_ros = None
        live_gazebo = None
        while time.monotonic() < deadline:
            attempt += 1
            try:
                live_ros = tools._lookup_world_drone_pose(timeout_sec=2.0)
                live_gazebo = self._current_gazebo_drone_pose(tools)
                break
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.5)
        if live_ros is None or live_gazebo is None:
            self._append_step(
                "map_gazebo_fixture_to_live_ros_world",
                "failed",
                {
                    "attempts": attempt,
                    "timeout_sec": 30.0,
                    "reason": f"live Gazebo/ROS mapping unavailable: {last_error}",
                },
            )
            raise RuntimeError(f"live Gazebo/ROS mapping unavailable after {attempt} attempt(s): {last_error}")
        self._append_step(
            "map_gazebo_fixture_to_live_ros_world",
            "succeeded",
            {"attempts": attempt, "live_ros_drone_pose": live_ros, "live_gazebo_drone_pose": live_gazebo},
        )
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
                "live_ros_drone_pose": live_ros,
                "live_gazebo_drone_pose": live_gazebo,
                "offset": offset,
                "delta_gazebo": delta_gazebo,
                "delta_ros": delta_ros,
            },
        }

    def _current_gazebo_drone_pose(self, tools: DroneAgentTools) -> dict[str, float]:
        result = tools.gazebo(
            "topic_once",
            topic=f"/world/{DEFAULT_GAZEBO_WORLD}/dynamic_pose/info",
            timeout_sec=5.0,
            filename="gz_dynamic_pose_for_target_mapping.txt",
        )
        stdout = ((result.data or {}).get("stdout") if isinstance(result.data, dict) else "") or ""
        pose = self._parse_gazebo_named_pose(stdout, DEFAULT_GAZEBO_DRONE_MODEL)
        if pose is None:
            raise RuntimeError(f"Gazebo pose for model {DEFAULT_GAZEBO_DRONE_MODEL!r} not found")
        return pose

    def _parse_gazebo_named_pose(self, stdout: str, model_name: str) -> dict[str, float] | None:
        for block in re.findall(r"pose\s*\{(.*?)(?=\npose\s*\{|\Z)", stdout, flags=re.S):
            if f'name: "{model_name}"' not in block:
                continue
            position = self._parse_proto_block(block, "position")
            orientation = self._parse_proto_block(block, "orientation")
            return {
                "x": float(position.get("x", 0.0)),
                "y": float(position.get("y", 0.0)),
                "z": float(position.get("z", 0.0)),
                "yaw": self._yaw_from_quaternion(orientation),
                "orientation": orientation,
            }
        return None

    @staticmethod
    def _parse_proto_block(text: str, name: str) -> dict[str, float]:
        match = re.search(rf"{re.escape(name)}\s*\{{(.*?)\}}", text, flags=re.S)
        if not match:
            return {}
        values: dict[str, float] = {}
        for key, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([-+0-9.eE]+)", match.group(1)):
            values[key] = float(value)
        return values

    @staticmethod
    def _yaw_from_quaternion(quaternion: dict[str, Any]) -> float:
        x = float(quaternion.get("x", 0.0))
        y = float(quaternion.get("y", 0.0))
        z = float(quaternion.get("z", 0.0))
        w = float(quaternion.get("w", 1.0))
        return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        return math.atan2(math.sin(angle), math.cos(angle))

    @staticmethod
    def _normalize_position_id(value: str) -> str:
        return normalize_fixture_id(value)

    def _wait_pose_at_target(self, tools: DroneAgentTools, target: dict[str, Any]) -> ToolResult:
        deadline = time.monotonic() + self.args.position_timeout_sec
        stable_since: float | None = None
        latest_pose: dict[str, float] | None = None
        latest_gazebo_pose: dict[str, float] | None = None
        latest_error = float("inf")
        latest_gazebo_xy_error = float("inf")
        latest_gazebo_error = float("inf")
        last_progress_log = 0.0
        gazebo_target = target.get("gazebo_ground_truth_pose") if isinstance(target.get("gazebo_ground_truth_pose"), dict) else None
        while time.monotonic() < deadline:
            latest_pose = tools._lookup_world_drone_pose(timeout_sec=1.0)
            latest_error = (
                (latest_pose["x"] - target["x"]) ** 2
                + (latest_pose["y"] - target["y"]) ** 2
                + (latest_pose["z"] - target["z"]) ** 2
            ) ** 0.5
            gazebo_ok = True
            if gazebo_target is not None:
                try:
                    latest_gazebo_pose = self._current_gazebo_drone_pose(tools)
                    latest_gazebo_xy_error = (
                        (latest_gazebo_pose["x"] - float(gazebo_target["x"])) ** 2
                        + (latest_gazebo_pose["y"] - float(gazebo_target["y"])) ** 2
                    ) ** 0.5
                    latest_gazebo_error = (
                        latest_gazebo_xy_error**2
                        + (latest_gazebo_pose["z"] - float(gazebo_target["z"])) ** 2
                    ) ** 0.5
                    gazebo_ok = latest_gazebo_error <= self.args.gazebo_position_tolerance_m
                except Exception as exc:
                    latest_gazebo_pose = {"error": str(exc)}
                    gazebo_ok = False
            if latest_error <= self.args.position_tolerance_m and gazebo_ok:
                if stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= self.args.position_settle_sec:
                    return ToolResult(
                        True,
                        {
                            "pose": latest_pose,
                            "gazebo_pose": latest_gazebo_pose,
                            "target": target,
                            "position_error_m": latest_error,
                            "gazebo_position_xy_error_m": latest_gazebo_xy_error if gazebo_target is not None else None,
                            "gazebo_position_error_m": latest_gazebo_error if gazebo_target is not None else None,
                            "tolerance_m": self.args.position_tolerance_m,
                            "gazebo_tolerance_m": self.args.gazebo_position_tolerance_m if gazebo_target is not None else None,
                            "settle_sec": self.args.position_settle_sec,
                        },
                        "staging pose reached",
                    )
            else:
                stable_since = None
            now = time.monotonic()
            if now - last_progress_log >= 10.0:
                last_progress_log = now
                self._write_status(
                    "running",
                    "pose wait progress",
                    pose_wait={
                        "target_position_id": target.get("position_id"),
                        "position_error_m": latest_error,
                        "gazebo_position_xy_error_m": latest_gazebo_xy_error if gazebo_target is not None else None,
                        "gazebo_position_error_m": latest_gazebo_error if gazebo_target is not None else None,
                        "tolerance_m": self.args.position_tolerance_m,
                        "gazebo_tolerance_m": self.args.gazebo_position_tolerance_m if gazebo_target is not None else None,
                        "gazebo_ok": gazebo_ok,
                        "pose": latest_pose,
                        "gazebo_pose": latest_gazebo_pose,
                    },
                )
            time.sleep(0.2)
        return ToolResult(
            False,
            {
                "pose": latest_pose,
                "gazebo_pose": latest_gazebo_pose,
                "target": target,
                "position_error_m": latest_error,
                "gazebo_position_xy_error_m": latest_gazebo_xy_error if gazebo_target is not None else None,
                "gazebo_position_error_m": latest_gazebo_error if gazebo_target is not None else None,
                "tolerance_m": self.args.position_tolerance_m,
                "gazebo_tolerance_m": self.args.gazebo_position_tolerance_m if gazebo_target is not None else None,
                "settle_sec": self.args.position_settle_sec,
                "timeout_sec": self.args.position_timeout_sec,
            },
            "staging pose not reached before timeout",
        )

    def _wait_takeoff_mode_exit(self, tools: DroneAgentTools) -> ToolResult:
        try:
            from px4_msgs.msg import VehicleLandDetected, VehicleStatus
        except ImportError as exc:
            return ToolResult(False, {"error": repr(exc)}, "px4_msgs is required for takeoff-mode inspection")

        deadline = time.monotonic() + self.args.takeoff_mode_exit_timeout_sec
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            vehicle_status = tools._take_message(
                "/fmu/out/vehicle_status_v1",
                VehicleStatus,
                min(0.5, remaining),
                required=False,
            )
            land_detected = tools._take_message(
                "/fmu/out/vehicle_land_detected",
                VehicleLandDetected,
                min(0.2, remaining),
                required=False,
            )
            if vehicle_status is not None:
                latest["nav_state"] = int(vehicle_status.nav_state)
                latest["nav_state_user_intention"] = int(vehicle_status.nav_state_user_intention)
                latest["arming_state"] = int(vehicle_status.arming_state)
            if land_detected is not None:
                latest["in_air"] = not bool(land_detected.landed)
                latest["landed"] = bool(land_detected.landed)
            takeoff_exited = (
                latest.get("nav_state") is not None
                and int(latest["nav_state"]) != int(VehicleStatus.NAVIGATION_STATE_AUTO_TAKEOFF)
            )
            airborne_or_armed = (
                latest.get("in_air") is True
                or int(latest.get("arming_state", -1)) == int(VehicleStatus.ARMING_STATE_ARMED)
            )
            if takeoff_exited and airborne_or_armed:
                return ToolResult(
                    True,
                    {"px4_status": latest, "timeout_sec": self.args.takeoff_mode_exit_timeout_sec},
                    "PX4 exited takeoff mode",
                )
            time.sleep(0.5)
        return ToolResult(
            False,
            {"px4_status": latest, "timeout_sec": self.args.takeoff_mode_exit_timeout_sec},
            "PX4 did not exit TAKEOFF mode before timeout",
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the III-Drone mission deployment staging workflow")
    parser.add_argument("--workflow-id", default="mission_deploy_manual")
    parser.add_argument("--artifact-dir", default="/tmp/iii_drone/mission_deploy/manual")
    parser.add_argument("--status-path", default="")
    parser.add_argument("--position-id", default=DEFAULT_POSITION_ID)
    parser.add_argument("--frame-id", default="world")
    parser.add_argument("--x", type=float)
    parser.add_argument("--y", type=float)
    parser.add_argument("--z", type=float)
    parser.add_argument("--yaw", type=float)
    parser.add_argument("--mission-start-position-id", default="")
    parser.add_argument("--mission-start-frame-id", default="world")
    parser.add_argument("--mission-start-x", type=float)
    parser.add_argument("--mission-start-y", type=float)
    parser.add_argument("--mission-start-z", type=float)
    parser.add_argument("--mission-start-yaw", type=float)
    parser.add_argument("--minimum-staging-z", type=float, default=0.0)
    parser.add_argument("--minimum-staging-above-ground", type=float, default=1.0)
    parser.add_argument("--staging-ground-clearance-margin", type=float, default=0.03)
    parser.add_argument("--ground-estimate-timeout-sec", type=float, default=3.0)
    parser.add_argument("--takeoff-altitude", type=float, default=2.0)
    parser.add_argument("--takeoff-mode-exit-timeout-sec", type=float, default=20.0)
    parser.add_argument("--arm-health-stable-sec", type=float, default=2.0)
    parser.add_argument("--post-takeoff-health-stable-sec", type=float, default=2.0)
    parser.add_argument("--px4-command-attempts", type=int, default=3)
    parser.add_argument("--px4-command-retry-delay-sec", type=float, default=2.0)
    parser.set_defaults(disable_manual_input_requirement=True)
    parser.add_argument("--disable-manual-input-requirement", dest="disable_manual_input_requirement", action="store_true")
    parser.add_argument("--keep-manual-input-requirement", dest="disable_manual_input_requirement", action="store_false")
    parser.add_argument("--min-powerline-lines", type=int, default=4)
    parser.add_argument("--powerline-timeout-sec", type=float, default=12.0)
    parser.add_argument("--overview-timeout-s", type=int, default=8)
    parser.add_argument("--overview-service-timeout-sec", type=float, default=15.0)
    parser.add_argument("--overview-query-timeout-sec", type=float, default=2.0)
    parser.add_argument("--overview-store-attempts", type=int, default=3)
    parser.add_argument("--overview-retry-delay-sec", type=float, default=1.0)
    parser.add_argument("--min-pylons", type=int, default=2)
    parser.add_argument("--pylon-overview-timeout-sec", type=float, default=2.0)
    parser.add_argument("--mission-specification-file", default="")
    parser.add_argument("--use-default-mission-specification", action="store_true")
    parser.add_argument("--demo-pos-over-corridor-id", default=DEMO_POS_OVER_CORRIDOR_ID)
    parser.add_argument("--demo-pos-pylon-1-id", default=DEMO_POS_PYLON_1_ID)
    parser.add_argument("--demo-pos-pylon-2-id", default=DEMO_POS_PYLON_2_ID)
    parser.set_defaults(require_pylon_overview=False)
    parser.add_argument("--require-pylon-overview", dest="require_pylon_overview", action="store_true")
    parser.add_argument("--no-require-pylon-overview", dest="require_pylon_overview", action="store_false")
    parser.set_defaults(force_update_overview=True)
    parser.add_argument("--force-update-overview", dest="force_update_overview", action="store_true")
    parser.add_argument("--reuse-stored-overview", dest="force_update_overview", action="store_false")
    parser.add_argument("--mission-mode", default="reach_cable")
    parser.add_argument("--px4-timeout-sec", type=float, default=30.0)
    parser.add_argument("--custom-mode-timeout-sec", type=float, default=10.0)
    parser.add_argument("--fly-send-timeout-sec", type=float, default=10.0)
    parser.add_argument("--fly-wait-timeout-sec", type=float, default=0.0)
    parser.add_argument("--fly-feedback-stale-timeout-sec", type=float, default=10.0)
    parser.add_argument("--cable-aware-fly-attempts", type=int, default=3)
    parser.add_argument("--cable-aware-fly-retry-delay-sec", type=float, default=0.5)
    parser.add_argument("--fly-fallback-idle-timeout-sec", type=float, default=5.0)
    parser.add_argument("--fly-fallback-idle-stable-sec", type=float, default=0.3)
    parser.add_argument("--position-timeout-sec", type=float, default=60.0)
    parser.add_argument("--position-tolerance-m", type=float, default=0.25)
    parser.add_argument("--gazebo-position-tolerance-m", type=float, default=0.75)
    parser.add_argument("--position-settle-sec", type=float, default=1.0)
    parser.add_argument("--skip-mission-activation", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.pid = os.getpid()
    if not args.status_path:
        args.status_path = str(Path(args.artifact_dir) / "status.json")
    raise SystemExit(MissionDeployWorkflow(args).run())


if __name__ == "__main__":
    main()
