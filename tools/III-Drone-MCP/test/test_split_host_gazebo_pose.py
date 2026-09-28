from types import MethodType

from iii_drone_mcp.agent_tools import DroneAgentTools, ToolResult


def _bare_tools() -> DroneAgentTools:
    return object.__new__(DroneAgentTools)


def test_gazebo_pose_uses_bridged_ground_truth_when_gz_is_unavailable() -> None:
    tools = _bare_tools()
    expected = {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.4, "source": "bridge"}

    def gazebo(_self, _command, **_kwargs):
        raise FileNotFoundError("gz")

    tools.gazebo = MethodType(gazebo, tools)
    tools._lookup_simulation_ground_truth_drone_pose = MethodType(
        lambda _self, **_kwargs: dict(expected), tools
    )

    assert tools._lookup_gazebo_drone_model_pose(timeout_sec=0.1) == expected


def test_gazebo_pose_prefers_local_gz_result() -> None:
    tools = _bare_tools()
    stdout = '''pose {
name: "d4s_dc_drone_0"
position { x: 1.0 y: 2.0 z: 3.0 }
orientation { x: 0.0 y: 0.0 z: 0.0 w: 1.0 }
}'''
    tools.gazebo = MethodType(
        lambda _self, _command, **_kwargs: ToolResult(True, {"stdout": stdout}), tools
    )
    tools._lookup_simulation_ground_truth_drone_pose = MethodType(
        lambda _self, **_kwargs: (_ for _ in ()).throw(AssertionError("fallback should not run")),
        tools,
    )

    pose = tools._lookup_gazebo_drone_model_pose(timeout_sec=0.1)
    assert pose == {
        "x": 1.0,
        "y": 2.0,
        "z": 3.0,
        "yaw": 0.0,
        "position": {"x": 1.0, "y": 2.0, "z": 3.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }
