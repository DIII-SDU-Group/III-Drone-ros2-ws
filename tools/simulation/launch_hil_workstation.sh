#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-status}"
SCRIPT_WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -n "${III_HIL_WORKSPACE_ROOT:-}" ]]; then
    WORKSPACE_ROOT="${III_HIL_WORKSPACE_ROOT}"
elif [[ -x /home/iii/ws/tools/simulation/launch_simulation_tools.sh ]]; then
    WORKSPACE_ROOT=/home/iii/ws
else
    WORKSPACE_ROOT="${SCRIPT_WORKSPACE_ROOT}"
fi
SIM_LAUNCHER="${WORKSPACE_ROOT}/tools/simulation/launch_simulation_tools.sh"
SIM_SESSION="${III_HIL_SIM_SESSION:-iii_hil_sim}"
ADAPTER_SESSION="${III_HIL_ADAPTER_SESSION:-iii_hil_adapters}"
SESSION_USER="${III_HIL_SESSION_USER:-iii}"
PX4_INSTANCE="${III_HIL_PX4_INSTANCE:-0}"
PX4_SYSTEM_ID="${III_HIL_PX4_SYSTEM_ID:-8}"
PX4_ROOT="${III_HIL_PX4_ROOT:-${WORKSPACE_ROOT}/PX4-Autopilot}"
PX4_BUILD_DIR="${III_HIL_PX4_BUILD_DIR:-${PX4_ROOT}/build/px4_sitl_default}"
PX4_CANONICAL_RCS="${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/rcS"
HIL_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/iii-hil-${UID}"
PX4_STARTUP_SCRIPT="${HIL_RUNTIME_DIR}/px4-rcS-${PX4_INSTANCE}"
PI_ADDRESS="${III_HIL_PI_ADDRESS:-10.42.0.15}"
WORKSTATION_ADDRESS="${III_HIL_WORKSTATION_ADDRESS:-10.42.0.1}"
PX4_AGENT_ADDRESS_U32="${III_HIL_PX4_AGENT_ADDRESS_U32:-170524687}"
XRCE_PORT="${III_HIL_XRCE_PORT:-8889}"
MAVLINK_REMOTE_PORT="${III_HIL_MAVLINK_REMOTE_PORT:-14542}"
MAVLINK_LOCAL_PORT="${III_HIL_MAVLINK_LOCAL_PORT:-14582}"
MAVLINK_AUDIT_REMOTE_PORT="${III_HIL_MAVLINK_AUDIT_REMOTE_PORT:-14543}"
MAVLINK_AUDIT_LOCAL_PORT="${III_HIL_MAVLINK_AUDIT_LOCAL_PORT:-14581}"
MAVLINK_PARAMETER_REMOTE_PORT="${III_HIL_MAVLINK_PARAMETER_REMOTE_PORT:-14551}"
MAVLINK_PARAMETER_LOCAL_PORT="${III_HIL_MAVLINK_PARAMETER_LOCAL_PORT:-14583}"
MAVLINK_QGC_REMOTE_PORT="${III_HIL_MAVLINK_QGC_REMOTE_PORT:-14550}"
MAVLINK_QGC_LOCAL_PORT="${III_HIL_MAVLINK_QGC_LOCAL_PORT:-14584}"
ROS_DOMAIN_ID="${III_HIL_ROS_DOMAIN_ID:-42}"
GZ_PARTITION="${III_HIL_GZ_PARTITION:-iii_hil_${PX4_INSTANCE}}"

usage() {
    cat <<EOF
Usage: $(basename "$0") {start|status|stop}

Runs only workstation-owned HIL processes. The aircraft runtime remains owned by
the Raspberry Pi. Standard link: workstation ${WORKSTATION_ADDRESS}, Pi ${PI_ADDRESS}.
EOF
}

if [[ "${ACTION}" == "-h" || "${ACTION}" == "--help" ]]; then
    usage
    exit 0
