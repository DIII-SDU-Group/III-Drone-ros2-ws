from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/simulation/launch_hil_workstation.sh"


def test_hil_workstation_launcher_is_shell_valid_and_uses_isolated_links():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    source = SCRIPT.read_text(encoding="utf-8")

    assert "III_HIL_XRCE_PORT:-8889" in source
    assert 'SCRIPT_WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"' in source
    assert "elif [[ -x /home/iii/ws/tools/simulation/launch_simulation_tools.sh ]]" in source
    assert source.count('III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}"') == 3
    assert "label=devcontainer.local_folder=${SCRIPT_WORKSPACE_ROOT}" in source
    assert 'exec docker exec -u iii "${forwarded_environment[@]}"' in source
    assert "III_HIL_MAVLINK_REMOTE_PORT:-14542" in source
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
    assert 'END { if (replacements != 1) exit 42 }' in source
    assert "-s '${PX4_STARTUP_SCRIPT}'" in source
    assert "-w '${PX4_BUILD_DIR}/rootfs' '${PX4_BUILD_DIR}/etc'" in source
    assert '"param set MAV_SYS_ID ${PX4_SYSTEM_ID}"' not in source
    assert "PX4_UXRCE_DDS_NO_NS" not in source
    assert "mavlink start -x" in source
    assert "MAVLINK_AUDIT_LOCAL_PORT" in source
    assert "MAVLINK_PARAMETER_LOCAL_PORT" in source
    assert '"mavlink stop-all"' in source
    assert "MAVLINK_QGC_LOCAL_PORT" in source
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in source
    assert "NetworkInterface address=" in source
    assert "ros2 launch iii_drone_simulation sim_assets.launch.py" in source
    assert "-p require_px4_battery_for_charging:=false" in source
    assert "-p px4_battery_charge_topic:=/hil/sim_battery_charge" in source
    assert "ros2 lifecycle set /payload/charger_gripper/charger_gripper activate" in source
    assert "socket.create_connection((pi, 22)" in source
    assert "sim_session_healthy" in source
    assert "adapter_panes_healthy" in source
    assert 'session_exists "${ADAPTER_SESSION}" && ! adapter_panes_healthy' in source
    assert "adapters_ready" in source
    assert "for attempt in {1..90}" in source
    assert "PX4_WORK_DIR" not in source
    assert "ip -4 route" not in source
    assert "ping -n" not in source


def test_hil_workstation_launcher_help_has_no_side_effects():
    result = subprocess.run(
        [str(SCRIPT), "--help"], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "{start|status|stop}" in result.stdout
