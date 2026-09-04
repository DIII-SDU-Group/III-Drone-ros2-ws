from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from iii_deployment.contracts import ContractRegistry, canonical_json, content_identity
from iii_deployment.px4_release import (
    PX4ReleaseError,
    audit_release,
    load_dds_contract,
    load_firmware_spec,
    normalized_dds_topics,
    prepare_release_media,
    validate_release_inputs,
)
from iii_deployment.px4_network import render_extras, render_net_cfg
from iii_deployment.px4_inspection import PX4ReleaseInspector


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")


def documents():
    return (
        load_firmware_spec(ROOT / "deployment/px4/firmware.json", REGISTRY),
        load_dds_contract(ROOT / "deployment/px4/dds-topics.json", REGISTRY),
        json.loads((ROOT / "deployment/px4/network-baseline.json").read_text()),
        json.loads((ROOT / "deployment/px4/real.json").read_text()),
    )


def test_generated_px4_release_contract_matches_source():
    spec, dds, network, parameters = documents()
    assert normalized_dds_topics(
        ROOT / "PX4-Autopilot/src/modules/uxrce_dds_client/dds_topics.yaml",
        firmware_commit=spec["git_commit"],
    ) == dds
    validate_release_inputs(
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        registry=REGISTRY,
    )
    assert len(dds["publications"]) == 30
    assert len(dds["subscriptions"]) == 28
    assert dds["subscriptions_multi"] == []


def test_dds_normalization_rejects_duplicates(tmp_path: Path):
    source = tmp_path / "dds_topics.yaml"
    source.write_text(
        "publications:\n- &row {topic: /fmu/out/x, type: 'px4_msgs::msg::X'}\n- *row\nsubscriptions: []\nsubscriptions_multi: []\n"
    )
    with pytest.raises(PX4ReleaseError, match="duplicates"):
        normalized_dds_topics(source, firmware_commit="a" * 40)


def test_exact_release_input_binding_rejects_parameter_manifest_drift():
    spec, dds, network, parameters = documents()
    parameters["manifest_id"] = "0" * 64
    with pytest.raises(PX4ReleaseError, match="one exact release"):
        validate_release_inputs(
            spec=spec,
            dds=dds,
            network=network,
            parameters=parameters,
            registry=REGISTRY,
        )


def snapshot(parameters, *, complete=True):
    rows = [
        {"name": item["name"], "mav_type": item["mav_type"], "value": item["value"], "index": index}
        for index, item in enumerate(parameters["parameters"])
        if item["value"] is not None
    ]
    return {
        "snapshot_id": content_identity(rows),
        "complete": complete,
        "parameters": rows,
    }


def comparison(parameters, observed):
    return {
        "manifest_id": parameters["manifest_id"],
        "snapshot_id": observed["snapshot_id"],
        "required_match": True,
    }


def test_px4_release_audit_accepts_exact_zero_write_observation():
    spec, dds, network, parameters = documents()
    observed = snapshot(parameters)
    result = audit_release(
        release_id="a" * 64,
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        status={
            "connected": True,
            "armed": False,
            "firmware_version": spec["version"],
            "firmware_commit": spec["advertised_commit"],
        },
        snapshot=observed,
        comparison=comparison(parameters, observed),
        provenance="receiver-px4-ethernet",
        network_artifacts={
            network["artifacts"]["net_cfg_path"]: render_net_cfg(network),
            network["artifacts"]["extras_path"]: render_extras(network),
        },
    )
    assert result["healthy"] is True
    assert result["findings"] == []
    assert result["writes_performed"] == 0
    assert result["dds_topics_observation"] == "proven-by-exact-firmware-commit"


def test_receiver_ethernet_parameter_snapshot_provenance_is_contract_valid():
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    target = {
        "system_id": 1,
        "component_id": 1,
        "armed": False,
        "firmware_version": "1.16.1",
        "firmware_commit": "7f41496535",
    }
    rows = [{"name": "SYS_AUTOSTART", "mav_type": "INT32", "value": 4001, "index": 0}]
    snapshot = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": content_identity({"profile": "real", "target": target, "parameter_count": 1, "parameters": rows}),
        "captured_at": "2026-09-03T12:00:00Z",
        "profile": "real",
        "provenance": "receiver-px4-ethernet",
        "target": target,
        "complete": True,
        "parameter_count": 1,
        "parameters": rows,
    }
    registry.validate("px4-parameter-snapshot", snapshot)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "PX4_UNREACHABLE"),
        ({"connected": True, "armed": True, "firmware_version": "1.16.1", "firmware_commit": "7f41496535"}, "PX4_ARMED"),
        ({"connected": True, "armed": False, "firmware_version": "1.15.0", "firmware_commit": "7f41496535"}, "PX4_VERSION_MISMATCH"),
        ({"connected": True, "armed": False, "firmware_version": "1.16.1", "firmware_commit": "a0e6f9cd70"}, "PX4_COMMIT_MISMATCH"),
    ],
)
def test_px4_release_audit_classifies_identity_failures(status, expected):
    spec, dds, network, parameters = documents()
    result = audit_release(
        release_id="a" * 64,
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        status=status,
        snapshot=None,
        comparison=None,
        provenance="receiver-px4-ethernet",
        network_artifacts={
            network["artifacts"]["net_cfg_path"]: render_net_cfg(network),
            network["artifacts"]["extras_path"]: render_extras(network),
        },
    )
    assert result["healthy"] is False
    assert expected in {item["code"] for item in result["findings"]}
    assert result["writes_performed"] == 0