fi
if (($# > 1)); then
    usage >&2
    exit 2
fi

# Gazebo and the PX4 SITL cache are owned by the workspace devcontainer. Make
# the operator-facing host command deterministic by entering that container
# instead of accidentally probing or mutating the container-built cache with
# host libraries.
if [[ ! -f /opt/ros/jazzy/setup.bash && -z "${III_HIL_NO_CONTAINER_REEXEC:-}" ]]; then
    container_id="$(
        docker ps \
            --filter "label=devcontainer.local_folder=${SCRIPT_WORKSPACE_ROOT}" \
            --format '{{.ID}}' | head -n1
    )"
    if [[ -z "${container_id}" ]]; then
        echo "The III workspace devcontainer is not running; HIL cannot start safely." >&2
        exit 1
    fi
    forwarded_environment=()
    while IFS= read -r variable_name; do
        forwarded_environment+=(--env "${variable_name}")
    done < <(compgen -A variable III_HIL_)
    exec docker exec -u iii "${forwarded_environment[@]}" "${container_id}" \
        /home/iii/ws/tools/simulation/launch_hil_workstation.sh "${ACTION}"
fi

session_user_command() {
    if [[ "$(id -u)" -eq 0 && "${SESSION_USER}" != "root" ]] && id -u "${SESSION_USER}" >/dev/null 2>&1; then
        sudo -H -u "${SESSION_USER}" env \
            "DISPLAY=${DISPLAY:-}" \
            "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" \
            "XAUTHORITY=${XAUTHORITY:-}" \
            "XDG_RUNTIME_DIR=/run/user/$(id -u "${SESSION_USER}")" \
            "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}" \
            "WORKSPACE_DIR=${WORKSPACE_ROOT}" \
            "$@"
    else
        "$@"
    fi
}

tmux_command() {
    session_user_command tmux "$@"
}

session_exists() {
    tmux_command has-session -t "=$1" 2>/dev/null
}

sim_session_healthy() {
    session_exists "${SIM_SESSION}" &&
        [[ "$(tmux_command display-message -p -t "${SIM_SESSION}:simulation.0" '#{pane_dead}' 2>/dev/null)" == "0" ]]
}

cyclone_uri() {
    printf '%s' "<CycloneDDS><Domain><General><Interfaces><NetworkInterface address=\"${WORKSTATION_ADDRESS}\" priority=\"default\" multicast=\"default\"/></Interfaces></General><Discovery><Peers><Peer address=\"${PI_ADDRESS}\"/></Peers></Discovery></Domain></CycloneDDS>"
}

ros_environment() {
    printf 'export ROS_DOMAIN_ID=%q ROS_LOCALHOST_ONLY=0 ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET ROS2CLI_DISABLE_DAEMON=1 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp GZ_PARTITION=%q CYCLONEDDS_URI=%q' \
        "${ROS_DOMAIN_ID}" "${GZ_PARTITION}" "$(cyclone_uri)"
}

link_probe() {
    python3 - "${WORKSTATION_ADDRESS}" "${PI_ADDRESS}" <<'PY'
import socket
import sys

workstation, pi = sys.argv[1:]
route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    route.connect((pi, 9))
    source = route.getsockname()[0]
finally:
    route.close()
if source != workstation:
    raise SystemExit(f"route to Pi {pi} uses {source}, expected {workstation}")
try:
    with socket.create_connection((pi, 22), timeout=2.0):
        pass
except OSError as exc:
    raise SystemExit(f"Pi {pi} SSH reachability failed: {exc}")
PY
}

require_standard_link() {
    link_probe || {
        echo "Refusing to start a disconnected HIL session." >&2
        return 1
    }
}

px4_command() {
    # PX4's POSIX rcS rewrites UXRCE_DDS_DOM_ID from ROS_DOMAIN_ID
    # immediately before it starts the client. A PX4_PARAM_ override alone is
    # silently overwritten with domain 0.
    # In split-host HIL the agent uses workstation wall time while PX4 SITL
    # advances lockstep simulation time. PX4 timestamp synchronisation would
    # repeatedly jump between those clocks and can starve command handling.
    printf '%s' "source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && exec env HEADLESS=1 GZ_IP=127.0.0.1 GZ_PARTITION='${GZ_PARTITION}' PX4_SIM_MODEL=gz_d4s_dc_drone ROS_DOMAIN_ID='${ROS_DOMAIN_ID}' PX4_UXRCE_DDS_PORT='${XRCE_PORT}' PX4_PARAM_UXRCE_DDS_AG_IP='${PX4_AGENT_ADDRESS_U32}' PX4_PARAM_UXRCE_DDS_SYNCT=0 PX4_PARAM_COM_DL_LOSS_T=300 '${PX4_BUILD_DIR}/bin/px4' -s '${PX4_STARTUP_SCRIPT}' -i '${PX4_INSTANCE}' -w '${PX4_BUILD_DIR}/rootfs' '${PX4_BUILD_DIR}/etc'"
}

prepare_px4_startup_script() {
    local temporary_script
    [[ -f "${PX4_CANONICAL_RCS}" ]] || {
        echo "Canonical PX4 startup script is missing: ${PX4_CANONICAL_RCS}" >&2
        return 1
    }
    mkdir -p "${HIL_RUNTIME_DIR}"
    chmod 700 "${HIL_RUNTIME_DIR}"
    temporary_script="${PX4_STARTUP_SCRIPT}.tmp.$$"
    # PX4's canonical POSIX rcS unconditionally derives MAV_SYS_ID from the
    # instance after applying environment parameter overrides. Commander caches
    # that identity at startup, so changing MAV_SYS_ID later makes MAVLink arm
    # requests disappear without an acknowledgement. Keep the canonical script
    # byte-for-byte except for this single validated assignment.
    awk -v system_id="${PX4_SYSTEM_ID}" '
        $0 == "param set MAV_SYS_ID $((px4_instance+1))" {
            print "param set MAV_SYS_ID " system_id
            replacements += 1
            next
        }
        { print }
        END { if (replacements != 1) exit 42 }
    ' "${PX4_CANONICAL_RCS}" >"${temporary_script}" || {
        rm -f "${temporary_script}"
        echo "PX4 rcS identity assignment changed; refusing an unvalidated HIL startup." >&2
        return 1
    }
    chmod 600 "${temporary_script}"
    mv -f "${temporary_script}" "${PX4_STARTUP_SCRIPT}"
}

