#!/usr/bin/env bash

set -euo pipefail

SESSION_NAME="${III_SIM_TOOLS_SESSION:-iii_sim_tools}"
SESSION_USER="${III_SIM_TOOLS_USER:-iii}"
WORKSPACE_ROOT="${III_SIM_TOOLS_WORKSPACE_ROOT:-/home/iii/ws}"
PX4_ROOT="${III_SIM_TOOLS_PX4_ROOT:-${WORKSPACE_ROOT}/PX4-Autopilot}"
PX4_BUILD_DIR="${III_SIM_TOOLS_PX4_BUILD_DIR:-${PX4_ROOT}/build/px4_sitl_default}"
PX4_INSTANCE="${III_SIM_TOOLS_PX4_INSTANCE:-0}"
RESET_PX4_PARAMS_ON_RECREATE="${III_SIM_TOOLS_RESET_PX4_PARAMS_ON_RECREATE:-1}"
GZ_WORLD="${III_SIM_TOOLS_GZ_WORLD:-hca_full_pylon_setup}"
SIM_ASSET_INSTALLER="${III_SIM_TOOLS_ASSET_INSTALLER:-${WORKSPACE_ROOT}/src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh}"
# PX4 SITL model: gz_<model> spawns Gazebo model <model> and selects the
# <N>_gz_<model> airframe, e.g. gz_d4s_dc_drone_powerline_eval for the powerline
# SLAM evaluation variant.
PX4_SIM_MODEL_NAME="${III_SIM_TOOLS_PX4_SIM_MODEL:-gz_d4s_dc_drone}"
DEFAULT_GZ_GUI_COMMAND="source ${WORKSPACE_ROOT}/setup/setup_dev.bash && ready=0; for attempt in {1..60}; do if gz service -i --service /world/${GZ_WORLD}/scene/info 2>&1 | grep -q 'Service providers'; then ready=1; break; fi; sleep 1; done; if [ \"\${ready}\" != 1 ]; then echo 'Timed out waiting for Gazebo world ${GZ_WORLD}' >&2; exit 1; fi; exec gz sim -g"
GZ_GUI_COMMAND="${III_SIM_TOOLS_GZ_GUI_COMMAND:-${DEFAULT_GZ_GUI_COMMAND}}"
GZ_GUI_READY_TIMEOUT_SECONDS="${III_SIM_TOOLS_GZ_GUI_READY_TIMEOUT_SECONDS:-75}"
GZ_GUI_READY_POLL_INTERVAL_SECONDS="${III_SIM_TOOLS_GZ_GUI_READY_POLL_INTERVAL_SECONDS:-0.25}"
GZ_GUI_PROC_ROOT="${III_SIM_TOOLS_GZ_GUI_PROC_ROOT:-/proc}"
PX4_PROC_ROOT="${III_SIM_TOOLS_PROC_ROOT:-/proc}"
PS_LIST_COMMAND="${III_SIM_TOOLS_PS_COMMAND:-ps -eo pid=,pgid=,comm=,args=}"
HOST_QGC_UDP_PORT="${III_SIM_TOOLS_HOST_QGC_UDP_PORT:-14550}"
ATTACH=1
ATTACH_ONLY=0
NO_ATTACH=0
RECREATE=0
STATUS=0
STOP=0
HEADLESS=0
RENDERED=0

# Simulation transport is local to the PX4/Gazebo/III host. Override inherited
# GZ_IP from long-lived agents unless an operator explicitly selects another
# Gazebo transport bind address for the simulation tools.
export GZ_IP="${III_SIM_TOOLS_GZ_IP:-127.0.0.1}"

