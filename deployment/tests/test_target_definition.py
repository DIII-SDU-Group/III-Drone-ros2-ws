from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from iii_deployment.contracts import ContractError, ContractRegistry, content_identity
from iii_deployment.target import (
    load_target_definition,
    manifest_target,
    manifest_toolchain,
    verify_release_target,
    verify_target_probe,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
DEFINITION_PATH = ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def definition() -> dict:
    return load_target_definition(DEFINITION_PATH, REGISTRY)


@pytest.fixture
def probe() -> dict:
    return _json(ROOT / "deployment/tests/fixtures/target_abi_probe.json")


def test_definition_and_host_baseline_have_verified_content_identities(
    definition: dict,
) -> None:
    assert definition["definition_id"] == content_identity(
        {key: value for key, value in definition.items() if key != "definition_id"}
    )
    baseline = definition["host_baseline"]
    assert baseline["contract_id"] == content_identity(
        {key: value for key, value in baseline.items() if key != "contract_id"}
    )
    assert definition["sysroot"]["aircraft_derived"] is False
    sysroot = definition["sysroot"]
    assert sysroot["content_id"] == content_identity(
        {key: value for key, value in sysroot.items() if key != "content_id"}
    )
    assert not set(definition["host_baseline"]["owns"]) & set(
        definition["release_boundary"]["owns"]
    )
    package_names = {
        item["name"] for item in definition["host_baseline"]["package_constraints"]
    }
    assert "libarmadillo12" in package_names
    assert "libopencv-core406t64" in package_names
    assert "libopencv-core4.6t64" not in package_names


def test_manifest_metadata_is_derived_from_one_definition(definition: dict) -> None:
    manifest = _json(ROOT / "deployment/tests/fixtures/release_manifest.json")
    assert manifest["target"] == manifest_target(definition)
    assert manifest["toolchain"] == manifest_toolchain(definition)
    REGISTRY.validate("release-manifest", manifest)


def test_canonical_probe_and_release_are_compatible(
    definition: dict, probe: dict
) -> None:
    verify_target_probe(definition, probe, REGISTRY)
    manifest = _json(ROOT / "deployment/tests/fixtures/release_manifest.json")
    verify_release_target(manifest, definition, probe, REGISTRY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("architecture", "x86_64"),
        ("os_version", "22.04"),
        ("ros", "humble"),
        ("python_abi", "cp310"),
        ("python_soabi", "cpython-310-aarch64-linux-gnu"),
        ("libc_version", "2.38"),
        ("compiler_version", "12.3.0"),
    ],
)
def test_target_incompatibility_fails_closed(
    definition: dict, probe: dict, field: str, value: object
) -> None:
    incompatible = deepcopy(probe)
    incompatible[field] = value
    with pytest.raises(ContractError, match=field):
        verify_target_probe(definition, incompatible, REGISTRY)


def test_definition_identity_tampering_is_rejected(tmp_path: Path) -> None:
    value = _json(DEFINITION_PATH)
    value["images"]["builder"]["platform_digest"] = "sha256:" + "0" * 64
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="content identity"):
        load_target_definition(path, REGISTRY)


def test_build_and_real_runtime_files_match_the_definition(definition: dict) -> None:
    dockerfile = (ROOT / "Dockerfile.cc").read_text(encoding="utf-8")
    runtime_dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    real_setup = (ROOT / "setup/setup_real.bash").read_text(encoding="utf-8")
    all_runtime = (
        dockerfile
        + runtime_dockerfile
        + real_setup
        + (ROOT / "entrypoint_real.sh").read_text(encoding="utf-8")
    )
    target = definition["target"]
    assert definition["images"]["target_seed"]["index_digest"] in dockerfile
    assert definition["images"]["builder"]["index_digest"] in dockerfile
    assert definition["apt_snapshot"]["uri"] in dockerfile
    assert definition["sysroot"]["ros_snapshot"]["uri"] in dockerfile
    assert definition["sysroot"]["ros_snapshot"]["key_sha256"] in dockerfile
    for package in definition["sysroot"]["packages"]:
        assert package["name"] in dockerfile
        assert package["version"] in dockerfile
    assert definition["toolchain"]["compiler_package_version"] in dockerfile
    assert definition["toolchain"]["ccache_version"] in dockerfile
    assert "CMAKE_CXX_COMPILER_LAUNCHER" in (
        ROOT / "cc_ws/arm64-toolchain.cmake"
    ).read_text(encoding="utf-8")
    assert target["ros"]["prefix"] in all_runtime
    assert target["target_id"] in dockerfile + runtime_dockerfile
    assert "humble" not in all_runtime.lower()
    assert "/arm64-sysroot" not in all_runtime


def test_sysroot_source_and_release_boundary_forbid_aircraft_mutability(
    definition: dict,
) -> None:
    assert definition["sysroot"]["source"] == "pinned-oci-seed-plus-snapshots"
    assert (
        definition["release_boundary"]["normal_deployment_may_run_package_manager"]
        is False
    )
    forbidden = set(definition["release_boundary"]["must_not_bundle"])
    assert {"glibc", "dynamic-loader", "ros-jazzy", "system-python"} <= forbidden


def test_production_ansible_baseline_is_derived_from_target_definition(
    definition: dict,
) -> None:
    variables = yaml.safe_load(
        (ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml").read_text(
            encoding="utf-8"
        )
    )
    target = definition["target"]
    assert variables["iii_target_definition_id"] == definition["definition_id"]
    assert variables["iii_baseline_id"] == definition["host_baseline"]["contract_id"]
    assert variables["iii_target_definition_id"] != variables["iii_baseline_id"]
    assert variables["iii_expected_release"] == target["os_version"]
    assert variables["iii_expected_codename"] == target["os_codename"]
    assert variables["iii_expected_architecture"] == target["architecture"]
    assert variables["iii_ubuntu_snapshot"] == definition["apt_snapshot"]["uri"]
    assert variables["iii_ros_snapshot"] == definition["sysroot"]["ros_snapshot"]["uri"]
    assert (
        variables["iii_ros_key_sha256"]
        == definition["sysroot"]["ros_snapshot"]["key_sha256"]
    )
    assert (
        variables["iii_ros_key_fingerprint"]
        == definition["sysroot"]["ros_snapshot"]["key_fingerprint"]
    )
    exact_ros_packages = {item for item in variables["iii_ros_packages"] if "=" in item}
    assert exact_ros_packages == {
        f"{package['name']}={package['version']}"
        for package in definition["sysroot"]["packages"]
    }
