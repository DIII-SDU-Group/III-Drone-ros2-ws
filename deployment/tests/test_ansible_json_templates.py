from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deployment/ansible"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _live_state(profile: str) -> dict[str, object]:
    configuration_hash = __import__("hashlib").sha256(
        _canonical({"schema": "iii.no-configuration/v1", "profile": profile}).encode(
            "utf-8"
        )
    ).hexdigest()
    commissioning_hash = __import__("hashlib").sha256(
        _canonical({"schema": "iii.not-commissioned/v1", "profile": profile}).encode(
            "utf-8"
        )
    ).hexdigest()
    value: dict[str, object] = {
        "schema": "iii.receiver-live-state/v1",
        "target_state_hash": "0" * 64,
        "active_release_id": None,
        "configuration_hash": configuration_hash,
        "commissioning_hash": commissioning_hash,
        "profile": profile,
    }
    unsigned = {key: item for key, item in value.items() if key != "target_state_hash"}
    value["target_state_hash"] = __import__("hashlib").sha256(
        _canonical(unsigned).encode("utf-8")
    ).hexdigest()
    return value


def test_ansible_json_templates_emit_only_canonical_json_and_one_newline() -> None:
    environment = Environment(
        loader=FileSystemLoader(ANSIBLE),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    environment.filters["iii_canonical_json"] = _canonical
    environment.filters["iii_live_state"] = _live_state
    context = {
        "ansible_check_mode": False,
        "iii_baseline_id": "a" * 64,
        "iii_unit_contract_id": "d" * 64,
        "iii_target_definition_id": "b" * 64,
        "iii_shared_target_profile_id": "e" * 64,
        "iii_target_class": "raspberry-pi-5-noble-arm64",
        "iii_logical_target": "drone",
        "iii_profile": "real",
        "iii_runtime_uid": 1100,
        "iii_runtime_gid": 1100,
        "iii_runtime_user": "iii",
        "iii_deployment_user": "iii-deploy",
        "iii_deployment_uid": 1101,
        "iii_deployment_gid": 1101,
        "iii_runtime_api_port": 8765,
        "iii_maintenance_ssh_client_id": "9" * 64,
        "iii_mdns_port": 5353,
        "iii_ubuntu_snapshot": "https://snapshot.ubuntu.com/ubuntu/example",
        "iii_ros_snapshot": "http://snapshots.ros.org/jazzy/example/ubuntu",
        "iii_host_packages": ["chrony"],
        "iii_ros_packages": ["ros-jazzy-ros-base"],
        "iii_hardware_packages": ["udev"],
        "iii_boot_profile_id": "f" * 64,
        "iii_udev_rule_fragments": [{"name": "90-iii-camera.rules"}],
        "iii_health_readiness": {"receiver_id": "c" * 64, "generation": 1},
        "iii_receiver_manifest": {"receiver_id": "c" * 64, "generation": 1},
        "iii_health_package_versions": {"chrony": ["4.5-1"]},
        "iii_provisioning_inputs": {
            "operator_cidr": "192.168.10.0/24",
            "maintenance_ssh_public_key_source": "/controller/maintenance.pub",
        },
    }
    templates = (
        "roles/apt_baseline/templates/host-package-policy.json.j2",
        "roles/firewall/templates/firewall-policy.json.j2",
        "roles/hardware_baseline/templates/hardware-baseline.json.j2",
        "roles/boot_baseline/templates/boot-baseline.json.j2",
        "roles/host_health/templates/host-baseline-report.json.j2",
        "roles/time/templates/clock-policy.json.j2",
        "roles/receiver/templates/deployment-receiver.json.j2",
        "roles/receiver/templates/live-state.json.j2",
    )
    for path in templates:
        raw = environment.get_template(path).render(**context)
        value = json.loads(raw)
        assert raw == _canonical(value) + "\n", path