while (($# > 0)); do
    case "$1" in
        --headless)
            HEADLESS=1
            ;;
        --rendered)
            RENDERED=1
            ;;
        --no-attach)
            ATTACH=0
            NO_ATTACH=1
            ;;
        --attach)
            ATTACH_ONLY=1
            ATTACH=0
            ;;
        --recreate)
            RECREATE=1
            ;;
        --sim-model)
            if (($# < 2)); then
                echo "--sim-model needs a value." >&2
                exit 1
            fi
            PX4_SIM_MODEL_NAME="$2"
            shift
            ;;
        --status)
            STATUS=1
            ATTACH=0
            ;;
        --stop)
            STOP=1
            ATTACH=0
            ;;
        --help|-h)
            cat <<EOF
Usage: $(basename "$0") [--headless|--rendered] [--sim-model gz_<model>] [--no-attach] [--attach] [--recreate] [--status] [--stop]

Options:
  --headless   Start only the PX4/Gazebo backend pane; skip the Gazebo GUI.
  --sim-model  PX4_SIM_MODEL for a new session (default gz_d4s_dc_drone, or
               III_SIM_TOOLS_PX4_SIM_MODEL). A running session keeps its model;
               use --recreate to switch.
  --rendered   Ensure the Gazebo GUI pane is present for this session.
  --no-attach  Start or recreate the tmux session without attaching.
  --attach     Attach to an existing simulation session without creating one.
  --recreate   Recreate the simulation tmux session and clean stale PX4 SITL state.
  --status     Print simulation session and process status.
  --stop       Stop the simulation session and clean stale PX4 SITL state.
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ ! "${PX4_SIM_MODEL_NAME}" =~ ^gz_[A-Za-z0-9_]+$ ]]; then
    echo "Invalid PX4 simulation model '${PX4_SIM_MODEL_NAME}'; expected gz_<model>." >&2
    exit 1
fi
PX4_GZ_MODEL_DIR="${PX4_SIM_MODEL_NAME#gz_}"

# UXRCE_DDS_SYNCT=0 keeps PX4 DDS timestamps in the lockstep simulation-time
# domain (as in HIL). With synchronization enabled, host stalls look like agent
# clock jumps; PX4 then resets its timesync filter and publishes samples with a
# zero offset (boot-relative stamps between wall-clock stamps), which Core
# correctly fences as a position-source epoch change.
DEFAULT_PX4_COMMAND="source ${WORKSPACE_ROOT}/setup/setup_dev.bash && cd ${PX4_ROOT} && make px4_sitl_default && cd ${PX4_BUILD_DIR}/rootfs && exec env HEADLESS=1 PX4_SIM_MODEL=${PX4_SIM_MODEL_NAME} GZ_IP=\$GZ_IP PX4_PARAM_UXRCE_DDS_SYNCT=0 ${PX4_BUILD_DIR}/bin/px4 -i ${PX4_INSTANCE}"
PX4_COMMAND="${III_SIM_TOOLS_PX4_COMMAND:-${DEFAULT_PX4_COMMAND}}"

if ((STATUS && STOP)); then
    echo "--status and --stop are mutually exclusive." >&2
    exit 1
fi
if ((HEADLESS && RENDERED)); then
    echo "--headless and --rendered are mutually exclusive." >&2
    exit 1
fi
if ((ATTACH_ONLY && (NO_ATTACH || STATUS || STOP || RECREATE || HEADLESS))); then
    echo "--attach cannot be combined with another operation or launch option." >&2
    exit 1
fi

session_exists() {
    # tmux otherwise accepts unique prefixes, so an isolated session such as
    # iii_sim_tools_dataset must not masquerade as the canonical session.
    tmux_command has-session -t "=${SESSION_NAME}" 2>/dev/null
}

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

gazebo_gui_process_running() {
    local pane_pid="$1" pane_command="$2" command_line
    [[ "${pane_command}" != "bash" && "${pane_pid}" =~ ^[0-9]+$ ]] || return 1
    [[ -r "${GZ_GUI_PROC_ROOT}/${pane_pid}/cmdline" ]] || return 1
    command_line="$(tr '\0' ' ' <"${GZ_GUI_PROC_ROOT}/${pane_pid}/cmdline")"
    [[ "${command_line}" =~ (^|[[:space:]/])gz[[:space:]]sim[[:space:]]-g([[:space:]]|$) ||
       "${command_line}" =~ (^|[[:space:]/])gz-sim-gui([[:space:]]|$) ]]
}

