#!/usr/bin/env bash

set -euo pipefail

SESSION_NAME="${III_SIM_TOOLS_SESSION:-iii_sim_tools}"
WORKSPACE_ROOT="${III_SIM_TOOLS_WORKSPACE_ROOT:-/home/iii/ws}"
PX4_ROOT="${III_SIM_TOOLS_PX4_ROOT:-${WORKSPACE_ROOT}/PX4-Autopilot}"
PX4_BUILD_DIR="${III_SIM_TOOLS_PX4_BUILD_DIR:-${PX4_ROOT}/build/px4_sitl_default}"
PX4_INSTANCE="${III_SIM_TOOLS_PX4_INSTANCE:-0}"
GZ_WORLD="${III_SIM_TOOLS_GZ_WORLD:-hca_full_pylon_setup}"
SIM_ASSET_INSTALLER="${III_SIM_TOOLS_ASSET_INSTALLER:-${WORKSPACE_ROOT}/src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh}"
PX4_COMMAND="${III_SIM_TOOLS_PX4_COMMAND:-source ${WORKSPACE_ROOT}/setup/setup_dev.bash && cd ${PX4_ROOT} && HEADLESS=1 make px4_sitl gz_d4s_dc_drone}"
DEFAULT_GZ_GUI_COMMAND="source ${WORKSPACE_ROOT}/setup/setup_dev.bash && ready=0; for attempt in {1..60}; do if gz service -i --service /world/${GZ_WORLD}/scene/info 2>&1 | grep -q 'Service providers'; then ready=1; break; fi; sleep 1; done; if [ \"\${ready}\" != 1 ]; then echo 'Timed out waiting for Gazebo world ${GZ_WORLD}' >&2; exit 1; fi; exec gz sim -g"
GZ_GUI_COMMAND="${III_SIM_TOOLS_GZ_GUI_COMMAND:-${DEFAULT_GZ_GUI_COMMAND}}"
QGC_COMMAND="${III_SIM_TOOLS_QGC_COMMAND:-cd /home/iii && ./QGroundControl.AppImage}"
ATTACH=1
RECREATE=0

while (($# > 0)); do
    case "$1" in
        --no-attach)
            ATTACH=0
            ;;
        --recreate)
            RECREATE=1
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
    shift
done

session_exists() {
    tmux has-session -t "${SESSION_NAME}" 2>/dev/null
}

px4_simulation_process_groups() {
    local pid
    local pgid
    local args

    ps -eo pid=,pgid=,args= | while read -r pid pgid args; do
        case "${args}" in
            *"${PX4_BUILD_DIR}/bin/px4"*|\
            *"make px4_sitl gz_d4s_dc_drone"*|\
            *"cmake --build ${PX4_BUILD_DIR}"*"gz_d4s_dc_drone"*|\
            *"gz sim "*"${PX4_ROOT}/Tools/simulation/gz/worlds/"*|\
            *"gz sim -g"*|\
            *"QGroundControl.AppImage"*)
                if [[ "${pid}" != "$$" ]]; then
                    printf '%s\n' "${pgid}"
                fi
                ;;
        esac
    done | sort -u
}

cleanup_stale_px4_simulation() {
    local process_groups
    process_groups="$(px4_simulation_process_groups)"

    if [[ -z "${process_groups}" && ! -e "/tmp/px4_lock-${PX4_INSTANCE}" && ! -e "/tmp/px4-sock-${PX4_INSTANCE}" ]]; then
        return
    fi

    cat >&2 <<EOF
Cleaning stale PX4 SITL state for instance ${PX4_INSTANCE}.
EOF

    if [[ -n "${process_groups}" ]]; then
        while IFS= read -r pgid; do
            [[ -n "${pgid}" ]] || continue
            kill -TERM -- -"${pgid}" 2>/dev/null || true
        done <<< "${process_groups}"
        sleep 1
        while IFS= read -r pgid; do
            [[ -n "${pgid}" ]] || continue
            kill -KILL -- -"${pgid}" 2>/dev/null || true
        done <<< "${process_groups}"
    fi

    rm -f "/tmp/px4_lock-${PX4_INSTANCE}" "/tmp/px4-sock-${PX4_INSTANCE}"
}

px4_assets_installed() {
    [[ -d "${PX4_ROOT}/Tools/simulation/gz/models/d4s_dc_drone" ]] &&
    [[ -f "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/99999_gz_d4s_dc_drone" ]] &&
    grep -q "99999_gz_d4s_dc_drone" "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/CMakeLists.txt"
}

px4_build_references_missing_gz_vendor() {
    local ninja_file="${PX4_BUILD_DIR}/build.ninja"
    local dependency

    [[ -f "${ninja_file}" ]] || return 1

    while IFS= read -r dependency; do
        if [[ ! -e "${dependency}" ]]; then
            echo "PX4 SITL build cache references missing Gazebo vendor library: ${dependency}" >&2
            return 0
        fi
    done < <(grep -hoE '/opt/ros/[^[:space:]]*libgz-sim8\.so\.[^[:space:]]*' "${ninja_file}" | sort -u)

    return 1
}

tmux_shell_command() {
    printf 'bash -lc %q' "$1"
}

refresh_px4_build_cache_if_needed() {
    if px4_build_references_missing_gz_vendor; then
        cat >&2 <<EOF
Removing stale PX4 SITL build cache:
  ${PX4_BUILD_DIR}

PX4 will reconfigure against the Gazebo vendor libraries installed in this container.
EOF
        rm -rf "${PX4_BUILD_DIR}"
    fi
}

if [[ -z "${III_SIM_TOOLS_PX4_COMMAND:-}" ]] && ! px4_assets_installed; then
    cat >&2 <<EOF
PX4 D4S Gazebo assets are not installed in ${PX4_ROOT}.

The default simulation command expects the custom model and airframe:
  make px4_sitl gz_d4s_dc_drone

Install the assets into the PX4 checkout first:
  ${SIM_ASSET_INSTALLER} ${PX4_ROOT}

Or override the PX4 pane command explicitly:
  III_SIM_TOOLS_PX4_COMMAND='cd ${PX4_ROOT} && make px4_sitl gz_x500'
EOF
    exit 1
fi

refresh_px4_build_cache_if_needed

if ((RECREATE)) && session_exists; then
    tmux kill-session -t "${SESSION_NAME}"
    cleanup_stale_px4_simulation
elif ! session_exists; then
    cleanup_stale_px4_simulation
fi

if ! session_exists; then
    tmux new-session -d -s "${SESSION_NAME}" -n "simulation" "$(tmux_shell_command "${PX4_COMMAND}")"
    tmux set-option -t "${SESSION_NAME}" remain-on-exit on
    tmux split-window -t "${SESSION_NAME}:simulation" -v "$(tmux_shell_command "${GZ_GUI_COMMAND}")"
    tmux split-window -t "${SESSION_NAME}:simulation" -h "$(tmux_shell_command "${QGC_COMMAND}")"
    tmux select-layout -t "${SESSION_NAME}:simulation" tiled
    tmux select-pane -t "${SESSION_NAME}:simulation.0" -T "PX4 / Gazebo"
    tmux select-pane -t "${SESSION_NAME}:simulation.1" -T "Gazebo GUI"
    tmux select-pane -t "${SESSION_NAME}:simulation.2" -T "QGroundControl"
fi

if ((ATTACH)); then
    exec tmux attach -t "${SESSION_NAME}"
fi