def test_px4_release_audit_classifies_network_and_parameter_drift():
    spec, dds, network, parameters = documents()
    observed = snapshot(parameters)
    required = next(iter(network["parameter_requirements"]))
    next(item for item in observed["parameters"] if item["name"] == required)["value"] = -1
    result = audit_release(
        release_id="a" * 64,
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        status={"connected": True, "armed": False, "firmware_version": spec["version"], "firmware_commit": spec["advertised_commit"]},
        snapshot=observed,
        comparison={**comparison(parameters, observed), "required_match": False},
        provenance="receiver-px4-ethernet",
        network_artifacts={
            network["artifacts"]["net_cfg_path"]: render_net_cfg(network),
            network["artifacts"]["extras_path"]: render_extras(network),
        },
    )
    assert {item["code"] for item in result["findings"]} == {
        "PX4_PARAMETER_MISMATCH",
        "PX4_NETWORK_MISMATCH",
    }
    assert result["writes_performed"] == 0


def test_px4_release_audit_rejects_operator_tunable_drift():
    spec, dds, network, parameters = documents()
    observed = snapshot(parameters)
    compared = comparison(parameters, observed)
    compared["drift"] = {
        "release-required": [],
        "operator-tunable": [{"name": "NAV_ACC_RAD", "expected": 10.0, "observed": 11.0}],
    }
    result = audit_release(
        release_id="a" * 64,
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        status={"connected": True, "armed": False, "firmware_version": spec["version"], "firmware_commit": spec["advertised_commit"]},
        snapshot=observed,
        comparison=compared,
        provenance="receiver-px4-ethernet",
        network_artifacts={
            network["artifacts"]["net_cfg_path"]: render_net_cfg(network),
            network["artifacts"]["extras_path"]: render_extras(network),
        },
    )
    assert {item["code"] for item in result["findings"]} == {"PX4_PARAMETER_MISMATCH"}


def test_prepare_release_media_is_exact_atomic_and_keeps_calibration_out(tmp_path: Path):
    spec, dds, network, parameters = documents()
    firmware = tmp_path / spec["build"]["artifact"]
    firmware.write_bytes(b"verified firmware")
    import hashlib

    body = {
        "schema": "iii.px4-firmware-build/v1",
        "spec_id": spec["spec_id"],
        "cache_key": "8" * 64,
        "firmware": {
            "filename": firmware.name,
            "sha256": hashlib.sha256(firmware.read_bytes()).hexdigest(),
            "bytes": firmware.stat().st_size,
            "magic": "PX4FWv1",
            "board_id": 53,
            "git_identity": "v1.16.1-2-g" + spec["git_commit"][:10],
        },
        "source": {
            "git_commit": spec["git_commit"],
            "git_describe": "v1.16.1-2-g" + spec["git_commit"][:10],
            "submodules_sha256": "9" * 64,
            "dds_topics_id": dds["contract_id"],
        },
        "toolchain": {
            "compiler": "arm-none-eabi-gcc",
            "compiler_version": "13.2.1",
            "compiler_sha256": "7" * 64,
        },
        "cache_hit": False,
    }
    build = {
        "build_id": content_identity(
            {key: value for key, value in body.items() if key != "cache_hit"}
        ),
        **body,
    }
    record = tmp_path / "px4-firmware-build.json"
    record.write_bytes(canonical_json(build) + b"\n")
    destination = tmp_path / "media"
    result = prepare_release_media(
        destination=destination,
        firmware_path=firmware,
        build_record_path=record,
        resource_root=ROOT / "deployment/px4",
        schema_root=ROOT / "deployment/schemas/v1",
    )
    assert result["git_commit"] == spec["git_commit"]
    assert result["writes_performed"] == 0
    assert (destination / "microSD/net.cfg").read_bytes() == render_net_cfg(network)
    assert (destination / "microSD/etc/extras.txt").read_bytes() == render_extras(network)
    exported = (destination / "parameters/real.params").read_text()
    calibration_names = {
        item["name"] for item in parameters["parameters"]
        if item["classification"] == "calibration-identity"
    }
    assert calibration_names.isdisjoint(exported.split())
    with pytest.raises(PX4ReleaseError, match="already exists"):
        prepare_release_media(
            destination=destination,
            firmware_path=firmware,
            build_record_path=record,
            resource_root=ROOT / "deployment/px4",
            schema_root=ROOT / "deployment/schemas/v1",
        )


