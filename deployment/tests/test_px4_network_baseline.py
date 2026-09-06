from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from iii_deployment.contracts import canonical_json, content_identity
from iii_deployment.px4_network import (
    PX4NetworkBaselineError,
    load_network_baseline,
    render_extras,
    render_net_cfg,
)

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "deployment/px4/network-baseline.json"
SCHEMAS = ROOT / "deployment/schemas/v1"


def test_release_px4_network_baseline_matches_host_peer_and_transports() -> None:
    baseline = load_network_baseline(BASELINE, schema_root=SCHEMAS)
    cloud_init = json.loads(
        (ROOT / "deployment/provisioning/cloud-init-profile.json").read_text()
    )["px4_ethernet"]
    variables = (
        ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml"
    ).read_text()

    assert baseline["network"] == {
        "boot_protocol": "static",
        "companion_address": "10.41.10.1",
        "device": "eth0",
        "dns": "0.0.0.0",
        "netmask": "255.255.255.0",
        "px4_address": "10.41.10.2",
        "router": "0.0.0.0",
    }
    assert cloud_init["address"] == "10.41.10.1/24"
    assert cloud_init["peer_address"] == baseline["network"]["px4_address"]
    assert (
        cloud_init["mavlink_udp_port"]
        == baseline["transports"]["mavlink"]["remote_port"]
    )
    assert (
        cloud_init["uxrce_dds_udp_port"]
        == baseline["transports"]["uxrce_dds"]["agent_port"]
    )
    assert "iii_px4_host_address: 10.41.10.1/24" in variables
    assert "iii_px4_peer_address: 10.41.10.2" in variables


def test_release_px4_network_baseline_renders_exact_sd_card_artifacts() -> None:
    baseline = load_network_baseline(BASELINE, schema_root=SCHEMAS)

    assert (
        render_net_cfg(baseline)
        == (
            "DEVICE=eth0\n"
            "BOOTPROTO=static\n"
            "NETMASK=255.255.255.0\n"
            "IPADDR=10.41.10.2\n"
            "ROUTER=0.0.0.0\n"
            "DNS=0.0.0.0\n"
        ).encode()
    )
    assert (
        render_extras(baseline)
        == (
            "set +e\n"
            "mavlink start -x -u 14540 -o 14540 -t 10.41.10.1 -m onboard -r 100000\n"
            "mavlink start -x -u 14541 -o 14541 -t 10.41.10.1 -m onboard -r 100000\n"
            "uxrce_dds_client start -t udp -p 8888 -h 10.41.10.1\n"
            "set -e\n"
        ).encode()
    )


def test_release_px4_parameter_manifest_disables_automatic_ethernet_owners() -> None:
    manifest = json.loads((ROOT / "deployment/px4/real.json").read_text())
    parameters = {item["name"]: item for item in manifest["parameters"]}
    baseline = load_network_baseline(BASELINE, schema_root=SCHEMAS)

    assert manifest["network_baseline_id"] == baseline["baseline_id"]
    for name, value in baseline["parameter_requirements"].items():
        assert parameters[name] == {
            "classification": "release-required",
            "enforcement": "exact",
            "mav_type": "INT32",
            "name": name,
            "tolerance": 0,
            "value": value,
        }


def test_px4_network_baseline_rejects_identity_or_artifact_drift(
    tmp_path: Path,
) -> None:
    value = json.loads(BASELINE.read_text())
    value["baseline_id"] = "0" * 64
    changed = tmp_path / "changed.json"
    changed.write_bytes(canonical_json(value) + b"\n")

    with pytest.raises(PX4NetworkBaselineError, match="identity"):
        load_network_baseline(changed, schema_root=SCHEMAS)

    value = json.loads(BASELINE.read_text())
    value["artifacts"]["net_cfg_sha256"] = "0" * 64
    value["baseline_id"] = content_identity(
        {key: item for key, item in value.items() if key != "baseline_id"}
    )
    changed.write_bytes(canonical_json(value) + b"\n")
    with pytest.raises(PX4NetworkBaselineError, match="artifact"):
        load_network_baseline(changed, schema_root=SCHEMAS)


def test_px4_network_renderer_is_exact_idempotent_and_refuses_drift(
    tmp_path: Path,
) -> None:
    script = ROOT / "deployment/scripts/render_px4_network_baseline.py"
    output = tmp_path / "px4-sd"
    command = [sys.executable, str(script), "--output", str(output)]

    first = subprocess.run(command, check=True, capture_output=True)
    second = subprocess.run(command, check=True, capture_output=True)
    baseline = load_network_baseline(BASELINE, schema_root=SCHEMAS)

    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["baseline_id"] == baseline["baseline_id"]
    assert (output / "net.cfg").read_bytes() == render_net_cfg(baseline)
    assert (output / "etc/extras.txt").read_bytes() == render_extras(baseline)

    (output / "net.cfg").write_text("drift\n")
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "refusing to overwrite drifted PX4 artifact" in rejected.stderr