gazebo_gui_pane() {
    local pane_index pane_dead pane_title pane_command pane_pid
    local starting_pane_index="" dead_pane_index=""
    session_exists || return 1
    while IFS=$'\t' read -r pane_index pane_dead pane_title pane_command pane_pid; do
        [[ "${pane_title}" == "Gazebo GUI" ]] || continue
        if [[ "${pane_dead}" == "0" ]]; then
            if gazebo_gui_process_running "${pane_pid}" "${pane_command}"; then
                printf '%s\t%s\t%s\n' "${pane_index}" "${pane_dead}" "${pane_command}"
                return 0
            fi
            [[ -n "${starting_pane_index}" ]] || starting_pane_index="${pane_index}"
            continue
        fi
        [[ -n "${dead_pane_index}" ]] || dead_pane_index="${pane_index}"
    done < <(tmux_command list-panes -t "${SESSION_NAME}:simulation" \
        -F $'#{pane_index}\t#{pane_dead}\t#{pane_title}\t#{pane_current_command}\t#{pane_pid}' 2>/dev/null || true)
    if [[ -n "${starting_pane_index}" ]]; then
        printf '%s\t0\tbash\n' "${starting_pane_index}"
        return 0
    fi
    if [[ -n "${dead_pane_index}" ]]; then
        printf '%s\t1\tbash\n' "${dead_pane_index}"
        return 0
    fi
    return 1
}

gazebo_gui_state() {
    local pane_index pane_dead pane_command
    if ! session_exists; then
        printf '%s\n' stopped
    elif IFS=$'\t' read -r pane_index pane_dead pane_command < <(gazebo_gui_pane); then
        if [[ "${pane_dead}" == "0" ]]; then
            if [[ "${pane_command}" == "bash" ]]; then
                printf '%s\n' starting
            else
                printf '%s\n' running
            fi
        else
            printf '%s\n' dead
        fi
    else
        printf '%s\n' missing
    fi
}

start_gazebo_gui() {
    local pane_index
    pane_index="$(tmux_command split-window -P -F '#{pane_index}' \
        -t "${SESSION_NAME}:simulation" -v \
        "$(tmux_gazebo_gui_shell_command)")"
    [[ -n "${pane_index}" ]] || {
        echo "Gazebo GUI pane was not created." >&2
        return 1
    }
    tmux_command select-pane -t "${SESSION_NAME}:simulation.${pane_index}" -T "Gazebo GUI"
}

respawn_gazebo_gui() {
    local pane_index="$1"
    # tmux retains a pane after its command exits. Reuse that space: a small
    # window may have no room for another split, especially after prior exits.
    tmux_command respawn-pane -t "${SESSION_NAME}:simulation.${pane_index}" \
        "$(tmux_gazebo_gui_shell_command)"
    tmux_command select-pane -t "${SESSION_NAME}:simulation.${pane_index}" -T "Gazebo GUI"
}

