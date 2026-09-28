from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/simulation/launch_hil_workstation.sh"


def test_hil_workstation_launcher_is_shell_valid_and_uses_isolated_links():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    source = SCRIPT.read_text(encoding="utf-8")

    assert "III_HIL_XRCE_PORT:-8890" in source
    assert 'PI_ENDPOINT="${III_HIL_PI_ENDPOINT:-iii.local}"' in source
    assert 'PI_ADDRESS="${III_HIL_PI_ADDRESS:-}"' in source
    assert 'WORKSTATION_ADDRESS="${III_HIL_WORKSTATION_ADDRESS:-}"' in source
    assert 'WORKSTATION_INTERFACE="${III_HIL_WORKSTATION_INTERFACE:-}"' in source
    assert "--host <hostname-or-IPv4>" in source
    assert 'HIL_PEER_ADDRESS_FILE="${III_HIL_PEER_ADDRESS_FILE:-${HIL_RUNTIME_DIR}/pi-address-${PX4_INSTANCE}}"' in source
    assert "getent ahostsv4" in source
    assert "session_pi_address" in source
    assert "record_pi_address" in source
    assert "rm -f \"${HIL_PEER_ADDRESS_FILE}\"" in source
    assert "select_workstation_route" in source
    assert 'PI_ENDPOINT}" == "iii.local"' not in source
    assert 'SCRIPT_WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"' in source
    assert "elif [[ -x /home/iii/ws/tools/simulation/launch_simulation_tools.sh ]]" in source
    assert source.count('III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}"') >= 3
    assert "label=devcontainer.local_folder=${SCRIPT_WORKSPACE_ROOT}" in source
    assert 'exec docker exec -u iii "${forwarded_environment[@]}"' in source
    assert "III_HIL_MAVLINK_REMOTE_PORT:-14544" in source
    assert "III_HIL_MAVLINK_AUDIT_REMOTE_PORT:-14543" in source
    assert "III_HIL_MAVLINK_PARAMETER_REMOTE_PORT:-14551" in source
    assert "III_HIL_MAVLINK_QGC_REMOTE_PORT:-14550" in source
    assert "III_HIL_PX4_INSTANCE:-0" in source
    assert "III_HIL_PX4_SYSTEM_ID:-8" in source
    assert "III_HIL_ROS_DOMAIN_ID:-42" in source
    assert "ros2 launch iii_drone_simulation tf_sim.launch.py" in source
    assert "/drone_frame_broadcaster/is_alive" in source
    assert "ROS_DOMAIN_ID='${ROS_DOMAIN_ID}'" in source
    assert "PX4_PARAM_UXRCE_DDS_DOM_ID" not in source
    assert "PX4_PARAM_UXRCE_DDS_AG_IP" in source
    assert "PX4_PARAM_UXRCE_DDS_SYNCT=0" in source
    assert "PX4_PARAM_MAV_SYS_ID" not in source
    assert 'PX4_STARTUP_SCRIPT="${HIL_RUNTIME_DIR}/px4-rcS-${PX4_INSTANCE}"' in source
    assert 'print "param set MAV_SYS_ID " system_id' in source
    assert 'END { if (system_id_replacements != 1 || dds_key_replacements != 1) exit 42 }' in source
    assert "-s '${PX4_STARTUP_SCRIPT}'" in source
    assert "-w '${PX4_BUILD_DIR}/rootfs' '${PX4_BUILD_DIR}/etc'" in source
    assert '"param set MAV_SYS_ID ${PX4_SYSTEM_ID}"' not in source
    assert "PX4_UXRCE_DDS_NO_NS" not in source
    assert "mavlink start -x" in source
    assert "MAVLINK_AUDIT_LOCAL_PORT" in source
    assert "MAVLINK_PARAMETER_LOCAL_PORT" in source
    assert '"mavlink stop-all"' in source
    assert "MAVLINK_QGC_LOCAL_PORT" in source
    assert "RMW_IMPLEMENTATION=rmw_fastrtps_cpp" in source
    assert "FASTDDS_BUILTIN_TRANSPORTS=UDPv4" in source
    assert "ros2 launch iii_drone_simulation sim_assets.launch.py" in source
    assert "-p require_px4_battery_for_charging:=false" not in source
    assert "-p px4_battery_charge_topic:=/hil/sim_battery_charge" not in source
    assert "synthetic_battery" not in source
    assert "ros2 lifecycle set /payload/charger_gripper/charger_gripper activate" in source
    assert "socket.create_connection((pi, 22)" in source
    assert 'PI_USER="${III_HIL_PI_USER:-iii}"' in source
    assert 'PX4_DDS_CLIENT_KEY="${III_HIL_PX4_DDS_CLIENT_KEY:-2}"' in source
    assert "require_standard_link" in source
    assert "III_HIL_RENDERED" in source
    assert "run_simulation_launcher" in source
    assert "hil_gazebo_viewer" in source
    assert "sim_session_healthy" in source
    assert "adapter_panes_healthy" in source
    assert 'session_exists "${ADAPTER_SESSION}" && ! adapters_locally_ready' in source
    assert "adapters_ready" in source
    assert "local -a probe_pids=()" in source
    assert "wait \"${probe_pid}\" || result=1" in source
    assert "canonical_px4_records \"${endpoint_pids}\"" in source
    assert "ADAPTER_READINESS_CONFIRMED=1" in source
    assert "for attempt in {1..90}" in source
    assert "PX4_WORK_DIR" not in source
    assert 'ip -4 route get "${pi_address}"' in source
    assert "ping -n" not in source


def test_hil_workstation_launcher_help_has_no_side_effects():
    result = subprocess.run(
        [str(SCRIPT), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "{start|status|stop|battery-check} [--host <hostname-or-IPv4>]" in result.stdout


def test_hil_workstation_launcher_accepts_battery_check_action(tmp_path):
    # Run only the argument parser. The full action requires a live Pi and
    # simulation, but rejecting it before dispatch broke HIL startup.
    source = SCRIPT.read_text(encoding="utf-8")
    parser = source.split("# The operator-facing script runs on the workstation", 1)[0]
    launcher = tmp_path / "launch_hil_workstation.sh"
    launcher.write_text(parser + '\nprintf "accepted:%s\\n" "${ACTION}"\n', encoding="utf-8")
    result = subprocess.run(
        ["bash", str(launcher), "battery-check"],
        env={
            **os.environ,
            "III_HIL_WORKSPACE_ROOT": str(ROOT),
            "III_HIL_GZ_PARTITION": "test-battery-check-parser",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "accepted:battery-check"


def test_hil_workstation_launcher_accepts_host_before_or_after_action():
    for argv in (
        ["--host", "192.0.2.9", "unknown-action"],
        ["unknown-action", "--host", "192.0.2.9"],
    ):
        result = subprocess.run(
            [str(SCRIPT), *argv],
            env={**os.environ, "III_HIL_PI_ENDPOINT": "stale-pi.local"},
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Pi 192.0.2.9" in result.stderr
