import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from iii_drone_mcp.agent_tools import ToolResult
from iii_drone_mcp.mission_deploy_workflow import (
    DEFAULT_INSPECTION_MISSION_START_POSITION_ID,
    MissionDeployWorkflow,
    build_parser,
)


def _workflow(tmp_path):
    args = build_parser().parse_args(
        [
            "--artifact-dir",
            str(tmp_path),
            "--status-path",
            str(tmp_path / "status.json"),
        ]
    )
    args.pid = 1
    return MissionDeployWorkflow(args)


def test_deploy_workflow_accepts_explicit_px4_mavlink_endpoint(tmp_path):
    args = build_parser().parse_args(
        [
            "--artifact-dir",
            str(tmp_path),
            "--status-path",
            str(tmp_path / "status.json"),
            "--px4-system-address",
            "udpin://0.0.0.0:14551",
        ]
    )

    assert args.px4_system_address == "udpin://0.0.0.0:14551"


def test_deploy_workflow_uses_profile_px4_endpoint_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("III_PX4_SYSTEM_ADDRESS", "udpin://0.0.0.0:14542")

    args = build_parser().parse_args(
        [
            "--artifact-dir",
            str(tmp_path),
            "--status-path",
            str(tmp_path / "status.json"),
        ]
    )

    assert args.px4_system_address == "udpin://0.0.0.0:14542"


def test_deploy_workflow_accepts_remote_runtime_api_endpoint(tmp_path):
    args = build_parser().parse_args(
        [
            "--artifact-dir",
            str(tmp_path),
            "--status-path",
            str(tmp_path / "status.json"),
            "--runtime-api-url",
            "http://192.168.1.251:8765",
        ]
    )

    assert args.runtime_api_url == "http://192.168.1.251:8765"


def test_optional_workflow_step_records_callback_exception_without_aborting(tmp_path):
    workflow = _workflow(tmp_path)

    result = workflow._step(
        "optional_status",
        lambda: (_ for _ in ()).throw(TimeoutError("status unavailable")),
        required=False,
    )

    assert result.success is False
    assert workflow.steps[-1]["state"] == "unavailable"
    assert "TimeoutError" in workflow.steps[-1]["data"]["exception"]


def test_deploy_workflow_selects_catalog_ids_and_rejects_legacy_path_option(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.args.mission_mode = "reach_cable"
    calls = []
    tools = SimpleNamespace(
        system=lambda *args, **kwargs: ToolResult(True, {"args": args, "kwargs": kwargs}, "started"),
        mission_status=lambda **_kwargs: ToolResult(True, {"active_catalog_id": "inspection-production"}, "status"),
        select_mission_catalog_entry=lambda **kwargs: (
            calls.append(kwargs)
            or ToolResult(True, {"active_catalog_id": kwargs["catalog_id"]}, "selected")
        ),
    )
    workflow._step = lambda _name, callback, required=True: callback()
    workflow._select_mission_catalog_if_requested(tools)
    assert calls == [
        {
            "catalog_id": "reach-charge-leave-experimental",
            "use_default": False,
            "timeout_sec": 10.0,
        }
    ]

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "--artifact-dir", str(tmp_path),
                "--status-path", str(tmp_path / "status.json"),
                "--mission-specification-file", "/tmp/mission.yaml",
            ]
        )