ensure_gazebo_gui() {
    local pane_index pane_dead pane_command
    case "$(gazebo_gui_state)" in
        running)
            return 0
            ;;
        starting)
            if wait_for_gazebo_gui; then
                return 0
            else
                local wait_status=$?
                if ((wait_status != 2)); then
                    echo "Timed out waiting for the Gazebo GUI pane to start." >&2
                    return 1
                fi
            fi
            IFS=$'\t' read -r pane_index pane_dead pane_command < <(gazebo_gui_pane)
            respawn_gazebo_gui "${pane_index}"
            wait_for_gazebo_gui || {
                echo "Gazebo GUI pane died before the viewer started." >&2
                return 1
            }
            return 0
            ;;
        dead)
            IFS=$'\t' read -r pane_index pane_dead pane_command < <(gazebo_gui_pane)
            respawn_gazebo_gui "${pane_index}"
            wait_for_gazebo_gui || {
                echo "Gazebo GUI pane died before the viewer started." >&2
                return 1
            }
            return 0
            ;;
        missing)
            ;;
        *)
            echo "Cannot open Gazebo GUI without simulation session ${SESSION_NAME}." >&2
            return 1
            ;;
    esac
    start_gazebo_gui
    wait_for_gazebo_gui || {
        case "$(gazebo_gui_state)" in
            dead) echo "Gazebo GUI pane died before the viewer started." >&2 ;;
            *) echo "Timed out waiting for the Gazebo GUI pane to start." >&2 ;;
        esac
        return 1
    }
}

wait_for_gazebo_gui() {
    local started_at="${SECONDS}"
    local state
    while :; do
        state="$(gazebo_gui_state)"
        case "${state}" in
            running)
                return 0
                ;;
            dead)
                return 2
                ;;
            starting)
                if ((SECONDS - started_at >= GZ_GUI_READY_TIMEOUT_SECONDS)); then
                    return 1
                fi
                sleep "${GZ_GUI_READY_POLL_INTERVAL_SECONDS}"
                ;;
            *)
                return 1
                ;;
        esac
    done
}

attach_simulation_tools() {
    if [[ "$(id -u)" -eq 0 && "${SESSION_USER}" != "root" ]] && id -u "${SESSION_USER}" >/dev/null 2>&1; then
        exec sudo -H -u "${SESSION_USER}" env \
            "DISPLAY=${DISPLAY:-}" \
            "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" \
            "XAUTHORITY=${XAUTHORITY:-}" \
            "XDG_RUNTIME_DIR=/run/user/$(id -u "${SESSION_USER}")" \
            "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}" \
            "WORKSPACE_DIR=${WORKSPACE_ROOT}" \
            tmux attach -t "=${SESSION_NAME}"
    fi
    exec tmux attach -t "=${SESSION_NAME}"
}

px4_simulation_process_groups() {
    local pane_pid
    local pid
    local pgid
    local comm
    local args

    # A live tmux session is the ownership boundary. Its pane processes are
    # isolated process-group leaders, and Gazebo spawned by PX4 remains in the
    # PX4 pane's process group.
    if session_exists; then
        while IFS= read -r pane_pid; do
            [[ -n "${pane_pid}" ]] || continue
            ps -o pgid= -p "${pane_pid}" 2>/dev/null | tr -d ' '
        done < <(tmux_command list-panes -t "${SESSION_NAME}:simulation" -F '#{pane_pid}' 2>/dev/null || true)
        # A dead pane legitimately has no process group. Do not leak the final
        # failed read/ps status into callers running under `set -e`.
        return 0
    fi

    # If tmux disappeared unexpectedly, only recover the explicitly selected
    # PX4 instance. Never sweep unrelated PX4, Gazebo, GUI, or build processes.
    ${PS_LIST_COMMAND} | while read -r pid pgid comm args; do
        [[ "${comm}" == "px4" ]] || continue
        case "${args}" in
            *"${PX4_BUILD_DIR}/bin/px4"*" -i ${PX4_INSTANCE}"*)
                if [[ "${pid}" != "$$" ]] && ! px4_owned_by_other_partition "${pid}"; then
                    printf '%s\n' "${pgid}"
                fi
                ;;
        esac
    done | sort -u
}

# SIM and the HIL workstation launcher share this PX4 build tree and instance
# number but run in different Gazebo partitions. A PX4 of this instance whose
# partition differs from this invocation's belongs to that other owner and must
# never be swept as "stale" (doing so silently killed a live HIL simulation).
px4_owned_by_other_partition() {
    local pid="$1" partition
    [[ -r "${PX4_PROC_ROOT}/${pid}/environ" ]] || return 1
    partition="$(tr '\0' '\n' <"${PX4_PROC_ROOT}/${pid}/environ" | sed -n 's/^GZ_PARTITION=//p' | head -n1)"
    [[ "${partition}" != "${GZ_PARTITION:-}" ]]
}

