from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node

from conftest import WORKSPACE_ROOT
from helpers import load_module_from_path


def test_core_and_simulation_launch_descriptions_share_same_config_tree(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONFIG_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("III_OPERATIONS_ROOT", str(tmp_path / "operations"))
    monkeypatch.setenv("SIMULATION", "true")

    core_module = load_module_from_path(
        WORKSPACE_ROOT / "src/III-Drone-Core/launch/iii_drone.launch.py"
    )
    simulation_module = load_module_from_path(
        WORKSPACE_ROOT / "src/III-Drone-Simulation/launch/tf_sim.launch.py"
    )

    monkeypatch.setattr(
        core_module.os,
        "popen",
        lambda _cmd: type("Reader", (), {"read": staticmethod(lambda: "")})(),
    )

    core_description = core_module.generate_launch_description()
    simulation_description = simulation_module.generate_launch_description()

    assert isinstance(core_description, LaunchDescription)
    assert isinstance(simulation_description, LaunchDescription)
    assert len(core_description.entities) >= 10
    # Launch arguments (log level, odometry source, world->drone ownership),
    # then the three named-argument sensor transforms and the mutually
    # exclusive PX4/ground-truth world->drone publishers.
    entities = simulation_description.entities
    assert [
        entity.name for entity in entities if isinstance(entity, DeclareLaunchArgument)
    ] == [
        "drone_frame_broadcaster_log_level",
        "use_ground_truth_odometry",
        "publish_world_to_drone",
    ]
    nodes = [entity for entity in entities if isinstance(entity, Node)]
    assert len(nodes) + 3 == len(entities)
    assert [
        (node._Node__package, node._Node__node_executable) for node in nodes
    ] == [
        ("tf2_ros", "static_transform_publisher"),
        ("tf2_ros", "static_transform_publisher"),
        ("tf2_ros", "static_transform_publisher"),
        ("iii_drone_core", "drone_frame_broadcaster"),
        ("iii_drone_simulation", "ground_truth_frame_broadcaster"),
    ]
    for static_tf in nodes[:3]:
        # Launch-time arguments are plain strings until the action executes.
        flags = [
            argument
            for argument in static_tf._Node__arguments
            if isinstance(argument, str) and argument.startswith("--")
        ]
        assert flags == [
            "--x", "--y", "--z", "--yaw", "--pitch", "--roll",
            "--frame-id", "--child-frame-id",
        ]
    assert all(node.condition is not None for node in nodes[3:])
