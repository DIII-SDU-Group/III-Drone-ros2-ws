import shutil
from pathlib import Path

import yaml
from launch_ros.actions import Node

from conftest import WORKSPACE_ROOT
from helpers import load_module_from_path

from iii_drone_configuration._native import NativeConfiguratorCore


CONFIG_PACKAGE_ROOT = WORKSPACE_ROOT / "src/III-Drone-Configuration"
CONFIG_SOURCE_DIR = CONFIG_PACKAGE_ROOT / "config"
PARAMETER_MANIFEST = CONFIG_SOURCE_DIR / "parameters" / "parameter_manifest.yaml"


def _load_runtime_parameters(path: Path) -> dict[str, object]:
    with path.open("r") as file:
        return (yaml.safe_load(file) or {}).get("/**", {}).get("ros__parameters", {})


def _copy_workspace_config_tree(destination_root: Path) -> Path:
    iii_config_dir = destination_root / "iii_drone"
    iii_config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONFIG_SOURCE_DIR / "ros_params_real.yaml", iii_config_dir / "ros_params_real.yaml")
    shutil.copy2(CONFIG_SOURCE_DIR / "ros_params_sim.yaml", iii_config_dir / "ros_params_sim.yaml")
    shutil.copytree(CONFIG_SOURCE_DIR / "parameters", iii_config_dir / "parameters", dirs_exist_ok=True)
    return iii_config_dir


def _configuration_server_param_file(description) -> str:
    nodes = [entity for entity in description.entities if isinstance(entity, Node)]
    server = next(
        node
        for node in nodes
        if node._Node__package == "iii_drone_core" and node._Node__node_executable == "configuration_server_node.py"
    )
    parameter_file = server._Node__parameters[0]
    return parameter_file.param_file[0].text


def test_production_runtime_parameter_files_cover_managed_schema():
    managed_names = set(NativeConfiguratorCore(str(PARAMETER_MANIFEST)).schema_parameter_names())
    real_params = _load_runtime_parameters(CONFIG_SOURCE_DIR / "ros_params_real.yaml")
    sim_params = _load_runtime_parameters(CONFIG_SOURCE_DIR / "ros_params_sim.yaml")

    missing_real = sorted(managed_names - set(real_params))
    missing_sim = sorted(managed_names - set(sim_params))

    assert not missing_real, f"ros_params_real.yaml is missing managed keys: {missing_real[:10]}"
    assert not missing_sim, f"ros_params_sim.yaml is missing managed keys: {missing_sim[:10]}"


def test_core_launch_selects_mode_specific_configuration_file(tmp_path, monkeypatch):
    iii_config_dir = _copy_workspace_config_tree(tmp_path)
    core_module = load_module_from_path(WORKSPACE_ROOT / "src/III-Drone-Core/launch/iii_drone.launch.py")

    monkeypatch.setenv("CONFIG_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(core_module.os, "popen", lambda _cmd: type("Reader", (), {"read": staticmethod(lambda: "")})())

    monkeypatch.delenv("SIMULATION", raising=False)
    real_description = core_module.generate_launch_description()
    assert _configuration_server_param_file(real_description) == str(iii_config_dir / "ros_params_real.yaml")

    monkeypatch.setenv("SIMULATION", "true")
    sim_description = core_module.generate_launch_description()
    assert _configuration_server_param_file(sim_description) == str(iii_config_dir / "ros_params_sim.yaml")
