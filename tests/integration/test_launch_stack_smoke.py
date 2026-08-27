from launch import LaunchDescription

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
    assert len(simulation_description.entities) == 5