px4_foreign_instance_pids() {
    ${PS_LIST_COMMAND} | while read -r pid pgid comm args; do
        [[ "${comm}" == "px4" ]] || continue
        case "${args}" in
            *"${PX4_BUILD_DIR}/bin/px4"*" -i ${PX4_INSTANCE}"*)
                if px4_owned_by_other_partition "${pid}"; then
                    printf '%s\n' "${pid}"
                fi
                ;;
        esac
    done
}

cleanup_stale_px4_simulation() {
    local intent="${1:-start}" process_groups foreign
    foreign="$(px4_foreign_instance_pids | tr '\n' ' ')"
    if [[ -n "${foreign// /}" ]]; then
        if [[ "${intent}" == "stop" ]]; then
            echo "PX4 instance ${PX4_INSTANCE} belongs to another simulation owner (pid ${foreign% }); leaving it and its instance lock untouched." >&2
        else
            cat >&2 <<EOF
Refusing to start: PX4 instance ${PX4_INSTANCE} is owned by another simulation
(pid ${foreign% }, different Gazebo partition, e.g. the HIL workstation launcher).
Stop that owner first (./iii-dev hil stop), then retry.
EOF
            exit 2
        fi
    fi
    process_groups="$(px4_simulation_process_groups)"

    if [[ -z "${process_groups}" && ( -n "${foreign// /}" ||
          ( ! -e "/tmp/px4_lock-${PX4_INSTANCE}" && ! -e "/tmp/px4-sock-${PX4_INSTANCE}" ) ) ]]; then
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

    # The instance lock/socket belong to whichever PX4 holds them; never remove
    # them from under another live owner.
    if [[ -z "${foreign// /}" ]]; then
        rm -f "/tmp/px4_lock-${PX4_INSTANCE}" "/tmp/px4-sock-${PX4_INSTANCE}"
    fi
}

reset_px4_persistent_sim_params() {
    if [[ "${RESET_PX4_PARAMS_ON_RECREATE}" != "1" ]]; then
        return
    fi

    local rootfs="${PX4_BUILD_DIR}/rootfs"
    [[ -d "${rootfs}" ]] || return 0

    rm -f \
        "${rootfs}/parameters.bson" \
        "${rootfs}/parameters_backup.bson" \
        "${rootfs}/${PX4_INSTANCE}/parameters.bson" \
        "${rootfs}/${PX4_INSTANCE}/parameters_backup.bson"
}

sim_assets_root() {
    echo "$(cd "$(dirname "${SIM_ASSET_INSTALLER}")/.." && pwd)/Gazebo-simulation-assets"
}