def test_deploy_workflow_default_selection_is_explicit_and_active_id_is_noop(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.args.use_default_mission_catalog = True
    calls = []
    tools = SimpleNamespace(
        system=lambda *args, **kwargs: ToolResult(True, {}, "started"),
        select_mission_catalog_entry=lambda **kwargs: calls.append(kwargs) or ToolResult(True, {}, "default"),
    )
    workflow._step = lambda _name, callback, required=True: callback()
    workflow._select_mission_catalog_if_requested(tools)
    assert calls == [{"catalog_id": "", "use_default": True, "timeout_sec": 10.0}]

    workflow = _workflow(tmp_path / "active")
    workflow.args.mission_catalog_id = "inspection-production"
    calls.clear()
    tools = SimpleNamespace(
        system=lambda *args, **kwargs: ToolResult(True, {}, "started"),
        mission_status=lambda **_kwargs: ToolResult(True, {"active_catalog_id": "inspection-production"}, "status"),
        select_mission_catalog_entry=lambda **kwargs: calls.append(kwargs) or ToolResult(True, {}, "selected"),
    )
    workflow._step = lambda _name, callback, required=True: callback()
    workflow._select_mission_catalog_if_requested(tools)
    assert calls == []


def test_active_mission_executor_does_not_trigger_redundant_selected_node_start(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        system=lambda command, **kwargs: (
            calls.append((command, kwargs))
            or ToolResult(
                True,
                {"stdout": "Managed nodes:\\n  mission_executor: active\\n"},
                "status",
            )
        )
    )

    result = workflow._ensure_mission_executor_active(tools)

    assert result.success is True
    assert result.data["already_active"] is True
    assert calls == [("status", {"timeout_sec": 10.0})]


def test_active_custom_operation_does_not_trigger_redundant_selected_node_start(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        system=lambda command, **kwargs: (
            calls.append((command, kwargs))
            or ToolResult(
                True,
                {"stdout": "Managed nodes:\n  custom_operation: active\n"},
                "status",
            )
        )
    )

    result = workflow._ensure_system_entity_active(tools, "custom_operation")

    assert result.success is True
    assert result.data["already_active"] is True
    assert result.data["entity_id"] == "custom_operation"
    assert calls == [("status", {"timeout_sec": 10.0})]


def test_prearm_external_mode_readiness_refreshes_stale_custom_operation_registration(tmp_path):
    workflow = _workflow(tmp_path)
    masks = iter([0, 1 << 27])
    refresh_calls = []
    operation_status = ToolResult(
        True,
        {"status": SimpleNamespace(data='{"mode_id":27}')},
        "CustomOperation status ready",
    )

    tools = SimpleNamespace(
        _wait_custom_operation_status=lambda **_kwargs: operation_status,
        px4_safety=lambda **_kwargs: ToolResult(
            True,
            {"ros": {"vehicle_status": {"can_set_nav_states_mask": next(masks), "nav_state": 4}}},
            "PX4 safety ok",
        ),
        system=lambda command, **kwargs: (
            refresh_calls.append((command, kwargs))
            or ToolResult(True, {}, "custom operation restarted")
        ),
    )

    workflow._ensure_custom_operation_mode_settable(tools, "prearm")

    assert refresh_calls == [
        (
            "restart",
            {
                "entity_id": "custom_operation",
                "include_dependencies": False,
                "cold": False,
                "timeout_sec": 180.0,
            },
        )
    ]
    assert workflow.steps[-1]["state"] == "succeeded"
    assert workflow.steps[-1]["data"]["attempts"][-1]["settable"] is True


def test_mission_mode_readiness_refreshes_stale_mission_executor_registration(tmp_path):
    workflow = _workflow(tmp_path)
    readiness = iter(
        [
            ToolResult(False, {"mission_mode_id": 23, "settable": False}, "not registered"),
            ToolResult(True, {"mission_mode_id": 23, "settable": True}, "registered"),
        ]
    )
    calls = []
    tools = SimpleNamespace(
        mission_mode_readiness=lambda **kwargs: (
            calls.append(("readiness", kwargs)) or next(readiness)
        ),
        system=lambda command, **kwargs: (
            calls.append((command, kwargs)) or ToolResult(True, {}, "restarted")
        ),
    )
    workflow._select_mission_catalog_if_requested = lambda _tools: calls.append(("catalog", {}))

    workflow._ensure_mission_mode_settable(tools)

    assert calls[0][0] == "readiness"
    assert calls[1] == (
        "restart",
        {
            "entity_id": "mission_executor",
            "include_dependencies": False,
            "cold": False,
            "timeout_sec": 180.0,
        },
    )
    assert calls[2][0] == "readiness"
    assert calls[3] == ("catalog", {})
    assert workflow.steps[-1]["state"] == "succeeded"


def test_takeoff_workflow_uses_target_resolved_direct_px4_commands(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        px4=lambda command, **kwargs: calls.append((command, kwargs)) or ToolResult(True, {}, command),
        px4_health=lambda **_kwargs: ToolResult(True, {}, "healthy"),
    )
    workflow._wait_takeoff_mode_exit = lambda _tools: ToolResult(True, {}, "mode exited")
    workflow._px4_command_with_retries = lambda _tools, command, **kwargs: (
        calls.append((command, kwargs)) or ToolResult(True, {}, command)
    )

    workflow._takeoff_if_needed(tools, {"armed": False, "in_air": False})

    assert ("arm_direct", {
        "timeout_sec": workflow.args.px4_timeout_sec,
        "postcondition_timeout_sec": workflow.args.px4_timeout_sec,
        "health_stable_sec": workflow.args.arm_health_stable_sec,
    }) in calls
    assert ("takeoff_direct", {
        "timeout_sec": workflow.args.px4_timeout_sec,
        "postcondition_timeout_sec": workflow.args.px4_timeout_sec,
        "takeoff_altitude_m": workflow.args.takeoff_altitude,
    }) in calls


def test_resilient_status_prefers_ros_truth_over_stale_mavsdk_status(tmp_path):
    workflow = _workflow(tmp_path)
    tools = SimpleNamespace(
        px4_safety=lambda **_kwargs: ToolResult(
            True,
            {
                "mavsdk": {
                    "available": True,
                    "status": {"armed": False, "in_air": False},
                },
                "ros": {
                    "available": True,
                    "vehicle_status": {
                        "arming_state": 2,
                        "nav_state": 27,
                        "failsafe": False,
                    },
                    "land_detected": {"landed": False},
                },
                "derived": {
                    "armed": True,
                    "in_air": True,
                    "flight_mode": "HOLD",
                    "nav_state": 27,
                    "failsafe": False,
                    "unexpected_recovery": False,
                },
            },
            "PX4 safety ok",
        )
    )

    result = workflow._px4_status_resilient(tools)

    assert result.success is True
    assert result.data["armed"] is True
    assert result.data["in_air"] is True
    assert result.data["arming_state"] == 2
    assert result.data["landed"] is False
    assert result.data["source"] == "px4_safety_reconciled"


def test_gazebo_fixture_altitude_is_not_overwritten_by_ros_ground_estimate(tmp_path):
    workflow = _workflow(tmp_path)
    target = {
        "position_id": "low_entry_side",
        "frame_id": "world",
        "x": 0.1,
        "y": 0.2,
        "z": 0.746464,
        "yaw": 0.0,
        "gazebo_ground_truth_pose": {"x": 1.0, "y": 2.0, "z": 1.669073, "yaw": 0.0},
    }
    interfaces_msg = ModuleType("iii_drone_interfaces.msg")
    interfaces_msg.CombinedDroneAwareness = object
    tools = SimpleNamespace(
        _take_message=lambda *_args, **_kwargs: SimpleNamespace(ground_altitude_estimate=0.209338)
    )

    with patch.dict(sys.modules, {"iii_drone_interfaces.msg": interfaces_msg}):
        adjusted = workflow._adjust_target_altitude_for_ground_estimate(
            tools, target, label="mission_start"
        )

    assert adjusted["z"] == pytest.approx(target["z"])
    assert "z_adjusted_to_ground_estimate" not in adjusted


def test_gazebo_staging_fixture_altitude_is_not_clamped_before_dispatch(tmp_path):
    workflow = _workflow(tmp_path)
    fixture = {
        "position_id": "staging_fixture",
        "frame_id": "world",
        "x": 1.0,
        "y": 2.0,
        "z": 0.3,
        "yaw": 0.0,
        "gazebo_ground_truth_pose": {"x": 4.0, "y": 5.0, "z": 0.6, "yaw": 0.1},
    }
    workflow.args.position_id = fixture["position_id"]
    workflow.args.minimum_staging_z = 0.5
    workflow._target_from_geometry = lambda *_args, **_kwargs: dict(fixture)

    target = workflow._target(SimpleNamespace())

    assert target["z"] == pytest.approx(0.3)
    assert "z_adjusted_to_minimum" not in target


def test_ros_only_target_still_observes_ground_clearance(tmp_path):
    workflow = _workflow(tmp_path)
    target = {"frame_id": "world", "x": 0.1, "y": 0.2, "z": 0.5, "yaw": 0.0}
    interfaces_msg = ModuleType("iii_drone_interfaces.msg")
    interfaces_msg.CombinedDroneAwareness = object
    tools = SimpleNamespace(
        _take_message=lambda *_args, **_kwargs: SimpleNamespace(ground_altitude_estimate=0.2)
    )

    with patch.dict(sys.modules, {"iii_drone_interfaces.msg": interfaces_msg}):
        adjusted = workflow._adjust_target_altitude_for_ground_estimate(
            tools, target, label="staging"
        )

    assert adjusted["z"] == pytest.approx(1.23)
    assert adjusted["z_adjusted_to_ground_estimate"] is True


def test_gazebo_pose_verification_includes_altitude(tmp_path):
    workflow = _workflow(tmp_path)
    target = {
        "frame_id": "world",
        "x": 0.1,
        "y": 0.2,
        "z": 0.7,
        "yaw": 0.0,
        "gazebo_ground_truth_pose": {"x": 1.0, "y": 2.0, "z": 1.7, "yaw": 0.0},
    }
    tools = SimpleNamespace(_lookup_world_drone_pose=lambda **_kwargs: dict(target))
    workflow._current_gazebo_drone_pose = lambda _tools: {
        "x": 1.0,
        "y": 2.0,
        "z": 2.7,
        "yaw": 0.0,
    }

    result = workflow._check_pose_at_target_once(tools, target)

    assert result.success is False
    assert result.data["gazebo_position_error_m"] == pytest.approx(1.0)


def test_fixture_xy_mapping_does_not_inherit_estimator_heading_error(tmp_path):
    workflow = _workflow(tmp_path)
    tools = SimpleNamespace(
        _lookup_world_drone_pose=lambda **_kwargs: {
            "x": 10.0,
            "y": 20.0,
            "z": 3.0,
            "yaw": -1.0,
        }
    )
    workflow._current_gazebo_drone_pose = lambda _tools: {
        "x": 1.0,
        "y": 2.0,
        "z": 4.0,
        "yaw": 0.5,
    }

    mapped = workflow._map_gazebo_pose_to_live_ros_world(
        tools,
        {"x": 4.0, "y": 6.0, "z": 9.0, "yaw": 0.7},
    )

    assert mapped["x"] == pytest.approx(14.0)
    assert mapped["y"] == pytest.approx(17.0)
    assert mapped["z"] == pytest.approx(8.0)
    assert mapped["yaw"] == pytest.approx(-0.8)
    assert mapped["live_mapping"]["position_yaw_offset"] == pytest.approx(-math.pi / 2.0)


def test_gazebo_pose_uses_bridged_ground_truth_when_local_gazebo_is_unavailable(tmp_path):
    workflow = _workflow(tmp_path)
    expected = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.4, "source": "bridged"}
    tools = SimpleNamespace(
        gazebo=lambda *_args, **_kwargs: ToolResult(False, message="gz executable unavailable"),
        _lookup_simulation_ground_truth_drone_pose=lambda **_kwargs: dict(expected),
    )

    assert workflow._current_gazebo_drone_pose(tools) == expected


