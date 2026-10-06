import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node

from conftest import WORKSPACE_ROOT
from helpers import load_module_from_path

STATIC_TF = ("tf2_ros", "static_transform_publisher")
WORLD_TO_DRONE = [
    ("iii_drone_core", "drone_frame_broadcaster"),
    ("iii_drone_simulation", "ground_truth_frame_broadcaster"),
]
STATIC_TF_FLAGS = [
    "--x", "--y", "--z", "--yaw", "--pitch", "--roll",
    "--frame-id", "--child-frame-id",
]


def _use_temporary_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("III_OPERATIONS_ROOT", str(tmp_path / "operations"))
    monkeypatch.setenv("SIMULATION", "true")


def _load_simulation_module():
    return load_module_from_path(
        WORKSPACE_ROOT / "src/III-Drone-Simulation/launch/tf_sim.launch.py"
    )


def _active_parameter_set(simulation_module):
    with open(simulation_module._resolve_ros_params_file(), "r") as file:
        return yaml.safe_load(file)


def _static_transforms(simulation_description):
    """Check the description's layout and return its static transforms.

    Launch arguments (log level, odometry source, world->drone ownership), then
    the named-argument sensor transforms of the sensor layout and the mutually
    exclusive PX4/ground-truth world->drone publishers. Each transform is
    returned as its {flag: value} arguments.
    """
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
    static_count = len(nodes) - len(WORLD_TO_DRONE)
    assert [
        (node._Node__package, node._Node__node_executable) for node in nodes
    ] == [STATIC_TF] * static_count + WORLD_TO_DRONE
    assert all(node.condition is not None for node in nodes[static_count:])
    transforms = []
    for static_tf in nodes[:static_count]:
        # Launch-time arguments are plain strings until the action executes.
        arguments = list(static_tf._Node__arguments)
        assert all(isinstance(argument, str) for argument in arguments)
        assert arguments[0::2] == STATIC_TF_FLAGS
        transforms.append(dict(zip(arguments[0::2], arguments[1::2])))
    return transforms


def _frames(transforms):
    return [(t["--frame-id"], t["--child-frame-id"]) for t in transforms]


def test_core_and_simulation_launch_descriptions_share_same_config_tree(
    tmp_path, monkeypatch
):
    _use_temporary_config(tmp_path, monkeypatch)

    core_module = load_module_from_path(
        WORKSPACE_ROOT / "src/III-Drone-Core/launch/iii_drone.launch.py"
    )
    simulation_module = _load_simulation_module()

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

    params = _active_parameter_set(simulation_module)["/**"]["ros__parameters"]
    assert params["/tf/sim/sensor_layout"] == "d4s_dc_drone"
    drone = params["/tf/drone_frame_id"]
    # The default layout mounts the cable gripper, the upward radar, the depth
    # camera and the cable camera.
    assert _frames(_static_transforms(simulation_description)) == [
        (drone, params["/tf/cable_gripper_frame_id"]),
        (drone, params["/tf/mmwave_frame_id"]),
        (drone, params["/tf/sim/depth_cam_frame_id"]),
        (drone, params["/tf/cable_camera_frame_id"]),
    ]


def test_powerline_eval_layout_adds_the_forward_radar(tmp_path, monkeypatch):
    _use_temporary_config(tmp_path, monkeypatch)
    simulation_module = _load_simulation_module()

    parameter_set = _active_parameter_set(simulation_module)
    params = parameter_set["/**"]["ros__parameters"]
    params["/tf/sim/sensor_layout"] = "d4s_dc_drone_powerline_eval"
    parameter_file = tmp_path / "powerline_eval.yaml"
    parameter_file.write_text(yaml.safe_dump(parameter_set))
    monkeypatch.setenv("III_SYSTEM_PARAMETER_FILE", str(parameter_file))

    transforms = _static_transforms(simulation_module.generate_launch_description())

    drone = params["/tf/drone_frame_id"]
    # The powerline SLAM evaluation layout keeps the four sensor transforms,
    # mounts the cable camera at its own pitch and adds the forward radar.
    assert _frames(transforms) == [
        (drone, params["/tf/cable_gripper_frame_id"]),
        (drone, params["/tf/mmwave_frame_id"]),
        (drone, params["/tf/sim/depth_cam_frame_id"]),
        (drone, params["/tf/cable_camera_frame_id"]),
        (drone, params["/tf/mmwave_forward_frame_id"]),
    ]
    camera_mount = params["/tf/sim/powerline_eval/drone_to_cable_camera"]
    assert transforms[3]["--pitch"] == str(camera_mount[4])
    assert transforms[3]["--pitch"] != str(params["/tf/sim/drone_to_cable_camera"][4])
