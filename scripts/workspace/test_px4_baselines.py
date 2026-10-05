"""PX4 NSH baselines must match the Pi endpoints that provisioning creates."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
VARS = yaml.safe_load(
    (ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml").read_text(encoding="utf-8")
)
PI_ADDRESS = int(ipaddress.IPv4Interface(VARS["iii_px4_host_address"]).ip)


def _parameters(name: str) -> dict[str, str]:
    lines = (ROOT / "deployment/px4" / name).read_text(encoding="utf-8").splitlines()
    commands = [line for line in lines if line.strip() and not line.startswith("#")]
    # NSH has no inline comments: every command is exactly what PX4 executes.
    assert commands[-2:] == ["param save", "reboot"], name
    parameters = {}
    for command in commands[:-2]:
        words = command.split()
        assert len(words) == 4 and words[:2] == ["param", "set"], command
        assert words[2] not in parameters, f"{words[2]} is set twice in {name}"
        parameters[words[2]] = words[3]
    return parameters


@pytest.mark.parametrize(
    ("name", "dds_port", "mavlink_port"),
    [
        ("hil-ethernet.nsh", VARS["iii_hil_uxrce_dds_udp_port"], VARS["iii_hil_mavlink_udp_port"]),
        ("opti-track.nsh", VARS["iii_px4_uxrce_dds_udp_port"], VARS["iii_px4_mavlink_udp_port"]),
    ],
)
def test_px4_baseline_targets_the_provisioned_pi_endpoints(name, dds_port, mavlink_port):
    parameters = _parameters(name)
    assert parameters["UXRCE_DDS_CFG"] == "1000"
    assert int(parameters["UXRCE_DDS_AG_IP"]) == PI_ADDRESS
    assert int(parameters["UXRCE_DDS_PRT"]) == dds_port
    assert parameters["MAV_2_CONFIG"] == "1000"
    assert int(parameters["MAV_2_UDP_PRT"]) == mavlink_port
    assert int(parameters["MAV_2_REMOTE_PRT"]) == mavlink_port
    assert parameters["MAV_2_BROADCAST"] == "1"
    # The telemetry radio's MAVLink instances are never reconfigured.
    assert not any(key.startswith(("MAV_0_", "MAV_1_")) for key in parameters)


def test_opti_track_baseline_matches_the_stack_domain_and_vision_contract():
    parameters = _parameters("opti-track.nsh")
    # Contract C6: PX4's DDS domain equals the provisioned stack domain.
    assert int(parameters["UXRCE_DDS_DOM_ID"]) == VARS["iii_ros_domain_id"]
    # Arrival stamping, as qualified in HIL.
    assert parameters["UXRCE_DDS_SYNCT"] == "0"
    # Vision horizontal + vertical position + yaw; vision is the height
    # reference with the barometer as backup; no GPS or magnetometer.
    assert int(parameters["EKF2_EV_CTRL"]) == 0b1011
    assert parameters["EKF2_HGT_REF"] == "3"
    assert parameters["EKF2_BARO_CTRL"] == "1"
    assert parameters["EKF2_GPS_CTRL"] == "0"
    assert parameters["EKF2_MAG_TYPE"] == "5"
    assert parameters["SYS_HAS_GPS"] == "0"
    assert parameters["SYS_HAS_MAG"] == "0"
    assert int(parameters["EKF2_NOAID_TOUT"]) == 1_000_000
    assert float(parameters["EKF2_EVA_NOISE"]) >= 0.05
    # Hover thrust is measured per payload configuration, never baselined.
    assert "MPC_THR_HOVER" not in parameters
    # The geofence stays commented until the cage is measured.
    assert not any(key.startswith("GF_") for key in parameters)