def test_receiver_inspector_binds_staged_release_and_returns_activation_evidence(
    tmp_path: Path, monkeypatch
):
    spec, dds, network, parameters = documents()
    release_root = tmp_path / "release"
    resources = release_root / "install/share/iii-deployment/px4"
    resources.mkdir(parents=True)
    for name in ("firmware.json", "dds-topics.json", "network-baseline.json", "real.json", "sim.json"):
        shutil.copyfile(ROOT / "deployment/px4" / name, resources / name)
    manifest = {
        "release_id": "a" * 64,
        "px4": {
            "spec_id": spec["spec_id"],
            "dds_topics_id": dds["contract_id"],
            "network_baseline_id": network["baseline_id"],
            "manifest_ids": {"real": parameters["manifest_id"]},
        },
    }
    (release_root / "release-manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    target = {
        "system_id": 1,
        "component_id": 1,
        "armed": False,
        "firmware_version": spec["version"],
        "firmware_commit": spec["advertised_commit"],
    }
    rows = [
        {"name": item["name"], "mav_type": item["mav_type"], "value": item["value"], "index": index}
        for index, item in enumerate(parameters["parameters"])
        if item["value"] is not None
    ]
    snap = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": content_identity(
            {"profile": "real", "target": target, "parameter_count": len(rows), "parameters": rows}
        ),
        "captured_at": "2026-09-03T12:00:00Z",
        "profile": "real",
        "provenance": "receiver-px4-ethernet",
        "target": target,
        "complete": True,
        "parameter_count": len(rows),
        "parameters": rows,
    }
    compare = {
        "schema": "iii.px4-parameter-comparison/v1",
        "profile": "real",
        "manifest_id": parameters["manifest_id"],
        "snapshot_id": snap["snapshot_id"],
        "inventory_complete": True,
        "missing": [],
        "unexpected": [],
        "drift": {"release-required": [], "operator-tunable": []},
        "preserved_calibration_identity": [],
        "required_match": True,
    }

    class Adapter:
        def __init__(self, *_args, **_kwargs): pass
        def status(self): return target | {"connected": True}
        def read_text_file(self, path):
            return render_net_cfg(network) if path == network["artifacts"]["net_cfg_path"] else render_extras(network)

    class Store:
        def __init__(self, **_kwargs): pass
        def pull(self, *_args, **_kwargs): return snap
        def compare(self, *_args, **_kwargs): return compare

    monkeypatch.setattr("iii_deployment.px4_inspection.MavlinkParameterAdapter", Adapter)
    monkeypatch.setattr("iii_deployment.px4_inspection.PX4ParameterStore", Store)
    result = PX4ReleaseInspector(
        schema_root=ROOT / "deployment/schemas/v1", state_root=tmp_path / "state"
    ).audit(release_id="a" * 64, release_root=release_root)
    assert result["audit"]["healthy"] is True
    assert result["activation_evidence"]["healthy"] is True
    assert result["activation_evidence"]["writes_performed"] == 0
    retained = tmp_path / "state" / f"{result['audit']['audit_id']}.json"
    assert json.loads(retained.read_text()) == result

    class NetworkBlockedAdapter:
        def __init__(self, *_args, **_kwargs):
            raise OSError(97, "Address family not supported by protocol")

    monkeypatch.setattr(
        "iii_deployment.px4_inspection.MavlinkParameterAdapter",
        NetworkBlockedAdapter,
    )
    unavailable = PX4ReleaseInspector(
        schema_root=ROOT / "deployment/schemas/v1",
        state_root=tmp_path / "unavailable-state",
    ).audit(release_id="a" * 64, release_root=release_root)
    assert unavailable["audit"]["healthy"] is False
    assert unavailable["audit"]["findings"][0]["code"] == "PX4_UNREACHABLE"
    assert unavailable["audit"]["writes_performed"] == 0

    class ArtifactReadBlockedAdapter(Adapter):
        def read_text_file(self, _path):
            from iii_deployment.px4_parameters import PX4ParameterError

            raise PX4ParameterError("PX4 SD configuration read timed out")

    monkeypatch.setattr(
        "iii_deployment.px4_inspection.MavlinkParameterAdapter",
        ArtifactReadBlockedAdapter,
    )
    blocked = PX4ReleaseInspector(
        schema_root=ROOT / "deployment/schemas/v1",
        state_root=tmp_path / "artifact-read-blocked-state",
    ).audit(release_id="a" * 64, release_root=release_root)
    assert blocked["audit"]["healthy"] is False
    assert {item["code"] for item in blocked["audit"]["findings"]} == {
        "PX4_NETWORK_ARTIFACTS_UNVERIFIED"
    }
    assert blocked["audit"]["writes_performed"] == 0
