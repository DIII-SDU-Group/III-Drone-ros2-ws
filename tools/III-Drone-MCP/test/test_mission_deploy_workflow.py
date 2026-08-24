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
        validate_stored_powerline_overview_against_sim_geometry=lambda **_kwargs: ToolResult(True, {}, "valid"),
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
    assert calls[0][0] == "cable_aware_fly_to_position"
    assert calls[0][1]["ignore_altitude"] is True
    assert calls[0][1]["validate_sim_powerline_overview"] is False


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