px4_assets_installed() {
    local assets airframe
    assets="$(sim_assets_root)"
    # The selected model must be installed when it is one of the III assets;
    # PX4's own models (e.g. gz_x500) need no III installation.
    if [[ -d "${assets}/models/${PX4_GZ_MODEL_DIR}" ]]; then
        [[ -d "${PX4_ROOT}/Tools/simulation/gz/models/${PX4_GZ_MODEL_DIR}" ]] || return 1
    fi
    for airframe in "${assets}"/init.d-posix_airframes/*; do
        [[ -f "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/$(basename "${airframe}")" ]] &&
        grep -q "$(basename "${airframe}")" "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/CMakeLists.txt" || return 1
    done
}

# Every III vehicle model, world model (pylon and conductor collisions,
# conductors.yaml, radar scene), world and airframe is installed into PX4; a
# stale copy of any of them would fly an old asset.
px4_assets_current() {
    local assets entry
    assets="$(sim_assets_root)"

    px4_assets_installed || return 1
    for entry in "${assets}"/models/*/ "${assets}"/world_models/*/; do
        diff -rq "${entry}" "${PX4_ROOT}/Tools/simulation/gz/models/$(basename "${entry}")" >/dev/null || return 1
    done
    for entry in "${assets}"/worlds/*; do
        cmp -s "${entry}" "${PX4_ROOT}/Tools/simulation/gz/worlds/$(basename "${entry}")" || return 1
    done
    for entry in "${assets}"/init.d-posix_airframes/*; do
        cmp -s "${entry}" "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/$(basename "${entry}")" || return 1
    done
}

# The default PX4 command builds PX4 before starting it. A custom command (HIL)
# starts the built binary directly, which reads its airframes from the build
# tree, so rebuild when the installed airframe is newer than the build's copy.
ensure_px4_build_airframes_current() {
    if [[ -z "${III_SIM_TOOLS_PX4_COMMAND:-}" || "${III_SIM_TOOLS_ENSURE_ASSETS_WITH_CUSTOM_COMMAND:-0}" != "1" ]]; then
        return
    fi
    local airframe stale=0
    for airframe in "$(sim_assets_root)"/init.d-posix_airframes/*; do
        cmp -s \
            "${PX4_ROOT}/ROMFS/px4fmu_common/init.d-posix/airframes/$(basename "${airframe}")" \
            "${PX4_BUILD_DIR}/etc/init.d-posix/airframes/$(basename "${airframe}")" || stale=1
    done
    if ((stale == 0)); then
        return
    fi
    echo "Rebuilding PX4 SITL: the build tree holds a stale D4S airframe." >&2
    (
        source "${WORKSPACE_ROOT}/setup/setup_dev.bash" &&
        cd "${PX4_ROOT}" &&
        make px4_sitl_default
    )
}

ensure_px4_assets_current() {
    if [[ -n "${III_SIM_TOOLS_PX4_COMMAND:-}" && "${III_SIM_TOOLS_ENSURE_ASSETS_WITH_CUSTOM_COMMAND:-0}" != "1" ]]; then
        return
    fi

    if px4_assets_current; then
        return
    fi

    if [[ ! -x "${SIM_ASSET_INSTALLER}" ]]; then
        cat >&2 <<EOF
PX4 D4S Gazebo assets are missing or stale, but the installer is not executable:
  ${SIM_ASSET_INSTALLER}
EOF
        exit 1
    fi

    cat >&2 <<EOF
Installing/updating III Gazebo simulation assets into PX4:
  ${PX4_ROOT}
EOF
    "${SIM_ASSET_INSTALLER}" "${PX4_ROOT}"
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

tmux_gazebo_gui_shell_command() {
    local command
    printf -v command 'export GZ_IP=%q; ' "${GZ_IP}"
    if [[ -n "${GZ_PARTITION:-}" ]]; then
        printf -v command '%s export GZ_PARTITION=%q; ' "${command}" "${GZ_PARTITION}"
    else
        command+=' unset GZ_PARTITION; '
    fi
    command+="${GZ_GUI_COMMAND}"
    tmux_shell_command "${command}"
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

# PX4_SIM_MODEL of this invocation's running PX4 instance, or "none".
running_px4_sim_model() {
    local pid pgid comm args model
    ${PS_LIST_COMMAND} | while read -r pid pgid comm args; do
        [[ "${comm}" == "px4" ]] || continue
        case "${args}" in
            *"${PX4_BUILD_DIR}/bin/px4"*" -i ${PX4_INSTANCE}"*)
                if [[ -r "${PX4_PROC_ROOT}/${pid}/environ" ]] && ! px4_owned_by_other_partition "${pid}"; then
                    model="$(tr '\0' '\n' <"${PX4_PROC_ROOT}/${pid}/environ" | sed -n 's/^PX4_SIM_MODEL=//p' | head -n1)"
                    printf '%s\n' "${model:-unknown}"
                fi
                ;;
        esac
    done | head -n1
}

print_simulation_status() {
    local process_groups running_model
    process_groups="$(px4_simulation_process_groups || true)"
    running_model="$(running_px4_sim_model || true)"

    if session_exists; then
        echo "tmux_session: running"
        tmux_command list-panes -t "${SESSION_NAME}:simulation" -F "pane=#{pane_index} title=#{pane_title} active=#{pane_active} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}" 2>/dev/null || true
    else
        echo "tmux_session: stopped"
    fi

    echo "gazebo_gui: $(gazebo_gui_state)"
    echo "px4_sim_model_requested: ${PX4_SIM_MODEL_NAME}"
    echo "px4_sim_model_running: ${running_model:-none}"

    if [[ -n "${process_groups}" ]]; then
        echo "simulation_process_groups: ${process_groups//$'\n'/ }"
    else
        echo "simulation_process_groups: none"
    fi

    # Match the rendered GUI readiness gate. The world scene service is a
    # narrower and more stable probe than starting repeated topic discovery.
    if session_user_command timeout "${III_SIM_TOOLS_STATUS_DISCOVERY_TIMEOUT_SEC:-8}" bash -lc \
        "source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && gz service -i --service /world/${GZ_WORLD}/scene/info 2>&1 | grep -q 'Service providers'"; then
        echo "gazebo_transport: available"
    else
        echo "gazebo_transport: unavailable"
    fi

    if command -v ss >/dev/null 2>&1 && ss -H -u -l -n "sport = :${HOST_QGC_UDP_PORT}" 2>/dev/null | grep -q .; then
        echo "host_qgc_udp_listener: available"
    else
        echo "host_qgc_udp_listener: unavailable"
    fi
    echo "host_qgc_transport: host-network udp/${HOST_QGC_UDP_PORT} (lifecycle owned by iii qgc)"

    if [[ -e "/tmp/px4_lock-${PX4_INSTANCE}" || -e "/tmp/px4-sock-${PX4_INSTANCE}" ]]; then
        echo "px4_instance_state: lock_or_socket_present"
    else
        echo "px4_instance_state: no_lock_or_socket"
    fi
}

stop_simulation_tools() {
    cleanup_stale_px4_simulation stop

    if session_exists; then
        tmux_command kill-session -t "${SESSION_NAME}"
    fi
}

if ((STATUS)); then
    print_simulation_status
    exit 0
fi

if ((STOP)); then
    stop_simulation_tools
    print_simulation_status
    exit 0
fi

if ((ATTACH_ONLY)); then
    if ! session_exists; then
        echo "Simulation tmux session '${SESSION_NAME}' is not running." >&2
        exit 1
    fi
    attach_simulation_tools
fi

if ((RECREATE)) && session_exists; then
    cleanup_stale_px4_simulation
    tmux_command kill-session -t "${SESSION_NAME}"
    reset_px4_persistent_sim_params
elif ! session_exists; then
    cleanup_stale_px4_simulation
    reset_px4_persistent_sim_params
fi

if ! session_exists; then
    # The build tree and installed Gazebo assets belong to the PX4 process.
    # Viewer-only repair of a live session must not rewrite either one.
    ensure_px4_assets_current
    refresh_px4_build_cache_if_needed
    ensure_px4_build_airframes_current
    tmux_command new-session -d -s "${SESSION_NAME}" -n "simulation" "$(tmux_shell_command "${PX4_COMMAND}")"
    tmux_command set-option -t "${SESSION_NAME}" remain-on-exit on
    tmux_command select-pane -t "${SESSION_NAME}:simulation.0" -T "PX4 / Gazebo"
fi

# A rendered request is also a repair request.  Do not recreate a healthy
# PX4/Gazebo pane merely because an operator closed the viewer; a repeat start
# must preserve the running simulator and any active mission.
if ((HEADLESS == 0)); then
    ensure_gazebo_gui
fi

if ((ATTACH)); then
    attach_simulation_tools
fi