start_adapters() {
    local ros_env
    local bridge_command
    local payload_command
    local tf_command
    ros_env="$(ros_environment)"
    bridge_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && ${ros_env} && exec ros2 launch iii_drone_simulation sim_assets.launch.py"
    # PX4 output topics terminate at the Pi-side XRCE agent in split-host HIL.
    # The workstation charger can publish the PX4 charge input, but cannot use
    # PX4 battery feedback as its local enable prerequisite. The mission smoke
    # still proves charge through Pi-observed PX4 BatteryStatus telemetry.
    payload_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' || exit; ${ros_env}; ros2 run iii_drone_simulation sim_charger_gripper_node --ros-args -p use_sim_time:=true -p require_px4_battery_for_charging:=false -p px4_battery_charge_topic:=/hil/sim_battery_charge & node_pid=\$!; for attempt in {1..60}; do ros2 lifecycle get /payload/charger_gripper/charger_gripper >/dev/null 2>&1 && break; sleep 0.5; done; ros2 lifecycle set /payload/charger_gripper/charger_gripper configure && ros2 lifecycle set /payload/charger_gripper/charger_gripper activate; wait \${node_pid}"
    tf_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && ${ros_env} && exec ros2 launch iii_drone_simulation tf_sim.launch.py use_ground_truth_odometry:=true"

    tmux_command new-session -d -s "${ADAPTER_SESSION}" -n adapters "bash -lc $(printf '%q' "${bridge_command}")"
    tmux_command set-option -t "${ADAPTER_SESSION}" remain-on-exit on
    tmux_command select-pane -t "${ADAPTER_SESSION}:adapters.0" -T "Gazebo ROS bridges"
    tmux_command split-window -t "${ADAPTER_SESSION}:adapters" -v "bash -lc $(printf '%q' "${payload_command}")"
    tmux_command select-pane -t "${ADAPTER_SESSION}:adapters.1" -T "Sim charger gripper"
    tmux_command split-window -t "${ADAPTER_SESSION}:adapters" -v "bash -lc $(printf '%q' "${tf_command}")"
    tmux_command select-pane -t "${ADAPTER_SESSION}:adapters.2" -T "Simulation transforms"
}

adapter_panes_healthy() {
    session_exists "${ADAPTER_SESSION}" &&
        ! tmux_command list-panes -t "${ADAPTER_SESSION}:adapters" -F '#{pane_dead}' | grep -q '^1$'
}

adapters_ready() {
    local ros_env
    ros_env="$(ros_environment)"
    adapter_panes_healthy &&
        session_user_command bash -lc "source '${WORKSPACE_ROOT}/setup/setup_dev.bash'; ${ros_env}; timeout 4 ros2 lifecycle get /payload/charger_gripper/charger_gripper" 2>/dev/null | grep -q '^active ' &&
        session_user_command bash -lc "source '${WORKSPACE_ROOT}/setup/setup_dev.bash'; ${ros_env}; timeout 4 ros2 topic echo --once /clock rosgraph_msgs/msg/Clock" >/dev/null 2>&1 &&
        session_user_command bash -lc "source '${WORKSPACE_ROOT}/setup/setup_dev.bash'; ${ros_env}; timeout 4 ros2 topic echo --once /drone_frame_broadcaster/is_alive std_msgs/msg/Header" >/dev/null 2>&1
}

