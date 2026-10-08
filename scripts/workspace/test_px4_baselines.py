"""PX4 parameter baselines must match the Pi endpoints that provisioning creates."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/III-Drone-Contracts"))

from iii_drone_contracts.px4_parameters import (  # noqa: E402
    BASELINE_DIRECTORY,
    BASELINE_FILES,
    load_baseline,
)

VARS = yaml.safe_load(
    (ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml").read_text(encoding="utf-8")
)
PI_ADDRESS = int(ipaddress.IPv4Interface(VARS["iii_px4_host_address"]).ip)
TRANSPORT_PREFIXES = ("UXRCE_DDS_", "MAV_2_")


def _parameters(profile: str) -> dict[str, int | float]:
    return load_baseline(profile, ROOT / BASELINE_DIRECTORY)


def test_each_profile_has_one_standalone_parameter_file():
    directory = ROOT / BASELINE_DIRECTORY
    assert sorted(path.name for path in directory.iterdir()) == sorted(BASELINE_FILES.values())
    # Parameters live in data files, not in scripts that apply them.
    assert not list((ROOT / "deployment/px4").glob("*.nsh"))
    for name in BASELINE_FILES.values():
        rows = [
            line.split("\t")
            for line in (directory / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        # QGroundControl's format: system 1, autopilot component 1.
        assert all(row[:2] == ["1", "1"] and row[4] in {"6", "9"} for row in rows), name


@pytest.mark.parametrize(
    ("profile", "dds_port", "mavlink_port"),
    [
        ("hil", VARS["iii_hil_uxrce_dds_udp_port"], VARS["iii_hil_mavlink_udp_port"]),
        ("opti_track", VARS["iii_px4_uxrce_dds_udp_port"], VARS["iii_px4_mavlink_udp_port"]),
        ("real", VARS["iii_px4_uxrce_dds_udp_port"], VARS["iii_px4_mavlink_udp_port"]),
    ],
)
def test_px4_baseline_targets_the_provisioned_pi_endpoints(profile, dds_port, mavlink_port):
    parameters = _parameters(profile)
    assert parameters["UXRCE_DDS_CFG"] == 1000
    assert parameters["UXRCE_DDS_AG_IP"] == PI_ADDRESS
    assert parameters["UXRCE_DDS_PRT"] == dds_port
    assert parameters["MAV_2_CONFIG"] == 1000
    assert parameters["MAV_2_UDP_PRT"] == mavlink_port
    assert parameters["MAV_2_REMOTE_PRT"] == mavlink_port
    assert parameters["MAV_2_BROADCAST"] == 1
    # The telemetry radio's MAVLink instances are never reconfigured.
    assert not any(key.startswith(("MAV_0_", "MAV_1_")) for key in parameters)


@pytest.mark.parametrize("profile", ["opti_track", "real"])
def test_aircraft_baselines_share_the_stack_domain_and_arrival_stamping(profile):
    parameters = _parameters(profile)
    # Contract C6: PX4's DDS domain equals the provisioned stack domain.
    assert parameters["UXRCE_DDS_DOM_ID"] == VARS["iii_ros_domain_id"]
    # Arrival stamping, as qualified in HIL.
    assert parameters["UXRCE_DDS_SYNCT"] == 0


def test_opti_track_baseline_is_vision_only():
    parameters = _parameters("opti_track")
    # Vision horizontal + vertical position + yaw; vision is the height
    # reference with the barometer as backup; no GPS or magnetometer.
    assert parameters["EKF2_EV_CTRL"] == 0b1011
    assert parameters["EKF2_HGT_REF"] == 3
    assert parameters["EKF2_BARO_CTRL"] == 1
    assert parameters["EKF2_GPS_CTRL"] == 0
    assert parameters["EKF2_MAG_TYPE"] == 5
    assert parameters["SYS_HAS_GPS"] == 0
    assert parameters["SYS_HAS_MAG"] == 0
    assert parameters["EKF2_NOAID_TOUT"] == 1_000_000
    assert parameters["EKF2_EVA_NOISE"] >= 0.05
    # EKF2_EV_DELAY is a float32 on PX4.
    assert isinstance(parameters["EKF2_EV_DELAY"], float)


@pytest.mark.parametrize("profile", ["opti_track", "real"])
def test_site_measured_values_are_never_baselined(profile):
    parameters = _parameters(profile)
    # Hover thrust is measured per payload configuration; the geofence is set
    # from the measured cage or the flight site.
    assert "MPC_THR_HOVER" not in parameters
    assert not any(key.startswith("GF_") for key in parameters)
    # Sensor calibration belongs to the individual flight controller.
    assert not any(key.startswith("CAL_") for key in parameters)


def test_real_baseline_is_a_dual_rtk_gnss_aircraft():
    parameters = _parameters("real")
    # Two u-blox receivers on the GPS 1 and GPS 2 ports, as moving base and rover.
    assert (parameters["GPS_1_CONFIG"], parameters["GPS_2_CONFIG"]) == (201, 202)
    assert (parameters["GPS_1_PROTOCOL"], parameters["GPS_2_PROTOCOL"]) == (1, 1)
    assert parameters["GPS_UBX_MODE"] == 1
    assert parameters["SENS_GPS_MASK"] == 7
    # GNSS position, altitude and velocity; the magnetometer gives the heading.
    assert parameters["SYS_HAS_GPS"] == 1 and parameters["SYS_HAS_MAG"] == 1
    assert parameters["EKF2_GPS_CTRL"] == 7
    assert parameters["EKF2_HGT_REF"] == 1
    assert parameters["EKF2_MAG_TYPE"] == 0
    # No external vision outdoors.
    assert parameters["EKF2_EV_CTRL"] == 0


def test_real_baseline_undoes_everything_the_opti_track_baseline_changes():
    real, opti_track = _parameters("real"), _parameters("opti_track")
    assert set(opti_track) <= set(real)
    estimator_and_failsafes = {
        name: value for name, value in opti_track.items() if not name.startswith(TRANSPORT_PREFIXES)
    }
    changed = {name for name, value in estimator_and_failsafes.items() if real[name] != value}
    # The lab's estimator sources and failsafes all differ from the outdoor ones.
    assert {"EKF2_EV_CTRL", "EKF2_GPS_CTRL", "EKF2_HGT_REF", "EKF2_MAG_TYPE", "SYS_HAS_GPS",
            "SYS_HAS_MAG", "EKF2_NOAID_TOUT", "NAV_RCL_ACT", "COM_LOW_BAT_ACT"} <= changed


SNAPSHOT = ROOT / "deployment/px4/snapshots/real-commissioned-2026-09-04.params"


def test_the_commissioned_flight_controller_is_tracked_in_full():
    from iii_drone_contracts.px4_parameters import parse_baseline

    snapshot = parse_baseline(SNAPSHOT.read_text(encoding="utf-8"))
    assert len(snapshot) == 1155
    # A reference record: complete, with this airframe's calibration.
    assert any(name.startswith("CAL_") for name in snapshot)
    assert "MPC_THR_HOVER" in snapshot


def test_real_baseline_takes_its_values_from_the_commissioned_snapshot():
    from iii_drone_contracts.px4_parameters import parse_baseline

    snapshot = parse_baseline(SNAPSHOT.read_text(encoding="utf-8"))
    for name, value in _parameters("real").items():
        if name.startswith(TRANSPORT_PREFIXES):
            # The transport moved to Ethernet after the snapshot.
            continue
        assert name in snapshot, name
        assert snapshot[name] == pytest.approx(value, rel=1e-6), name
        assert type(snapshot[name]) is type(value), name