def test_gazebo_pose_uses_bridged_ground_truth_when_gazebo_query_throws(tmp_path):
    workflow = _workflow(tmp_path)
    expected = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.4, "source": "bridged"}
    tools = SimpleNamespace(
        gazebo=lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("gz")),
        _lookup_simulation_ground_truth_drone_pose=lambda **_kwargs: dict(expected),
    )

    assert workflow._current_gazebo_drone_pose(tools) == expected


def test_inspection_mission_start_uses_cable_aware_flight_without_direct_fallback(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.args.mission_mode = "inspection_demo"

    assert workflow._mission_start_fly_operation() == (
        "cable_aware_fly_to_position",
        "start_cable_aware_fly_to_mission_start_position",
        "wait_cable_aware_fly_to_mission_start_position",
        None,
    )


def test_inspection_defaults_to_first_class_outside_corridor_mission_start_fixture(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.args.mission_mode = "inspection_demo"
    workflow.args.position_id = "mid_corridor_taken_off_conductors_visible"
    workflow.args.mission_start_position_id = ""
    expected = {
        "position_id": DEFAULT_INSPECTION_MISSION_START_POSITION_ID,
        "frame_id": "world",
        "x": 1.0,
        "y": 2.0,
        "z": 0.6,
        "yaw": 0.0,
    }
    workflow._target_from_geometry = lambda position_id, **_kwargs: (
        dict(expected) if position_id == DEFAULT_INSPECTION_MISSION_START_POSITION_ID else None
    )

    assert workflow._mission_start_target(SimpleNamespace()) == expected
    assert DEFAULT_INSPECTION_MISSION_START_POSITION_ID == "low_entry_side"


def test_powerline_overview_is_refreshed_after_long_pylon_survey(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        pl_mapper=lambda command, **kwargs: (
            calls.append(("mapper", command, kwargs))
            or ToolResult(True, {}, "mapper restarted")
        )
    )
    workflow._wait_powerline_lines_with_retries = lambda _tools: (
        calls.append(("lines",)) or ToolResult(True, {"line_count": 4}, "lines ready")
    )
    workflow._store_powerline_overview_with_retries = lambda _tools: (
        calls.append(("store",)) or ToolResult(True, {}, "overview stored")
    )

    workflow._refresh_powerline_overview_after_pylon_survey(tools)

    assert calls == [
        ("mapper", "start", {"reset": True, "timeout_sec": 3.0}),
        ("lines",),
        ("store",),
    ]
    assert [step["name"] for step in workflow.steps[-3:]] == [
        "restart_pl_mapper_after_pylon_survey",
        "wait_powerline_lines_after_pylon_survey",
        "refresh_powerline_overview_after_pylon_survey",
    ]


def test_pylon_survey_hands_control_to_px4_hold_before_refreshing_overview(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        px4=lambda command, **kwargs: (
            calls.append(("px4", command, kwargs))
            or ToolResult(True, {"nav_state": 4}, "Hold selected")
        )
    )
    target = {"frame_id": "world", "x": 1.0, "y": 2.0, "z": 11.0, "yaw": 0.0}
    workflow._wait_pose_at_target = lambda _tools, observed_target: (
        calls.append(("pose", observed_target))
        or ToolResult(True, {}, "pose stable in Hold")
    )

    workflow._stabilize_after_pylon_survey(tools, target)

    assert calls == [
        (
            "px4",
            "hold_direct",
            {
                "timeout_sec": workflow.args.px4_timeout_sec,
                "postcondition_timeout_sec": workflow.args.px4_timeout_sec,
                "target_system": workflow.args.px4_target_system,
                "target_component": workflow.args.px4_target_component,
            },
        ),
        ("pose", target),
    ]
    assert [step["name"] for step in workflow.steps[-2:]] == [
        "select_hold_after_pylon_survey",
        "verify_over_corridor_pose_in_hold",
    ]


def test_powerline_refresh_returns_to_visible_staging_pose_and_holds(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        activate_custom_operation=lambda **kwargs: (
            calls.append(("activate", kwargs))
            or ToolResult(True, {}, "activated")
        ),
        px4=lambda command, **kwargs: (
            calls.append(("px4", command, kwargs))
            or ToolResult(True, {}, "Hold selected")
        ),
    )
    target = {"frame_id": "world", "x": 1.0, "y": 2.0, "z": 0.6, "yaw": 0.0}
    workflow._ensure_custom_operation_mode_settable = lambda _tools, label: calls.append(("ready", label))
    workflow._fly_to_target = lambda _tools, observed_target, **kwargs: calls.append(
        ("fly", observed_target, kwargs)
    )
    workflow._wait_pose_at_target = lambda _tools, observed_target: (
        calls.append(("pose", observed_target))
        or ToolResult(True, {}, "pose stable")
    )

    workflow._return_to_powerline_staging_after_pylon_survey(tools, target)

    assert calls[0] == ("ready", "powerline_refresh")
    assert calls[1][0] == "activate"
    assert calls[2] == (
        "fly",
        target,
        {
            "operation_name": "fly_to_position",
            "start_step_name": "start_return_to_powerline_staging_after_pylons",
            "wait_step_name": "wait_return_to_powerline_staging_after_pylons",
            "pose_step_name": "wait_pose_at_powerline_staging_after_pylons",
            "ignore_altitude": True,
        },
    )
    assert calls[3][0:2] == ("px4", "hold_direct")
    assert calls[4] == ("pose", target)
    assert [step["name"] for step in workflow.steps[-3:]] == [
        "activate_custom_operation_for_powerline_refresh",
        "select_hold_at_powerline_staging_after_pylons",
        "verify_powerline_staging_pose_in_hold",
    ]


def test_staging_ftp_passes_ignore_altitude_to_operation_goal(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        start_operation=lambda operation_name, **kwargs: (
            calls.append((operation_name, kwargs))
            or ToolResult(True, {"goal_id": "staging-goal"}, "accepted")
        )
    )
    workflow._wait_fly_goal_resilient = lambda *_args, **_kwargs: ToolResult(True, {}, "complete")
    workflow._verify_or_skip_pose = lambda *_args, **_kwargs: None

    workflow._fly_to_target(
        tools,
        {"frame_id": "world", "x": 1.0, "y": 2.0, "z": 0.3, "yaw": 0.0},
        operation_name="fly_to_position",
        start_step_name="start_staging",
        wait_step_name="wait_staging",
        pose_step_name="pose_staging",
        ignore_altitude=True,
    )

    assert calls[0][0] == "fly_to_position"
    assert calls[0][1]["ignore_altitude"] is True


def test_staging_caftp_retry_passes_ignore_altitude_without_skipping_clearance(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []
    tools = SimpleNamespace(
        validate_stored_powerline_overview_against_sim_geometry=lambda **kwargs: (
            calls.append(("overview_validation", kwargs))
            or ToolResult(True, {}, "valid")
        ),
        validate_cable_aware_target_clearance=lambda **_kwargs: ToolResult(True, {}, "clear"),
        start_operation=lambda operation_name, **kwargs: (
            calls.append((operation_name, kwargs))
            or ToolResult(True, {"goal_id": "staging-caftp-goal"}, "accepted")
        ),
    )
    workflow._wait_fly_goal_resilient = lambda *_args, **_kwargs: ToolResult(True, {}, "complete")

    succeeded = workflow._try_cable_aware_fly_to_target_with_retries(
        tools,
        {"frame_id": "world", "x": 1.0, "y": 2.0, "z": 0.3, "yaw": 0.0},
        start_step_name="start_staging_caftp",
        wait_step_name="wait_staging_caftp",
        ignore_altitude=True,
    )

    assert succeeded is True
    assert calls[0] == (
        "overview_validation",
        {
            "max_line_error_m": workflow.args.max_sim_powerline_overview_line_error_m,
            "timeout_sec": workflow.args.overview_query_timeout_sec,
        },
    )
    assert calls[1][0] == "cable_aware_fly_to_position"
    assert calls[1][1]["ignore_altitude"] is True
    assert calls[1][1]["validate_sim_powerline_overview"] is False
    assert workflow.args.max_sim_powerline_overview_line_error_m == 0.75


def test_cable_aware_mission_start_uses_workflow_validation_without_direct_fallback(
    tmp_path,
):
    workflow = _workflow(tmp_path)
    target = {"frame_id": "world", "x": 1.0, "y": 2.0, "z": 0.3, "yaw": 0.0}
    calls = []
    workflow._try_cable_aware_fly_to_target_with_retries = (
        lambda observed_tools, observed_target, **kwargs: (
            calls.append((observed_tools, observed_target, kwargs)) or True
        )
    )
    workflow._verify_or_skip_pose = lambda *_args, **_kwargs: None

    tools = SimpleNamespace()
    workflow._fly_to_target(
        tools,
        target,
        operation_name="cable_aware_fly_to_position",
        start_step_name="start_mission",
        wait_step_name="wait_mission",
        pose_step_name="pose_mission",
        ignore_altitude=True,
        fallback_operation_name=None,
    )

    assert calls == [
        (
            tools,
            target,
            {
                "start_step_name": "start_mission",
                "wait_step_name": "wait_mission",
                "ignore_altitude": True,
            },
        )
    ]


def test_automation_bypass_disables_manual_input_requirement_even_if_input_was_recent(tmp_path):
    workflow = _workflow(tmp_path)
    calls = []

    def px4(command, **kwargs):
        calls.append((command, kwargs))
        if command == "get_param":
            return ToolResult(True, {"param_value": 0.0, "param_type": 6})
        return ToolResult(True, {"param_value": kwargs["param_value"]})

    workflow._configure_px4_automation_input(SimpleNamespace(px4=px4))

    assert [call[0] for call in calls] == ["get_param", "set_param"]
    assert calls[1][1]["param_name"] == "COM_RC_IN_MODE"
    assert calls[1][1]["param_value"] == 4
    assert workflow._manual_input_mode_restore == {
        "param_name": "COM_RC_IN_MODE",
        "param_value": 0,
        "param_type": 6,
    }


def test_mission_behavior_trees_never_enable_ignore_altitude():
    workspace = Path(__file__).resolve().parents[3]
    behavior_trees = workspace / "src" / "III-Drone-Mission" / "behavior_trees"

    enabled = []
    for path in behavior_trees.glob("*.xml"):
        if 'ignore_altitude="true"' in path.read_text(encoding="utf-8").lower():
            enabled.append(path.name)

    assert enabled == []
