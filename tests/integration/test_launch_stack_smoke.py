from pathlib import Path

from launch import LaunchDescription

from conftest import WORKSPACE_ROOT
from helpers import load_module_from_path


def _write_config_tree(base_dir: Path):
    config_root = base_dir / "iii_drone"
    parameters_dir = config_root / "parameters"
    parameters_dir.mkdir(parents=True, exist_ok=True)

    (config_root / "ros_params.yaml").write_text(
        "/**:\n"
        "  ros__parameters:\n"
        "    parameters_path_postfix: parameters\n"
        "    sim_parameter_file: sim.yaml\n"
        "    default_parameter_file: sim.yaml\n"
    )

    (config_root / "ros_params_sim.yaml").write_text(
        "/**:\n"
        "  ros__parameters:\n"
        "    /tf/drone_frame_id: drone\n"
        "    /tf/cable_gripper_frame_id: cable_gripper\n"
        "    /tf/mmwave_frame_id: mmwave\n"
        "    /tf/sim/depth_cam_frame_id: depth_camera\n"
        "    /tf/sim/drone_to_cable_gripper: [0, 0, 0, 0, 0, 0]\n"
        "    /tf/sim/drone_to_mmwave: [0, 0, 0, 0, 0, 0]\n"
        "    /tf/sim/drone_to_depth_cam: [0, 0, 0, 0, 0, 0]\n"
    )

    (parameters_dir / "sim.yaml").write_text(
        "tf:\n"
        "  drone_frame_id:\n"
        "    value: drone\n"
        "  cable_gripper_frame_id:\n"
        "    value: cable_gripper\n"
        "  mmwave_frame_id:\n"
        "    value: mmwave\n"
        "  drone_to_cable_gripper:\n"
        "    value: [0, 0, 0, 0, 0, 0]\n"
        "  drone_to_mmwave:\n"
        "    value: [0, 0, 0, 0, 0, 0]\n"
        "  sim:\n"
        "    depth_cam_frame_id:\n"
        "      value: depth_camera\n"
        "    drone_to_cable_gripper:\n"
        "      value: [0, 0, 0, 0, 0, 0]\n"
        "    drone_to_mmwave:\n"
        "      value: [0, 0, 0, 0, 0, 0]\n"
        "    drone_to_depth_cam:\n"
        "      value: [0, 0, 0, 0, 0, 0]\n"
    )


def test_core_and_simulation_launch_descriptions_share_same_config_tree(tmp_path, monkeypatch):
    _write_config_tree(tmp_path)
    monkeypatch.setenv("CONFIG_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SIMULATION", "1")

    core_module = load_module_from_path(WORKSPACE_ROOT / "src/III-Drone-Core/launch/iii_drone.launch.py")
    simulation_module = load_module_from_path(WORKSPACE_ROOT / "src/III-Drone-Simulation/launch/tf_sim.launch.py")

    monkeypatch.setattr(core_module.os, "popen", lambda _cmd: type("Reader", (), {"read": staticmethod(lambda: "")})())

    core_description = core_module.generate_launch_description()
    simulation_description = simulation_module.generate_launch_description()

    assert isinstance(core_description, LaunchDescription)
    assert isinstance(simulation_description, LaunchDescription)
    assert len(core_description.entities) >= 10
    assert len(simulation_description.entities) == 5