print_status() {
    local result=0
    if session_exists "${SIM_SESSION}"; then
        echo "hil_simulation: running"
        tmux_command list-panes -t "${SIM_SESSION}:simulation" -F 'sim_pane=#{pane_index} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}'
    else
        echo "hil_simulation: stopped"
        result=1
    fi
    if session_exists "${ADAPTER_SESSION}"; then
        echo "hil_adapters: running"
        tmux_command list-panes -t "${ADAPTER_SESSION}:adapters" -F 'adapter_pane=#{pane_index} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}'
        if adapters_ready; then
            echo "hil_adapter_readiness: ready"
        else
            echo "hil_adapter_readiness: unavailable"
            result=1
        fi
    else
        echo "hil_adapters: stopped"
        result=1
    fi
    if link_probe >/dev/null 2>&1; then
        echo "hil_pi_route: ready"
        echo "hil_pi_reachability: ready"
    else
        echo "hil_pi_route: unavailable"
        echo "hil_pi_reachability: unavailable"
        result=1
    fi
    return "${result}"
}

start() {
    require_standard_link
    [[ -x "${PX4_BUILD_DIR}/bin/px4" ]] || {
        echo "Cached PX4 SITL binary is missing: ${PX4_BUILD_DIR}/bin/px4" >&2
        return 1
    }
    prepare_px4_startup_script
    if session_exists "${SIM_SESSION}" && ! sim_session_healthy; then
        env \
            III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
            III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
            III_SIM_TOOLS_PX4_BUILD_DIR="${PX4_BUILD_DIR}" \
            "${SIM_LAUNCHER}" --stop >/dev/null
    fi
    if ! session_exists "${SIM_SESSION}"; then
        env \
            III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
            III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
            III_SIM_TOOLS_RESET_PX4_PARAMS_ON_RECREATE=1 \
            III_SIM_TOOLS_GZ_IP=127.0.0.1 \
            III_SIM_TOOLS_PX4_COMMAND="$(px4_command)" \
            "${SIM_LAUNCHER}" --headless --no-attach
        for attempt in {1..90}; do
            tmux_command capture-pane -p -t "${SIM_SESSION}:simulation.0" -S -80 2>/dev/null | grep -q 'pxh>' && break
            sleep 1
        done
        tmux_command capture-pane -p -t "${SIM_SESSION}:simulation.0" -S -80 | grep -q 'pxh>' || {
            echo "PX4 shell did not become ready." >&2
            return 1
        }
        # rcS starts environment-dependent default MAVLink instances. Replace
        # them with the complete, deterministic HIL endpoint set so repeated
        # launches cannot exhaust PX4's instance limit or leave tools attached
        # to an accidental port.
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" "mavlink stop-all" C-m
        sleep 2
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" \
            "mavlink start -x -u ${MAVLINK_LOCAL_PORT} -o ${MAVLINK_REMOTE_PORT} -t ${PI_ADDRESS} -r 4000000 -f -m onboard" C-m
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" \
            "mavlink start -x -u ${MAVLINK_AUDIT_LOCAL_PORT} -o ${MAVLINK_AUDIT_REMOTE_PORT} -t ${PI_ADDRESS} -r 4000000 -f -m onboard" C-m
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" \
            "mavlink start -x -u ${MAVLINK_PARAMETER_LOCAL_PORT} -o ${MAVLINK_PARAMETER_REMOTE_PORT} -t 127.0.0.1 -r 4000000 -f -m onboard" C-m
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" \
            "mavlink start -x -u ${MAVLINK_QGC_LOCAL_PORT} -o ${MAVLINK_QGC_REMOTE_PORT} -t 127.0.0.1 -r 4000000 -f -m onboard" C-m
    fi
    if session_exists "${ADAPTER_SESSION}" && ! adapter_panes_healthy; then
        tmux_command kill-session -t "${ADAPTER_SESSION}"
    fi
    if ! session_exists "${ADAPTER_SESSION}"; then
        start_adapters
    fi
    for attempt in {1..90}; do
        adapters_ready && break
        sleep 1
    done
    adapters_ready || {
        echo "HIL workstation adapters did not become ready." >&2
        print_status || true
        return 1
    }
    print_status
}

stop() {
    if session_exists "${ADAPTER_SESSION}"; then
        tmux_command kill-session -t "${ADAPTER_SESSION}"
    fi
    env \
        III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
        III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
        III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
        III_SIM_TOOLS_PX4_BUILD_DIR="${PX4_BUILD_DIR}" \
        "${SIM_LAUNCHER}" --stop >/dev/null
    print_status || true
}

case "${ACTION}" in
    start) start ;;
    status) print_status ;;
    stop) stop ;;
    *) usage >&2; exit 2 ;;
esac
