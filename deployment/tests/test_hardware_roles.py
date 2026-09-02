from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry, content_identity
from iii_deployment.hardware_roles import (
    generate_udev_rules,
    inspect_hardware,
    load_manifest,
    validate_commissioning_sequence,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ContractRegistry(ROOT / "schemas/v1")
MANIFEST_PATH = ROOT / "hardware/shared-hardware-role-manifest.json"
RULES_PATH = ROOT / "hardware/90-iii-hardware-roles.rules"


def _device(
    name: str,
    *,
    subsystem: str = "tty",
    vendor: str | None = None,
    product: str | None = None,
    interface: str | None = None,
    serial: str | None = None,
    port: str = "pci-0000:01:00.0-usb-0:1",
    v4l_index: str | None = None,
    capabilities: str | None = None,
) -> dict:
    value = {
        "subsystem": subsystem,
        "device_node": f"/dev/{name}",
        "sysfs_path": f"/sys/devices/{port}/{name}",
        "id_bus": "usb",
        "vendor_id": vendor,
        "product_id": product,
        "serial": serial,
        "interface_number": interface,
        "id_path": port,
        "driver": "usbserial" if subsystem == "tty" else "uvcvideo",
        "v4l_index": v4l_index,
        "v4l_capabilities": capabilities,
        "product_name": "fixture-device",
    }
    value["device_id"] = content_identity(value)
    return value


def _exact_devices(*, port_suffix: str = "") -> list[dict]:
    return [
        _device(
            "video0",
            subsystem="video4linux",
            port="usb-camera" + port_suffix,
            v4l_index="0",
            capabilities=":capture:",
        ),
        _device(
            "ttyACM0", vendor="2341", product="8054", port="usb-charger" + port_suffix
        ),
        _device(
            "ttyUSB0",
            vendor="10c4",
            product="ea70",
            interface="00",
            port="usb-mmwave" + port_suffix,
        ),
        _device(
            "ttyUSB1",
            vendor="10c4",
            product="ea70",
            interface="01",
            port="usb-mmwave" + port_suffix,
        ),
    ]


def _inspect(manifest: dict, devices: list[dict], *, boot: str = "boot-a") -> dict:
    return inspect_hardware(
        manifest,
        devices,
        profile="real",
        boot_id=boot,
        captured_monotonic_ns=1,
    )


def test_manifest_schema_identity_and_generated_rule_golden() -> None:
    manifest = load_manifest(MANIFEST_PATH, REGISTRY)
    assert manifest["inventory_mode"] == "shared-hardware-class"
    assert [item["role"] for item in manifest["roles"]] == sorted(
        item["role"] for item in manifest["roles"]
    )
    assert all(item["match"]["serial_allowlist"] == [] for item in manifest["roles"])
    assert generate_udev_rules(manifest) == RULES_PATH.read_bytes()
    assert b"/dev/video0" not in generate_udev_rules(manifest)
    assert "fmu" not in manifest["requirements"]["required"]
    assert all(item["role"] != "fmu" for item in manifest["roles"])
    metadata = json.loads((ROOT / "release-metadata.json").read_text())
    for profile in metadata["profiles"]:
        if profile["id"] in {"real", "opti_track"}:
            assert profile["health"]["required_hardware_roles"] == manifest[
                "requirements"
            ]["required"]


def test_ansible_installs_one_source_manifest_and_does_not_preempt_retirement() -> None:
    tasks = (ROOT / "ansible/roles/hardware_baseline/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    assert tasks.count("shared-hardware-role-manifest.json") == 1
    assert tasks.count("90-iii-hardware-roles.rules") == 2
    assert "/etc/iii/hardware-role-manifest.json" in tasks
    assert "/etc/udev/rules.d/90-iii-hardware-roles.rules" in tasks
    assert "99-diii-usb.rules" not in tasks


def test_exact_missing_ambiguous_duplicate_and_changed_port_resolution() -> None:
    manifest = load_manifest(MANIFEST_PATH, REGISTRY)
    exact = _inspect(manifest, _exact_devices())
    REGISTRY.validate("hardware-inspection", exact)
    assert exact["accepted"] is True
    assert all(item["state"] == "present" for item in exact["roles"].values())

    missing = _inspect(manifest, _exact_devices()[1:])
    assert missing["accepted"] is False
    assert missing["roles"]["cable_camera"]["state"] == "missing"

    duplicate = _exact_devices()
    duplicate.append(
        _device("ttyUSB9", vendor="2341", product="8054", port="usb-second-charger")
    )
    ambiguous = _inspect(manifest, duplicate)
    assert ambiguous["accepted"] is False
    assert ambiguous["roles"]["charger_gripper"]["state"] == "ambiguous"
    assert len(ambiguous["roles"]["charger_gripper"]["matched_device_ids"]) == 2

    moved = _inspect(manifest, _exact_devices(port_suffix="-other-port"))
    assert moved["accepted"] is True
    assert moved["inspection_id"] != exact["inspection_id"]


def test_optional_absence_is_honest_and_does_not_block() -> None:
    manifest = deepcopy(load_manifest(MANIFEST_PATH, REGISTRY))
    optional = deepcopy(
        next(item for item in manifest["roles"] if item["role"] == "charger_gripper")
    )
    optional.update(
        role="optional_debug_adapter",
        requirement="optional",
        stable_path="/dev/iii/optional-debug-adapter",
    )
    optional["match"].update(vendor_id="9999", product_id="0001")
    manifest["roles"].append(optional)
    manifest["roles"].sort(key=lambda item: item["role"])
    report = _inspect(manifest, _exact_devices())
    assert report["accepted"] is True
    assert report["roles"]["optional_debug_adapter"] == {
        "requirement": "optional",
        "state": "missing",
        "unambiguous": False,
        "stable_path": "/dev/iii/optional-debug-adapter",
        "stable_path_ok": False,
        "matched_device_ids": [],
    }


def test_unmatched_device_is_sanitized_reviewable_and_never_learned() -> None:
    manifest = load_manifest(MANIFEST_PATH, REGISTRY)
    before = json.dumps(manifest, sort_keys=True)
    devices = _exact_devices() + [
        _device("ttyUSB8", vendor="dead", product="beef", serial="replacement-x")
    ]
    report = _inspect(manifest, devices)
    assert report["accepted"] is True
    assert len(report["unmatched_device_ids"]) == 1
    assert report["automatic_learning"] is False
    assert json.dumps(manifest, sort_keys=True) == before
    assert set(report["devices"][0]) == {
        "device_id",
        "subsystem",
        "device_node",
        "sysfs_path",
        "id_bus",
        "vendor_id",
        "product_id",
        "serial",
        "interface_number",
        "id_path",
        "driver",
        "v4l_index",
        "v4l_capabilities",
        "product_name",
    }


def test_uncommissioned_serial_allowlist_and_unevidenced_retirement_are_refused() -> (
    None
):
    manifest = deepcopy(load_manifest(MANIFEST_PATH, REGISTRY))
    manifest["roles"][2]["match"]["serial_allowlist"] = ["observed-live-value"]
    manifest["manifest_id"] = content_identity(
        {key: value for key, value in manifest.items() if key != "manifest_id"}
    )
    with pytest.raises(ContractError, match="uncommissioned serial"):
        validate_manifest(manifest, REGISTRY)

    manifest = deepcopy(load_manifest(MANIFEST_PATH, REGISTRY))
    manifest["legacy_rule_retirement"]["state"] = "retired"
    manifest["manifest_id"] = content_identity(
        {key: value for key, value in manifest.items() if key != "manifest_id"}
    )
    with pytest.raises(ContractError, match="retirement"):
        validate_manifest(manifest, REGISTRY)


def test_commissioning_requires_every_phase_and_a_distinct_reboot() -> None:
    manifest = load_manifest(MANIFEST_PATH, REGISTRY)
    phases = manifest["legacy_rule_retirement"]["required_phases"]
    reports = {
        phase: _inspect(
            manifest,
            _exact_devices(port_suffix=f"-{phase}"),
            boot="boot-b" if phase == "reboot" else "boot-a",
        )
        for phase in phases
    }
    functional = {role: "f" * 64 for role in manifest["requirements"]["required"]}
    evaluation = validate_commissioning_sequence(
        manifest, reports, functional_evidence=functional
    )
    REGISTRY.validate("hardware-commissioning-evaluation", evaluation)
    assert evaluation["accepted"] is True
    assert evaluation["automatic_learning"] is False
    with pytest.raises(ContractError, match="phase set"):
        validate_commissioning_sequence(
            manifest,
            {key: value for key, value in reports.items() if key != "port-swap"},
            functional_evidence=functional,
        )
    for report in reports.values():
        report["boot_id"] = "boot-a"
        report["inspection_id"] = content_identity(
            {key: value for key, value in report.items() if key != "inspection_id"}
        )
    with pytest.raises(ContractError, match="reboot evidence"):
        validate_commissioning_sequence(
            manifest, reports, functional_evidence=functional
        )
    with pytest.raises(ContractError, match="functional evidence"):
        validate_commissioning_sequence(
            manifest,
            {
                phase: _inspect(
                    manifest,
                    _exact_devices(port_suffix=f"-functional-{phase}"),
                    boot="boot-b" if phase == "reboot" else "boot-a",
                )
                for phase in phases
            },
            functional_evidence={"mmwave_cli": "f" * 64},
        )
