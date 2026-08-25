from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

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


def test_definition_and_host_baseline_have_verified_content_identities(definition: dict) -> None:
    assert definition["definition_id"] == content_identity(
        {key: value for key, value in definition.items() if key != "definition_id"}
    )
    baseline = definition["host_baseline"]
    assert baseline["contract_id"] == content_identity(
        {key: value for key, value in baseline.items() if key != "contract_id"}
    )
    assert definition["sysroot"]["aircraft_derived"] is False
    assert not set(definition["host_baseline"]["owns"]) & set(definition["release_boundary"]["owns"])


def test_manifest_metadata_is_derived_from_one_definition(definition: dict) -> None:
    manifest = _json(ROOT / "deployment/tests/fixtures/release_manifest.json")
    assert manifest["target"] == manifest_target(definition)
    assert manifest["toolchain"] == manifest_toolchain(definition)
    REGISTRY.validate("release-manifest", manifest)


def test_canonical_probe_and_release_are_compatible(definition: dict, probe: dict) -> None:
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
def test_target_incompatibility_fails_closed(definition: dict, probe: dict, field: str, value: object) -> None:
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
    all_runtime = dockerfile + runtime_dockerfile + real_setup + (
        ROOT / "entrypoint_real.sh"
    ).read_text(encoding="utf-8")
    target = definition["target"]
    assert definition["images"]["target_seed"]["index_digest"] in dockerfile
    assert definition["images"]["builder"]["index_digest"] in dockerfile
    assert definition["apt_snapshot"]["uri"] in dockerfile
    assert definition["toolchain"]["compiler_package_version"] in dockerfile
    assert target["ros"]["prefix"] in all_runtime
    assert target["target_id"] in dockerfile + runtime_dockerfile
    assert "humble" not in all_runtime.lower()
    assert "/arm64-sysroot" not in all_runtime


def test_sysroot_source_and_release_boundary_forbid_aircraft_mutability(definition: dict) -> None:
    assert definition["sysroot"]["source"] == "pinned-oci-target-seed"
    assert definition["release_boundary"]["normal_deployment_may_run_package_manager"] is False
    forbidden = set(definition["release_boundary"]["must_not_bundle"])
    assert {"glibc", "dynamic-loader", "ros-jazzy", "system-python"} <= forbidden
