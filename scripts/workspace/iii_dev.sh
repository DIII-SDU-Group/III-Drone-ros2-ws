#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
III_DEV_WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
export III_DEV_WORKSPACE_ROOT

# shellcheck source=scripts/workspace/lib/iii_dev_container.sh
source "${SCRIPT_DIR}/lib/iii_dev_container.sh"
# shellcheck source=scripts/workspace/lib/iii_dev_readiness.sh
source "${SCRIPT_DIR}/lib/iii_dev_readiness.sh"

SIM_SCRIPT="${III_DEV_CONTAINER_WORKSPACE}/tools/simulation/launch_simulation_tools.sh"
HIL_RESTART_SCRIPT="${III_DEV_HIL_COORDINATOR:-${III_DEV_WORKSPACE_ROOT}/scripts/workspace/coordinate_hil_restart.py}"
III_GC_INSTALL_ROOT="${III_GC_INSTALL_ROOT:-${XDG_DATA_HOME:-${HOME}/.local/share}/iii/gc}"
export III_GC_INSTALL_ROOT
III_GC_INSTALL_PROFILE="${III_GC_INSTALL_PROFILE:-unknown}"
export III_GC_INSTALL_PROFILE
GC_SCRIPT="${III_DEV_GC_SCRIPT:-${III_GC_INSTALL_ROOT}/workspace/scripts/workspace/iii_ground_control.sh}"
QGC_CLI="${III_DEV_QGC_CLI:-${HOME}/.local/bin/iii}"
HOST_III_CLI="${III_DEV_NATIVE_CLI:-${HOME}/.local/bin/iii}"
BROWSER_WINDOW_SCRIPT="${III_DEV_BROWSER_WINDOW_SCRIPT:-${III_DEV_WORKSPACE_ROOT}/scripts/workspace/open_default_browser_window.py}"
HIL_GC_BIND_SCRIPT="${III_DEV_HIL_GC_BIND_SCRIPT:-${III_DEV_WORKSPACE_ROOT}/scripts/workspace/bind_hil_gc_target.py}"
HIL_LOG_DIR="${III_DEV_HIL_LOG_DIR:-${III_DEV_WORKSPACE_ROOT}/runtime_logs/hil}"
RUNTIME_API_HEALTH_URL="${III_DEV_RUNTIME_API_HEALTH_URL:-http://127.0.0.1:8765/health}"
RUNTIME_API_VEHICLE_STATUS_URL="${III_DEV_RUNTIME_API_VEHICLE_STATUS_URL:-${RUNTIME_API_HEALTH_URL%/health}/vehicle/status}"

usage() {
    cat <<'EOF'
Usage: ./iii-dev <command> [arguments]

Host workspace commands:
  container status|up|down  Inspect, start, or stop the workspace devcontainer
  shell                     Open a configured interactive container shell
  exec <command> [args...]  Run an arbitrary configured container command

  sim start [options]       Ensure rendered PX4/Gazebo/QGC simulation is running
  sim restart [options]     Recreate the simulation without attaching
  sim attach                Attach to the simulation tmux session
  sim status|stop           Inspect or stop the simulation

  hil start [--headless] [--host HOST]   Start HIL and open operator windows
  hil restart [--headless] [--host HOST] Restart HIL and open operator windows
  hil status [--json] [--host HOST]      Show aggregate HIL state
  hil logs [--follow]      Show the latest captured HIL command log
  hil stop [--host HOST]   Stop owned HIL components and the Pi runtime

  tmux list                 List container tmux sessions
  tmux attach <session>     Attach to an exact container tmux session

  stack start [options]     Start simulation, III runtime, and GUI
  stack status              Show aggregate container/sim/system/API/GUI status
  stack attach [system|sim] Attach to an operator tmux view
  stack stop                Stop GUI, III runtime, and simulation

Native command migration:
  iii --runtime-target sim system <arguments>
  iii --runtime-target sim api <action>
  iii --runtime-target sim rosbag <action> [options]
  <III_GC_INSTALL_ROOT>/workspace/scripts/workspace/iii_ground_control.sh <action> [options]

Stack start options:
  --headless                Do not start Gazebo GUI or QGroundControl
  --sim-model gz_<model>    PX4 simulation model, e.g. gz_d4s_dc_drone_powerline_eval
  --recreate-sim            Recreate SITL and clear its persistent parameters
  --no-gui                  Do not start the ground-control web application

Environment overrides:
  III_DEV_SIM_READY_TIMEOUT_SEC  Simulation readiness timeout (default: 300)
  III_DEV_CONTAINER_USER         Container user (default: iii)
  III_DEV_CONTAINER_WORKSPACE    Container workspace (default: /home/iii/ws)
  III_DEV_DEVCONTAINER_BIN       Dev Container CLI executable
  III_DEV_RUNTIME_API_HEALTH_URL Runtime API health URL
  III_DEV_RUNTIME_API_VEHICLE_STATUS_URL Runtime API vehicle readiness URL
  III_DEV_NATIVE_CLI              Installed native `iii` command for stack operations
  III_GC_INSTALL_ROOT            Installed host GC/QGC snapshot (default: XDG data path)
  III_DEV_QGC_CLI                Override installed host CLI for QGroundControl
EOF
}

resolve_gc_install_profile() {
    if [[ "${III_GC_INSTALL_PROFILE}" == "unknown" ]]; then
        III_GC_INSTALL_PROFILE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("profile", "unknown"))' "${III_GC_INSTALL_ROOT}/install.json" 2>/dev/null || printf unknown)"
        export III_GC_INSTALL_PROFILE
    fi
    printf '%s\n' "${III_GC_INSTALL_PROFILE}"
}

help_requested() {
    local argument
    for argument in "$@"; do
        [[ "${argument}" == "-h" || "${argument}" == "--help" ]] && return 0
    done
    return 1
}

command_usage() {
    local command="$1"
    local action="${2:-}"

    case "${command}:${action}" in
        container:)
            printf 'Usage: ./iii-dev container {status|up|down}\n'
            ;;
        container:status|container:up|container:down)
            printf 'Usage: ./iii-dev container %s\n' "${action}"
            ;;
        shell:)
            printf 'Usage: ./iii-dev shell\n'
            ;;
        exec:)
            printf 'Usage: ./iii-dev exec <command> [args...]\n'
            ;;
        sim:)
            printf 'Usage: ./iii-dev sim {start|restart|attach|status|stop}\n'
            ;;
        sim:start|sim:restart)
            printf 'Usage: ./iii-dev sim %s [--headless] [--sim-model gz_<model>]\n' "${action}"
            ;;
        sim:attach|sim:status|sim:stop)
            printf 'Usage: ./iii-dev sim %s\n' "${action}"
            ;;
        hil:)
            printf 'Usage: ./iii-dev hil {start|restart|status|logs|stop}\n'
            ;;
        hil:start|hil:restart)
            printf 'Usage: ./iii-dev hil %s [--headless] [--verbose] [--host HOST]\n' "${action}"
            ;;
        hil:status)
            printf 'Usage: ./iii-dev hil status [--json] [--verbose] [--host HOST]\n'
            ;;
        hil:logs)
            printf 'Usage: ./iii-dev hil logs [--follow]\n'
            ;;
        hil:stop)
            printf 'Usage: ./iii-dev hil stop [--verbose] [--host HOST]\n'
            ;;
        tmux:)
            printf 'Usage: ./iii-dev tmux {list|attach <session>}\n'
            ;;
        tmux:list)
            printf 'Usage: ./iii-dev tmux list\n'
            ;;
        tmux:attach)
            printf 'Usage: ./iii-dev tmux attach <session>\n'
            ;;
        stack:)
            printf 'Usage: ./iii-dev stack {start|status|attach|stop}\n'
            ;;
        stack:start)
            printf 'Usage: ./iii-dev stack start [--headless] [--recreate-sim] [--no-gui]\n'
            ;;
        stack:status|stack:stop)
            printf 'Usage: ./iii-dev stack %s\n' "${action}"
            ;;
        stack:attach)
            printf 'Usage: ./iii-dev stack attach [system|sim]\n'
            ;;
        *)
            iii_dev_die "No help is available for ${command}${action:+ ${action}}."
            ;;
    esac
}

section() {
    printf '\n== %s ==\n' "$1"
}

require_no_args() {
    local label="$1"
    shift
    if (($# != 0)); then
        iii_dev_die "${label} does not accept arguments: $*"
    fi
}

removed_command_guidance() {
    local command="$1"
    shift
    case "${command}" in
        system)
            iii_dev_error "iii-dev system was removed. Use: iii --runtime-target sim system $*"
            ;;
        api)
            iii_dev_error "iii-dev api was removed. Use: iii --runtime-target sim api $*"
            ;;
        rosbag)
            iii_dev_error "iii-dev rosbag was removed. Use: iii --runtime-target sim rosbag $*"
            ;;
        gui)
            iii_dev_error "iii-dev gui was removed. Use the installed GC launcher at ${GC_SCRIPT} $*"
            ;;
    esac
    return 2
}

run_native_cli() {
    [[ -x "${HOST_III_CLI}" ]] || {
        iii_dev_die "Installed native III CLI is missing or not executable: ${HOST_III_CLI}"
        return 1
    }
    env -u PYTHONPATH III_GC_INSTALL_ROOT="${III_GC_INSTALL_ROOT}" \
        "${HOST_III_CLI}" --runtime-target sim "$@"
}

run_system() {
    if [[ "${1:-}" == "boot" ]]; then
        iii_dev_repair_generated_ownership
    fi
    run_native_cli system "$@"
}

run_system_mutation() {
    local action="$1"
    shift
    local operation_id="iii-dev-${action}-$(date +%s)-${BASHPID}"

    # System mutations use the same durable two-stage contract as direct CLI
    # operation: retain the exact plan first, then apply that plan explicitly.
    run_system "${action}" "$@" \
        --dry-run --operation-id "${operation_id}" --output=json
    run_system "${action}" "$@" \
        --operation-id "${operation_id}" --confirm --non-interactive --output=json
}

run_sim() {
    local action="${1:-}"
    local argument
    local headless=0
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage sim
        return
    fi
    shift
    if help_requested "$@"; then
        command_usage sim "${action}"
        return
    fi

    case "${action}" in
        start|restart)
            local sim_args=()
            while (($# > 0)); do
                case "$1" in
                    --headless)
                        headless=1
                        sim_args+=(--headless)
                        ;;
                    --sim-model)
                        if (($# < 2)); then
                            iii_dev_die "sim ${action} --sim-model needs a gz_<model> value."
                            return
                        fi
                        sim_args+=(--sim-model "$2")
                        shift
                        ;;
                    *)
                        iii_dev_die "sim ${action} accepts only --headless and --sim-model gz_<model>."
                        return
                        ;;
                esac
                shift
            done
            if [[ "${action}" == "start" ]]; then
                iii_dev_exec never "${SIM_SCRIPT}" --no-attach "${sim_args[@]}"
            else
                iii_dev_exec never "${SIM_SCRIPT}" --recreate --no-attach "${sim_args[@]}"
            fi
            ((headless)) || sim_start_qgc
            ;;
        attach)
            require_no_args "sim attach" "$@" || return
            iii_dev_exec interactive "${SIM_SCRIPT}" --attach
            ;;
        status)
            require_no_args "sim status" "$@" || return
            iii_dev_exec never "${SIM_SCRIPT}" --status
            printf 'Host GC install: %s · profile=%s · host=%s\n' \
                "${III_GC_INSTALL_ROOT}" "$(resolve_gc_install_profile)" "$(hostname)"
            ;;
        stop)
            require_no_args "sim stop" "$@" || return
            iii_dev_exec never "${SIM_SCRIPT}" --stop
            ;;
        *)
            iii_dev_die "Unknown simulation action: ${action}"
            ;;
    esac
}

sim_start_qgc() {
    local operation_id="iii-dev-qgc-$(date +%s%N)-${BASHPID}"
    [[ -x "${QGC_CLI}" ]] || {
        iii_dev_die "Installed III host CLI is missing or not executable: ${QGC_CLI}"
        return 1
    }
    env -u PYTHONPATH III_GC_INSTALL_ROOT="${III_GC_INSTALL_ROOT}" \
        "${QGC_CLI}" qgc start --dry-run --operation-id "${operation_id}" --output=json || return
    env -u PYTHONPATH III_GC_INSTALL_ROOT="${III_GC_INSTALL_ROOT}" \
        "${QGC_CLI}" qgc start --operation-id "${operation_id}" --confirm --non-interactive --output=json
}

hil_prepare_log() {
    local action="$1"
    local timestamp
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${HIL_LOG_DIR}"
    HIL_LOG_FILE="${HIL_LOG_DIR}/hil-${action}-${timestamp}-$$.log"
    : >"${HIL_LOG_FILE}"
    ln -sfn "$(basename "${HIL_LOG_FILE}")" "${HIL_LOG_DIR}/latest.log"
    export III_HIL_LOG_PATH="${HIL_LOG_DIR}/latest.log"
}

hil_run_logged() {
    local verbose="$1"
    shift
    if ((verbose)); then
        set +e
        "$@" 2>&1 | tee -a "${HIL_LOG_FILE}"
        local result="${PIPESTATUS[0]}"
        set -e
        return "${result}"
    fi
    "$@" >>"${HIL_LOG_FILE}" 2>&1
}

hil_gc_lock_path() {
    local project="${III_GC_COMPOSE_PROJECT:-iii-ground-control}"
    local lock_dir="${III_GC_LOCK_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iii-ground-control}"
    local safe_project
    safe_project="$(printf '%s' "${project}" | tr -c 'A-Za-z0-9_.-' '_')"
    printf '%s/%s.lock\n' "${lock_dir}" "${safe_project}"
}

hil_acquire_ground_control_lock() {
    local path
    command -v flock >/dev/null 2>&1 || { echo "flock is required to serialize ground-control lifecycle operations." >&2; return 1; }
    path="${III_GC_LOCK_PATH:-$(hil_gc_lock_path)}"
    mkdir -p "$(dirname "${path}")"
    exec 8>"${path}"
    flock -x 8
    export III_GC_LOCK_PATH="${path}" III_GC_LOCK_HELD=1
}

hil_release_ground_control_lock() {
    flock -u 8 2>/dev/null || true
    exec 8>&-
}

hil_launch_group() {
    local output="$1" identity_file="$2" release_file="$3"
    shift 3
    # Keep the lifecycle lock owned by this parent transaction. Child
    # processes must not inherit its descriptor, otherwise an orphaned child
    # could keep the lock held after the parent exits unexpectedly.
    setsid -- bash -c '
        exec 8>&- 9>&-
        trap - INT TERM HUP
        identity_file="$1"
        release_file="$2"
        shift 2
        if [[ -n "${III_HIL_GATE_PUBLISH_DELAY_SEC:-}" ]]; then
            sleep "${III_HIL_GATE_PUBLISH_DELAY_SEC}"
        fi
        stat_line="$(<"/proc/$$/stat")" || exit 125
        remainder="${stat_line##*") "}"
        read -r -a stat_fields <<<"${remainder}"
        (( ${#stat_fields[@]} >= 20 )) || exit 125
        identity_tmp="${identity_file}.$$"
        printf "%s %s %s\n" "$$" "${stat_fields[2]}" "${stat_fields[19]}" >"${identity_tmp}" || exit 125
        mv -f -- "${identity_tmp}" "${identity_file}" || exit 125
        while [[ ! -e "${release_file}" ]]; do
            [[ -r "/proc/$$/stat" ]] || exit 125
            sleep 0.005
        done
        exec "$@"
    ' hil-child "${identity_file}" "${release_file}" "$@" >"${output}" 2>&1 &
    pending_child_pid=$!
}

hil_child_pgid() {
    ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '
}

hil_child_identity() {
    local pid="$1" stat_line remainder
    local -a stat_fields=()
    [[ -n "${pid}" && -r "/proc/${pid}/stat" ]] || return 1
    stat_line="$(<"/proc/${pid}/stat")" || return 1
    # comm is parenthesized and may itself contain spaces or ')'. The final
    # ') ' is the only reliable delimiter before field 3 (state).
    remainder="${stat_line##*') '}"
    read -r -a stat_fields <<<"${remainder}"
    (( ${#stat_fields[@]} >= 20 )) || return 1
    printf '%s %s\n' "${stat_fields[2]}" "${stat_fields[19]}"
}

hil_wait_child_identity() {
    local pid="$1" identity_file="$2" identity attempt max_attempts
    local recorded_pid recorded_pgid recorded_start_ticks current_pgid current_start_ticks
    local timeout_ms="${III_HIL_GATE_TIMEOUT_MS:-2000}"
    [[ "${timeout_ms}" =~ ^[0-9]+$ ]] || timeout_ms=2000
    max_attempts=$(( (timeout_ms + 4) / 5 ))
    (( max_attempts > 0 )) || max_attempts=1
    for ((attempt = 0; attempt < max_attempts; attempt++)); do
        if [[ -s "${identity_file}" ]] &&
            read -r recorded_pid recorded_pgid recorded_start_ticks <"${identity_file}" &&
            [[ "${recorded_pid}" == "${pid}" && -n "${recorded_pgid}" && -n "${recorded_start_ticks}" ]] &&
            read -r current_pgid current_start_ticks < <(hil_child_identity "${pid}") &&
            [[ "${current_pgid}" == "${recorded_pgid}" &&
                "${current_start_ticks}" == "${recorded_start_ticks}" &&
                "${current_pgid}" != "${parent_pgid}" ]]; then
            hil_captured_pgid="${recorded_pgid}"
            hil_captured_start_ticks="${recorded_start_ticks}"
            return 0
        fi
        kill -0 "${pid}" 2>/dev/null || return 1
        sleep 0.005
    done
    return 1
}

hil_release_child_gate() {
    : >"$1"
}

hil_clear_child_identity() {
    case "$1" in
        coordinator)
            coordinator_pid=""
            coordinator_pgid=""
            coordinator_start_ticks=""
            ;;
        ground-control)
            gc_pid=""
            gc_pgid=""
            gc_start_ticks=""
            ;;
    esac
}

hil_show_coordinator_progress() {
    local pid="$1" output="$2" line marker stage state detail extra
    local last_detail="Pi/workstation lifecycle" last_update=${SECONDS} progress_fd
    local heartbeat_seconds="${III_HIL_PROGRESS_HEARTBEAT_SEC:-20}"
    [[ "${heartbeat_seconds}" =~ ^[1-9][0-9]*$ ]] || heartbeat_seconds=20
    exec {progress_fd}<"${output}"
    while :; do
        # The coordinator writes flushed, single-line progress records to its
        # captured log. Keep all other output in that log for diagnostics.
        while IFS= read -r -t 0.01 line <&"${progress_fd}"; do
            case "${line}" in
                III_HIL_PROGRESS\|*)
                    IFS='|' read -r marker stage state detail extra <<<"${line}"
                    [[ "${marker}" == "III_HIL_PROGRESS" &&
                        "${stage}" =~ ^[a-z_]+$ &&
                        "${state}" =~ ^(start|done|waiting|update|failed)$ &&
                        -n "${detail}" && -z "${extra}" ]] || continue
                    case "${state}" in
                        waiting|update) state=wait ;;
                        failed) state=fail ;;
                    esac
                    printf '[%s] %s\n' "${state}" "${detail}"
                    last_detail="${detail}"
                    last_update=${SECONDS}
                    ;;
            esac
        done
        kill -0 "${pid}" 2>/dev/null || break
        if ((SECONDS - last_update >= heartbeat_seconds)); then
            printf '  … %s still running\n' "${last_detail}"
            last_update=${SECONDS}
        fi
        sleep 0.2
    done
    # Drain records written just before the child exited. The following wait
    # still owns its exit status and the existing failure/rollback path.
    while IFS= read -r line <&"${progress_fd}"; do
        case "${line}" in
            III_HIL_PROGRESS\|*)
                IFS='|' read -r marker stage state detail extra <<<"${line}"
                [[ "${marker}" == "III_HIL_PROGRESS" &&
                    "${stage}" =~ ^[a-z_]+$ &&
                    "${state}" =~ ^(start|done|waiting|update|failed)$ &&
                    -n "${detail}" && -z "${extra}" ]] || continue
                case "${state}" in
                    waiting|update) state=wait ;;
                    failed) state=fail ;;
                esac
                printf '[%s] %s\n' "${state}" "${detail}"
                ;;
        esac
    done
    exec {progress_fd}<&-
}

hil_show_child_heartbeat() {
    local pid="$1" label="$2" last_update=${SECONDS}
    local heartbeat_seconds="${III_HIL_PROGRESS_HEARTBEAT_SEC:-20}"
    [[ "${heartbeat_seconds}" =~ ^[1-9][0-9]*$ ]] || heartbeat_seconds=20
    while kill -0 "${pid}" 2>/dev/null; do
        if ((SECONDS - last_update >= heartbeat_seconds)); then
            printf '  … %s still running\n' "${label}"
            last_update=${SECONDS}
        fi
        sleep 0.2
    done
}

hil_terminate_children() {
    local own_pgid pid pgid start_ticks current_pgid current_start_ticks gate
    local coordinator_to_reap="${coordinator_pid:-}" gc_to_reap="${gc_pid:-}"
    if [[ "${coordinator_gate:-0}" == "1" && -z "${coordinator_to_reap}" &&
        "${pending_child_role:-}" == "coordinator" ]]; then
        coordinator_to_reap="${pending_child_pid:-}"
    fi
    if [[ "${gc_gate:-0}" == "1" && -z "${gc_to_reap}" &&
        "${pending_child_role:-}" == "ground-control" ]]; then
        gc_to_reap="${pending_child_pid:-}"
    fi
    if [[ -z "${coordinator_to_reap}" && -z "${gc_to_reap}" &&
        -z "${coordinator_pgid:-}" && -z "${gc_pgid:-}" ]]; then
        return 0
    fi
    own_pgid=""
    read -r own_pgid _ < <(hil_child_identity "$$") || own_pgid=""
    for pid in "${coordinator_to_reap}" "${gc_to_reap}"; do
        [[ -n "${pid}" ]] || continue
        if [[ "${pid}" == "${coordinator_to_reap}" ]]; then
            pgid="${coordinator_pgid:-}"
            start_ticks="${coordinator_start_ticks:-}"
            gate="${coordinator_gate:-0}"
        else
            pgid="${gc_pgid:-}"
            start_ticks="${gc_start_ticks:-}"
            gate="${gc_gate:-0}"
        fi
        if [[ "${gate}" == "1" ]]; then
            # Before gate release this PID is still the direct child gate,
            # so direct termination is safe even without group identity.
            kill -TERM "${pid}" 2>/dev/null || true
            continue
        fi
        [[ -n "${pgid}" && -n "${start_ticks}" ]] || continue
        if ! read -r current_pgid current_start_ticks < <(hil_child_identity "${pid}"); then
            continue
        fi
        [[ "${current_pgid}" == "${pgid}" && "${current_start_ticks}" == "${start_ticks}" ]] || continue
        [[ "${current_pgid}" != "${own_pgid}" ]] || continue
        kill -TERM -- "-${current_pgid}" 2>/dev/null || true
    done
    set +e
    if [[ -n "${coordinator_to_reap}" ]]; then
        wait "${coordinator_to_reap}"
        hil_clear_child_identity coordinator
    fi
    if [[ -n "${gc_to_reap}" ]]; then
        wait "${gc_to_reap}"
        hil_clear_child_identity ground-control
    fi
    set -e
}

hil_terminate_status_probe() {
    local pid="${status_probe_pid:-}" current_pgid current_start_ticks attempt
    if [[ -z "${pid}" && "${pending_child_role:-}" == "final-status" ]]; then
        pid="${pending_child_pid:-}"
    fi
    [[ -n "${pid}" ]] || return 0
    if [[ "${status_probe_gate:-0}" == "1" ]]; then
        kill -TERM "${pid}" 2>/dev/null || true
    elif read -r current_pgid current_start_ticks < <(hil_child_identity "${pid}") &&
        [[ "${current_pgid}" == "${status_probe_pgid:-}" &&
            "${current_start_ticks}" == "${status_probe_start_ticks:-}" &&
            "${current_pgid}" != "${parent_pgid:-}" ]]; then
        kill -TERM -- "-${current_pgid}" 2>/dev/null || true
        for ((attempt = 0; attempt < 10; attempt++)); do
            kill -0 "${pid}" 2>/dev/null || break
            sleep 0.01
        done
        if read -r current_pgid current_start_ticks < <(hil_child_identity "${pid}") &&
            [[ "${current_pgid}" == "${status_probe_pgid:-}" &&
                "${current_start_ticks}" == "${status_probe_start_ticks:-}" ]]; then
            kill -KILL -- "-${current_pgid}" 2>/dev/null || true
        fi
    fi
    wait "${pid}" 2>/dev/null || true
    status_probe_pid=""
    status_probe_pgid=""
    status_probe_start_ticks=""
    status_probe_gate=0
}

hil_append_child_log() {
    local label="$1" output="$2"
    printf '\n--- %s ---\n' "${label}" >>"${HIL_LOG_FILE}"
    cat "${output}" >>"${HIL_LOG_FILE}"
    if ((verbose)); then
        cat "${output}" >&2
    fi
}

hil_append_transaction_logs() {
    ((transaction_logs_appended == 1)) && return 0
    hil_append_child_log coordinator "${coordinator_output}"
    hil_append_child_log ground-control "${gc_output}"
    transaction_logs_appended=1
}

hil_cleanup_transaction() {
    local result="$1"
    local stage="$2"
    local rollback_allowed="$3"
    local rollback_result=0
    ((cleanup_done == 1)) && return "${result}"
    cleanup_done=1
    trap - INT TERM HUP EXIT
    set +e
    hil_terminate_status_probe
    if [[ "${status_probe_output_pending:-0}" == "1" && -f "${status_probe_output:-}" ]]; then
        cat "${status_probe_output}" >>"${HIL_LOG_FILE}"
        status_probe_output_pending=0
    fi
    hil_terminate_children
    hil_append_transaction_logs
    if ((rollback_allowed == 1 && gc_preexisting == 0 && rollback_done == 0)); then
        rollback_done=1
        printf 'Rolling back ground-control startup.\n' >&2
        env III_GC_LOCK_HELD=1 III_GC_LOCK_PATH="${III_GC_LOCK_PATH}" "${GC_SCRIPT}" stop >"${rollback_output}" 2>&1
        rollback_result=$?
        hil_append_child_log rollback "${rollback_output}"
        if ((rollback_result != 0)); then
            printf 'Ground-control rollback failed; inspect the HIL status before retrying.\n' >&2
        fi
    fi
    hil_release_ground_control_lock
    rm -rf "${transaction_dir}"
    if ((result != 0)); then
        hil_failure "${action}" "${stage}"
    fi
    return "${result}"
}

hil_exit_cleanup() {
    local result=$?
    [[ "${transaction_active:-0}" == "1" && "${cleanup_done:-0}" == "0" ]] || return 0
    hil_cleanup_transaction "${result}" "unexpected transaction exit" 1
    return 0
}

hil_signal_handler() {
    local signal="$1" signal_code
    case "${signal}" in
        INT) signal_code=130 ;;
        TERM) signal_code=143 ;;
        HUP) signal_code=129 ;;
    esac
    trap - INT TERM HUP
    set +e
    hil_cleanup_transaction "${signal_code}" "signal SIG${signal}" 1
    printf 'HIL interrupted by SIG%s; captured diagnostics are in %s.\n' "${signal}" "${HIL_LOG_FILE}" >&2
    exit "${signal_code}"
}

# Store the current time in nanoseconds in the named variable without
# spawning a process (bash 5 EPOCHREALTIME; date fallback for older shells).
hil_now_ns() {
    local seconds fraction
    if [[ -n "${EPOCHREALTIME:-}" ]]; then
        seconds="${EPOCHREALTIME%[.,]*}"
        fraction="${EPOCHREALTIME#*[.,]}000000"
        printf -v "$1" '%d' "$((seconds * 1000000000 + 10#${fraction:0:6} * 1000))"
    else
        printf -v "$1" '%s' "$(date +%s%N)"
    fi
}

hil_capture_status_json() {
    local verbose="$1" output_var="$2"
    local output result state="" parse_result now_ns deadline_ns probe_deadline_ns remaining_ms sleep_ms sleep_seconds
    local recorded_pid recorded_pgid recorded_start_ticks current_pgid current_start_ticks probe_timed_out
    # The overall gate retries a valid non-ready state until its deadline.
    # Each individual status command is bounded separately so one hung probe
    # is terminated and retried instead of consuming the whole gate.
    local timeout_ms="${III_DEV_HIL_FINAL_STATUS_TIMEOUT_MS:-90000}"
    local call_timeout_ms="${III_DEV_HIL_FINAL_STATUS_CALL_TIMEOUT_MS:-60000}"
    if [[ ! "${timeout_ms}" =~ ^[0-9]+$ ]] || ((timeout_ms < 1 || timeout_ms > 600000)); then
        echo "Final HIL health check failed: invalid final status deadline ${timeout_ms}." >&2
        return 1
    fi
    if [[ ! "${call_timeout_ms}" =~ ^[0-9]+$ ]] || ((call_timeout_ms < 1 || call_timeout_ms > 600000)); then
        echo "Final HIL health check failed: invalid final status call timeout ${call_timeout_ms}." >&2
        return 1
    fi
    hil_now_ns now_ns
    deadline_ns=$((now_ns + timeout_ms * 1000000))
    while :; do
        hil_now_ns now_ns
        if ((now_ns >= deadline_ns)); then
            echo "Final HIL health check failed: expected state=ready, got state=${state:-<missing>} after ${timeout_ms} ms." >&2
            return 1
        fi
        status_probe_output="${transaction_dir}/final-status.log"
        status_probe_identity_file="${transaction_dir}/final-status.identity"
        status_probe_release_file="${transaction_dir}/final-status.release"
        rm -f -- "${status_probe_identity_file}" "${status_probe_release_file}"
        : >"${status_probe_output}"
        status_probe_gate=1
        pending_child_role=final-status
        hil_launch_group "${status_probe_output}" "${status_probe_identity_file}" "${status_probe_release_file}" \
            python3 "${HIL_RESTART_SCRIPT}" status --json
        status_probe_pid="${pending_child_pid}"
        pending_child_pid=""
        pending_child_role=""
        status_probe_output_pending=1
        probe_deadline_ns=$((now_ns + call_timeout_ms * 1000000))
        ((probe_deadline_ns <= deadline_ns)) || probe_deadline_ns=${deadline_ns}
        while :; do
            if [[ -s "${status_probe_identity_file}" ]] &&
                read -r recorded_pid recorded_pgid recorded_start_ticks <"${status_probe_identity_file}" &&
                [[ "${recorded_pid}" == "${status_probe_pid}" && -n "${recorded_pgid}" && -n "${recorded_start_ticks}" ]] &&
                read -r current_pgid current_start_ticks < <(hil_child_identity "${status_probe_pid}") &&
                [[ "${current_pgid}" == "${recorded_pgid}" &&
                    "${current_start_ticks}" == "${recorded_start_ticks}" &&
                    "${current_pgid}" != "${parent_pgid}" ]]; then
                status_probe_pgid="${recorded_pgid}"
                status_probe_start_ticks="${recorded_start_ticks}"
                hil_release_child_gate "${status_probe_release_file}"
                status_probe_gate=0
                break
            fi
            hil_now_ns now_ns
            if ((now_ns >= deadline_ns)) || ! kill -0 "${status_probe_pid}" 2>/dev/null; then
                hil_terminate_status_probe
                if [[ -n "${state}" ]]; then
                    echo "Final HIL health check failed: expected state=ready, got state=${state} after ${timeout_ms} ms." >&2
                    return 1
                fi
                echo "Final HIL health check failed: status command did not start within ${timeout_ms} ms." >&2
                return 124
            fi
            sleep 0.005
        done
        probe_timed_out=0
        while kill -0 "${status_probe_pid}" 2>/dev/null; do
            hil_now_ns now_ns
            if ((now_ns >= probe_deadline_ns)); then
                probe_timed_out=1
                hil_terminate_status_probe
                break
            fi
            sleep 0.05
        done
        if ((probe_timed_out)); then
            result=124
        else
            set +e
            wait "${status_probe_pid}"
            result=$?
            set -e
            status_probe_pid=""
            status_probe_pgid=""
            status_probe_start_ticks=""
        fi
        output="$(cat "${status_probe_output}")"
        printf '%s\n' "${output}" >>"${HIL_LOG_FILE}"
        status_probe_output_pending=0
        if ((verbose)); then
            printf '%s\n' "${output}" >&2
        fi
        if ((probe_timed_out)) && ((probe_deadline_ns < deadline_ns)); then
            # Only this status command exceeded its own bound; the overall
            # gate still has time, so retry with a fresh probe.
            printf 'Final HIL health check: status command exceeded %s ms call timeout; retrying within %s ms deadline.\n' \
                "${call_timeout_ms}" "${timeout_ms}" >>"${HIL_LOG_FILE}"
            continue
        fi
        if ((probe_timed_out)); then
            if [[ -n "${state}" ]]; then
                echo "Final HIL health check failed: expected state=ready, got state=${state} after ${timeout_ms} ms; latest status command timed out." >&2
                return 1
            fi
            echo "Final HIL health check failed: status command exceeded ${timeout_ms} ms deadline; last state=${state:-<none>}." >&2
            return 124
        fi
        if ((result != 0)); then
            echo "Final HIL health check failed: status command exited ${result}; see ${HIL_LOG_FILE}." >&2
            return "${result}"
        fi
        set +e
        state="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", ""))' <<<"${output}" 2>/dev/null)"
        parse_result=$?
        set -e
        if ((parse_result != 0)); then
            echo "Final HIL health check failed: status output was not valid JSON." >&2
            return 1
        fi
        hil_now_ns now_ns
        if [[ "${state}" == "ready" ]] && ((now_ns <= deadline_ns)); then
            printf -v "${output_var}" '%s' "${output}"
            return 0
        fi
        remaining_ms=$(((deadline_ns - now_ns) / 1000000))
        if ((remaining_ms <= 0)); then
            echo "Final HIL health check failed: expected state=ready, got state=${state:-<missing>} after ${timeout_ms} ms." >&2
            return 1
        fi
        sleep_ms=${remaining_ms}
        if ((sleep_ms > 1000)); then
            sleep_ms=1000
        fi
        printf 'Final HIL health check: state=%s; retrying within %s ms deadline.\n' "${state:-<missing>}" "${timeout_ms}" >>"${HIL_LOG_FILE}"
        printf -v sleep_seconds '%d.%03d' "$((sleep_ms / 1000))" "$((sleep_ms % 1000))"
        sleep "${sleep_seconds}"
    done
}

hil_print_status_json() {
    python3 -c '
import json
import sys
state = json.load(sys.stdin)
components = state["components"]
print("HIL · {}".format(state["state"]))
target = state.get("pi_target", {})
print("Pi target:       {}{}".format(target.get("host", "iii.local"), " ({})".format(target["peer_ipv4"]) if target.get("peer_ipv4") else ""))
print("Host install:    {}".format(state.get("native_install", {}).get("root", "unknown")))
print("Install profile: {}".format(state.get("native_install", {}).get("install_profile", "unknown")))
print("Runtime profile: {}".format(state.get("native_install", {}).get("runtime_profile", "hil")))
print("Host:            {}".format(state.get("native_install", {}).get("host", "unknown")))
print("Pi runtime:      {}".format(components["pi_runtime"]))
print("Workstation:     {}".format(components["workstation"]))
print("Gazebo viewer:   {} ({})".format(state["viewer_state"], state["render_mode"]))
print("Ground control:  {} · {}".format(components["ground_control"], state["gc_url"]))
for detail in state.get("diagnostics", {}).get("pi_runtime", []):
    print("Pi detail:       {}".format(detail))
for detail in state.get("diagnostics", {}).get("workstation", []):
    print("Workstation detail: {}".format(detail))
for detail in state.get("diagnostics", {}).get("ground_control", []):
    print("Ground control detail: {}".format(detail))
print("Log:             {}".format(state["log_path"]))
'
}

hil_failure() {
    local action="$1"
    local stage="$2"
    local detail
    detail="$(python3 - "${HIL_LOG_FILE}" "${stage}" <<'PY'
import re
import sys
from pathlib import Path

try:
    lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
except OSError:
    raise SystemExit(0)
sections = {"coordinator": [], "ground-control": [], "rollback": [], "other": []}
section = "other"
for line in lines:
    match = re.fullmatch(r"--- (coordinator|ground-control|rollback) ---", line)
    if match:
        section = match.group(1)
    else:
        sections[section].append(line.strip())
stage = sys.argv[2]
if stage == "Pi/workstation lifecycle":
    candidates = sections["coordinator"] or lines
elif stage == "ground control":
    candidates = sections["ground-control"] or lines
else:
    candidates = sections["other"] or lines
diagnostic = re.compile(r"failed|error|unable|cannot|missing|not running|refus|timed out|denied|invalid|unavailable|occupied|conflict|mismatch|not ready", re.I)
for line in reversed(candidates):
    line = line.strip()
    if not line or line.startswith(("III_HIL_PROGRESS|", "III_HIL_STOP|")):
        continue
    if line.startswith("Coordinated HIL ") and " failed: " in line:
        line = line.split(" failed: ", 1)[1]
    if line.startswith("Command '['") and "returned non-zero exit status" in line:
        continue
    if diagnostic.search(line):
        print(line[:700])
        break
PY
)"
    printf '\nHIL %s failed at %s.\n' "${action}" "${stage}" >&2
    [[ -z "${detail}" ]] || printf 'Reason: %s\n' "${detail}" >&2
    printf 'Log: %s\n' "${HIL_LOG_FILE}" >&2
    printf 'Some HIL components may still be running; inspect before retrying:\n' >&2
    printf '  ./iii-dev hil status\n' >&2
}

hil_print_stop_components() {
    # Render the coordinator's per-owner stop records from this run's log.
    local marker component outcome detail label symbol
    [[ -f "${HIL_LOG_FILE:-}" ]] || return 0
    while IFS='|' read -r marker component outcome detail; do
        [[ "${marker}" == "III_HIL_STOP" ]] || continue
        case "${component}" in
            pi_runtime) label="Pi runtime" ;;
            workstation) label="Workstation simulation" ;;
            *) continue ;;
        esac
        case "${outcome}" in
            stopped|already_stopped) symbol='✓' ;;
            *) symbol='✗' ;;
        esac
        printf '%s %s: %s%s\n' "${symbol}" "${label}" "${outcome//_/ }" "${detail:+ (${detail})}"
    done < <(grep '^III_HIL_STOP|' "${HIL_LOG_FILE}" 2>/dev/null || true)
}

hil_export_ground_control_endpoints() {
    set -a
    source "${III_GC_ENV_FILE}"
    set +a
    resolve_gc_install_profile >/dev/null
    export III_HIL_GC_URL="http://127.0.0.1:${III_GC_FRONTEND_PORT:-5174}"
    export III_HIL_GC_PROXY_HEALTH_URL="http://127.0.0.1:${III_GC_PROXY_PORT:-8781}/health"
}

hil_ground_control_owned() {
    # Endpoint reachability cannot establish Compose ownership: a foreign
    # service may be bound to either port. Ask the GC helper about this exact
    # project before deciding whether rollback is authorized.
    "${GC_SCRIPT}" owned >/dev/null 2>&1
}

hil_open_operator_windows() {
    local operation_id="iii-dev-hil-qgc-$(date +%s%N)-${BASHPID}"
    local result=0 qgc_status attempt qgc_active=0 browser_result
    printf '[start] QGroundControl\n'
    if [[ -x "${QGC_CLI}" ]] &&
        env -u PYTHONPATH "${QGC_CLI}" qgc start --dry-run --operation-id "${operation_id}" --output=json >>"${HIL_LOG_FILE}" 2>&1 &&
        env -u PYTHONPATH "${QGC_CLI}" qgc start --operation-id "${operation_id}" --confirm --non-interactive --output=json >>"${HIL_LOG_FILE}" 2>&1; then
        for attempt in 1 2 3 4 5; do
            qgc_status="$(env -u PYTHONPATH "${QGC_CLI}" qgc status --output=json 2>>"${HIL_LOG_FILE}")" || qgc_status=""
            printf '%s\n' "${qgc_status}" >>"${HIL_LOG_FILE}"
            if python3 -c 'import json,sys; p=json.load(sys.stdin)["payload"]; sys.exit(0 if p["selection"]["selected"] and p["unit"]["ActiveState"] == "active" else 1)' <<<"${qgc_status}" 2>/dev/null; then
                qgc_active=1
                break
            fi
            sleep 0.5
        done
        if ((qgc_active)); then
            printf '[done] QGroundControl\n'
        else
            printf 'QGroundControl service did not become active after start.\n' >>"${HIL_LOG_FILE}"
            printf '[fail] QGroundControl (see %s)\n' "${HIL_LOG_FILE}" >&2
            result=1
        fi
    else
        printf '[fail] QGroundControl (see %s)\n' "${HIL_LOG_FILE}" >&2
        result=1
    fi
    printf '[start] Ground-control browser window\n'
    if browser_result="$(python3 "${BROWSER_WINDOW_SCRIPT}" "${III_HIL_GC_URL}" 2>>"${HIL_LOG_FILE}")"; then
        printf '%s\n' "${browser_result}" >>"${HIL_LOG_FILE}"
        case "${browser_result}" in
            opened) printf '[done] Ground-control browser window opened · %s\n' "${III_HIL_GC_URL}" ;;
            already-open) printf '[done] Ground-control browser window already open · %s\n' "${III_HIL_GC_URL}" ;;
            *) printf '[fail] Ground-control browser window returned an unknown result (see %s)\n' "${HIL_LOG_FILE}" >&2; result=1 ;;
        esac
    else
        printf '[fail] Ground-control browser window (see %s)\n' "${HIL_LOG_FILE}" >&2
        result=1
    fi
    return "${result}"
}

hil_apply_host_override() {
    local host="$1"
    [[ "${host}" =~ ^[[:alnum:]][[:alnum:].-]*$ ]] || {
        iii_dev_die "Invalid HIL host: ${host}"
        return 1
    }
    # --host selects the whole remote authority for this invocation, including
    # the CLI's SSH route, the Runtime API, DDS discovery and PX4 transport.
    export III_HIL_PI_ENDPOINT="${host}"
    unset III_HIL_PI_ADDRESS CYCLONEDDS_URI
    export III_SSH_HOST="${host}"
    export III_RUNTIME_HOST="${host}"
    export III_RUNTIME_API_HOST="${host}"
    export III_RUNTIME_API_URL="http://${host}:8765"
}

hil_pin_resolved_peer() {
    local peer="${III_HIL_RESOLVED_PI_ADDRESS:-}"
    [[ -n "${peer}" ]] || return 0
    [[ "${peer}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
        iii_dev_die "HIL setup returned an invalid Pi IPv4 address: ${peer}"
        return 1
    }
    export III_HIL_PI_ENDPOINT="${peer}"
    export III_HIL_PI_ADDRESS="${peer}"
    export III_SSH_HOST="${peer}"
    export III_RUNTIME_HOST="${peer}"
    export III_RUNTIME_API_HOST="${peer}"
    export III_RUNTIME_API_URL="http://${peer}:8765"
}

run_hil() {
    local action="${1:-}"
    local argument
    local host_override=""
    local -a hil_arguments=()
    local headless=0
    local verbose=0
    local json_output=0
    local status_json
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage hil
        return
    fi
    shift
    if [[ "${action}" == "start" || "${action}" == "restart" || "${action}" == "status" || "${action}" == "stop" ]]; then
        while (($#)); do
            case "$1" in
                --host)
                    (($# >= 2)) && [[ -n "$2" ]] || { iii_dev_die "--host requires a hostname or IPv4 address"; return 1; }
                    host_override="$2"
                    shift 2
                    ;;
                --host=*) host_override="${1#--host=}"; shift ;;
                *) hil_arguments+=("$1"); shift ;;
            esac
        done
        set -- "${hil_arguments[@]}"
        if [[ -n "${host_override}" ]]; then
            [[ "${host_override}" =~ ^[[:alnum:]][[:alnum:].-]*$ ]] || { iii_dev_die "Invalid HIL host: ${host_override}"; return 1; }
        fi
    fi
    if help_requested "$@"; then
        command_usage hil "${action}"
        return
    fi
    case "${action}" in
        start|restart)
            for argument in "$@"; do
                case "${argument}" in
                    --headless) headless=1 ;;
                    --verbose) verbose=1 ;;
                    *) iii_dev_die "Usage: ./iii-dev hil ${action} [--headless] [--verbose] [--host HOST]"; return ;;
                esac
            done
            lock_stack_mutation || return
            [[ -z "${host_override}" ]] || hil_apply_host_override "${host_override}"
            # shellcheck source=setup/setup_hil.bash
            source "${III_DEV_WORKSPACE_ROOT}/setup/setup_hil.bash"
                hil_pin_resolved_peer || return
                [[ -z "${III_HIL_RESOLVED_PI_ADDRESS:-}" ]] || host_override="${III_HIL_RESOLVED_PI_ADDRESS}"
                export III_GC_ENV_FILE="${III_HIL_GC_ENV_FILE:-${III_DEV_WORKSPACE_ROOT}/setup/ground-control.hil.env}"
                export III_GC_LOG_DIR="${III_HIL_GC_LOG_DIR:-${III_DEV_WORKSPACE_ROOT}/runtime_logs/ground-control-hil}"
                hil_export_ground_control_endpoints
                hil_prepare_log "${action}"
                printf 'HIL · %s (%s)\n\n' "${action}ing" "$([[ "${headless}" == "1" ]] && echo headless || echo rendered)"
                local -a coordinator_args=("${action}")
                local gc_preexisting=0 coordinator_pid="" gc_pid="" coordinator_pgid="" gc_pgid="" coordinator_start_ticks="" gc_start_ticks="" coordinator_result=1 gc_result=1 parent_pgid="" coordinator_gate=0 gc_gate=0 pending_child_pid="" pending_child_role=""
                local transaction_dir coordinator_output gc_output rollback_output coordinator_identity_file coordinator_release_file gc_identity_file gc_release_file
                local transaction_active=1 cleanup_done=0 rollback_done=0 transaction_logs_appended=0
                local status_probe_pid="" status_probe_pgid="" status_probe_start_ticks="" status_probe_gate=0
                local status_probe_output="" status_probe_output_pending=0 status_probe_identity_file="" status_probe_release_file=""
                ((headless)) && coordinator_args+=(--headless)
                [[ -z "${host_override}" ]] || coordinator_args+=(--host "${host_override}")
                hil_acquire_ground_control_lock || exit 1
                # Snapshot exact Compose ownership while holding the same lock
                # used by the GC helper. Endpoint health never grants rollback
                # authority, and another project mutation cannot race this
                # snapshot through the concurrent startup or rollback.
                if hil_ground_control_owned; then
                    gc_preexisting=1
                fi
                transaction_dir="$(mktemp -d "${HIL_LOG_DIR}/.hil-${action}.XXXXXX")"
                coordinator_output="${transaction_dir}/coordinator.log"
                gc_output="${transaction_dir}/ground-control.log"
                rollback_output="${transaction_dir}/rollback.log"
                coordinator_identity_file="${transaction_dir}/coordinator.identity"
                coordinator_release_file="${transaction_dir}/coordinator.release"
                gc_identity_file="${transaction_dir}/ground-control.identity"
                gc_release_file="${transaction_dir}/ground-control.release"
                : >"${coordinator_output}"
                : >"${gc_output}"
                : >"${rollback_output}"
                read -r parent_pgid _ < <(hil_child_identity "$$") || parent_pgid=""
                trap 'hil_exit_cleanup' EXIT
                trap 'hil_signal_handler INT' INT
                trap 'hil_signal_handler TERM' TERM
                trap 'hil_signal_handler HUP' HUP
                set +e
                coordinator_gate=1
                pending_child_role=coordinator
                hil_launch_group "${coordinator_output}" "${coordinator_identity_file}" "${coordinator_release_file}" env III_HIL_PROGRESS=1 python3 "${HIL_RESTART_SCRIPT}" "${coordinator_args[@]}"
                coordinator_pid="${pending_child_pid}"
                pending_child_pid=""
                pending_child_role=""
                trap 'hil_signal_handler INT' INT
                trap 'hil_signal_handler TERM' TERM
                trap 'hil_signal_handler HUP' HUP
                if hil_wait_child_identity "${coordinator_pid}" "${coordinator_identity_file}"; then
                    coordinator_pgid="${hil_captured_pgid}"
                    coordinator_start_ticks="${hil_captured_start_ticks}"
                    hil_release_child_gate "${coordinator_release_file}"
                    coordinator_gate=0
                    printf '[start] Pi/workstation lifecycle\n'
                else
                    set +e
                    hil_cleanup_transaction 1 "coordinator startup handshake" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                gc_gate=1
                pending_child_role=ground-control
                hil_launch_group "${gc_output}" "${gc_identity_file}" "${gc_release_file}" env III_GC_LOCK_HELD=1 III_GC_LOCK_PATH="${III_GC_LOCK_PATH}" "${GC_SCRIPT}" start
                gc_pid="${pending_child_pid}"
                pending_child_pid=""
                pending_child_role=""
                trap 'hil_signal_handler INT' INT
                trap 'hil_signal_handler TERM' TERM
                trap 'hil_signal_handler HUP' HUP
                if hil_wait_child_identity "${gc_pid}" "${gc_identity_file}"; then
                    gc_pgid="${hil_captured_pgid}"
                    gc_start_ticks="${hil_captured_start_ticks}"
                    hil_release_child_gate "${gc_release_file}"
                    gc_gate=0
                    printf '[start] Ground control\n'
                else
                    set +e
                    hil_cleanup_transaction 1 "ground control startup handshake" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                hil_show_coordinator_progress "${coordinator_pid}" "${coordinator_output}"
                wait "${coordinator_pid}"
                coordinator_result=$?
                hil_clear_child_identity coordinator
                if ((coordinator_result == 0)); then
                    printf '[done] Pi/workstation lifecycle\n'
                fi
                hil_show_child_heartbeat "${gc_pid}" "Ground control"
                wait "${gc_pid}"
                gc_result=$?
                hil_clear_child_identity ground-control
                if ((gc_result == 0)); then
                    printf '[done] Ground control\n'
                fi
                set -e
                trap - INT TERM HUP
                hil_append_transaction_logs
                if ((coordinator_result != 0)); then
                    set +e
                    hil_cleanup_transaction "${coordinator_result}" "Pi/workstation lifecycle" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                if ((gc_result != 0)); then
                    set +e
                    hil_cleanup_transaction "${gc_result}" "ground control" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                printf '✓ Pi runtime and workstation simulation ready\n'
                printf '✓ Ground control started\n'
                printf '[start] Ground-control runtime selection\n'
                if ! hil_run_logged "${verbose}" python3 "${HIL_GC_BIND_SCRIPT}" \
                    --proxy-url "${III_HIL_GC_PROXY_HEALTH_URL%/health}" \
                    --runtime-url "${III_RUNTIME_API_URL}"; then
                    set +e
                    hil_cleanup_transaction 1 "ground-control runtime selection" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                printf '[done] Ground-control runtime selection · %s\n' "${III_RUNTIME_API_URL}"
                printf '[start] Final HIL health check\n'
                trap 'hil_signal_handler INT' INT
                trap 'hil_signal_handler TERM' TERM
                trap 'hil_signal_handler HUP' HUP
                if hil_capture_status_json "${verbose}" status_json; then
                    printf '[done] Final HIL health check\n'
                else
                    final_health_result=$?
                    set +e
                    hil_cleanup_transaction "${final_health_result}" "final health check" 1
                    cleanup_result=$?
                    set -e
                    exit "${cleanup_result}"
                fi
                set +e
                hil_cleanup_transaction 0 "completed" 0
                cleanup_result=$?
                set -e
                printf '✓ Gazebo viewer and HIL health verified\n\n'
                printf '%s\n' "${status_json}" | hil_print_status_json
                if ((headless == 0)) && [[ "${III_DEV_OPEN_OPERATOR_WINDOWS:-1}" == "1" ]]; then
                    if ! hil_open_operator_windows; then
                        printf 'HIL core is ready, but an operator window failed to open. Log: %s\n' "${HIL_LOG_FILE}" >&2
                        return 1
                    fi
                fi
            ;;
        status)
            for argument in "$@"; do
                case "${argument}" in
                    --json) json_output=1 ;;
                    --verbose) verbose=1 ;;
                    *) iii_dev_die "Usage: ./iii-dev hil status [--json] [--verbose] [--host HOST]"; return ;;
                esac
            done
            (
                [[ -z "${host_override}" ]] || hil_apply_host_override "${host_override}"
                # shellcheck source=setup/setup_hil.bash
                source "${III_DEV_WORKSPACE_ROOT}/setup/setup_hil.bash"
                hil_pin_resolved_peer || return
                [[ -z "${III_HIL_RESOLVED_PI_ADDRESS:-}" ]] || host_override="${III_HIL_RESOLVED_PI_ADDRESS}"
                export III_GC_ENV_FILE="${III_HIL_GC_ENV_FILE:-${III_DEV_WORKSPACE_ROOT}/setup/ground-control.hil.env}"
                hil_export_ground_control_endpoints
                export III_HIL_LOG_PATH="${HIL_LOG_DIR}/latest.log"
                set +e
                local -a status_args=(status --json)
                [[ -z "${host_override}" ]] || status_args+=(--host "${host_override}")
                status_json="$(python3 "${HIL_RESTART_SCRIPT}" "${status_args[@]}")"
                local status_result=$?
                set -e
                if ((json_output)); then
                    printf '%s\n' "${status_json}"
                else
                    printf '%s\n' "${status_json}" | hil_print_status_json
                    if ((verbose)); then
                        printf '\nRaw status:\n%s\n' "${status_json}"
                    fi
                fi
                exit "${status_result}"
            )
            ;;
        logs)
            if (($# == 0)); then
                [[ -e "${HIL_LOG_DIR}/latest.log" ]] || { iii_dev_die "No HIL command log exists yet."; return; }
                tail -n 200 "${HIL_LOG_DIR}/latest.log"
            elif (($# == 1)) && [[ "$1" == "--follow" ]]; then
                [[ -e "${HIL_LOG_DIR}/latest.log" ]] || { iii_dev_die "No HIL command log exists yet."; return; }
                tail -n 200 --follow=name "${HIL_LOG_DIR}/latest.log"
            else
                iii_dev_die "Usage: ./iii-dev hil logs [--follow]"
            fi
            ;;
        stop)
            for argument in "$@"; do
                case "${argument}" in
                    --verbose) verbose=1 ;;
                    *) iii_dev_die "Usage: ./iii-dev hil stop [--verbose] [--host HOST]"; return ;;
                esac
            done
            lock_stack_mutation || return
            (
                [[ -z "${host_override}" ]] || hil_apply_host_override "${host_override}"
                # shellcheck source=setup/setup_hil.bash
                source "${III_DEV_WORKSPACE_ROOT}/setup/setup_hil.bash"
                hil_pin_resolved_peer || return
                [[ -z "${III_HIL_RESOLVED_PI_ADDRESS:-}" ]] || host_override="${III_HIL_RESOLVED_PI_ADDRESS}"
                export III_GC_ENV_FILE="${III_HIL_GC_ENV_FILE:-${III_DEV_WORKSPACE_ROOT}/setup/ground-control.hil.env}"
                export III_GC_LOG_DIR="${III_HIL_GC_LOG_DIR:-${III_DEV_WORKSPACE_ROOT}/runtime_logs/ground-control-hil}"
                hil_export_ground_control_endpoints
                hil_prepare_log stop
                printf 'HIL · stopping\n\n'
                local -a stop_args=(stop)
                local stop_failed=0
                [[ -z "${host_override}" ]] || stop_args+=(--host "${host_override}")
                # Stop is best effort across the three owners. A failed or
                # unreachable Pi must not leave workstation PX4/Gazebo (handled
                # by the coordinator) or ground control running. Each owner
                # still applies its own ownership checks before acting.
                if hil_run_logged "${verbose}" python3 "${HIL_RESTART_SCRIPT}" "${stop_args[@]}"; then
                    printf '✓ Pi runtime and workstation simulation stopped\n'
                else
                    stop_failed=1
                    hil_print_stop_components
                    hil_failure stop "Pi/workstation lifecycle"
                    printf '\n'
                fi
                if hil_run_logged "${verbose}" "${GC_SCRIPT}" stop; then
                    printf '✓ Ground control stopped\n\n'
                else
                    stop_failed=1
                    printf '✗ Ground control: stop failed\n'
                    hil_failure stop "ground control"
                    printf '\n'
                fi
                if ((stop_failed)); then
                    printf 'HIL stop incomplete; see the component results above.\nLog: %s\n' "${HIL_LOG_FILE}" >&2
                    exit 1
                fi
                printf 'HIL stopped\nLog: %s\n' "${HIL_LOG_FILE}"
            )
            ;;
        *)
            iii_dev_die "Unknown HIL action: ${action}"
            ;;
    esac
}

run_gui() {
    local action="${1:-}"
    shift

    case "${action}" in
        start|stop|restart|recover|status|logs)
            "${GC_SCRIPT}" "${action}" "$@"
            ;;
        *)
            iii_dev_die "Unknown internal ground-control action: ${action}"
            ;;
    esac
}

run_runtime_api() {
    local action="${1:-}"
    local operation_id
    shift

    case "${action}" in
        start|stop|restart)
            require_no_args "api ${action}" "$@" || return
            operation_id="iii-dev-api-${action}-$(date +%s)-${BASHPID}"
            run_native_cli api "${action}" --dry-run --operation-id "${operation_id}" --output=json || return
            run_native_cli api "${action}" --operation-id "${operation_id}" --confirm --non-interactive --output=json
            ;;
        status)
            require_no_args "api status" "$@" || return
            run_native_cli api status
            ;;
        logs)
            if (($# == 0)); then
                run_native_cli api logs
            elif (($# == 1)) && [[ "$1" == "--follow" ]]; then
                run_native_cli api logs --follow
            else
                iii_dev_die "Usage: iii --runtime-target sim api logs [--follow]"
            fi
            ;;
        *)
            iii_dev_die "Unknown runtime API action: ${action}"
            ;;
    esac
}

simulation_ready() {
    local status
    status="$(iii_dev_exec never env III_SIM_TOOLS_STATUS_DISCOVERY_TIMEOUT_SEC=8 "${SIM_SCRIPT}" --status)" || return
    if grep -Eq '^pane=0 .* dead=1 ' <<< "${status}"; then
        printf '%s\n' "${status}"
        return 2
    fi
    if grep -q '^tmux_session: running$' <<< "${status}" &&
        grep -Eq '^simulation_process_groups: [0-9]' <<< "${status}" &&
        grep -q '^gazebo_transport: available$' <<< "${status}"; then
        return 0
    fi
    printf '%s\n' "${status}"
    return 1
}

runtime_api_ready() {
    local status
    iii_dev_exec never curl --fail --silent --show-error --max-time 2 "${RUNTIME_API_HEALTH_URL}" >/dev/null || return
    status="$(iii_dev_exec never curl --fail --silent --show-error --max-time 2 "${RUNTIME_API_VEHICLE_STATUS_URL}")" || return
    python3 -c '
import json, sys
try:
    status = json.load(sys.stdin)
    latest = status.get("latest") or {}
    command = latest.get("command_transport") or {}
    ros = latest.get("ros_uxrce") or {}
    ready = (status.get("freshness") == "fresh"
             and command.get("connected") is True
             and command.get("command_available") is True
             and ros.get("available") is True)
    if not ready:
        reason = (command.get("degraded_reason") or status.get("degraded_reason")
                  or "waiting for fresh PX4 telemetry and command transport")
        print("Runtime API is not ready: " + str(reason))
    sys.exit(0 if ready else 1)
except (ValueError, AttributeError, TypeError):
    print("Runtime API returned invalid vehicle readiness data")
    sys.exit(1)
' <<<"${status}"
}

lock_stack_mutation() {
    local lock_dir="${III_DEV_WORKSPACE_ROOT}/runtime"
    mkdir -p "${lock_dir}"
    exec 9>"${lock_dir}/iii-dev.lock"
    flock -n 9 || iii_dev_die "Another iii-dev stack mutation is already running."
}

stack_start() {
    local headless=0
    local recreate=0
    local start_gui=1
    local argument
    local -a sim_args=()

    while (($# > 0)); do
        argument="$1"
        case "${argument}" in
            --headless)
                headless=1
                ;;
            --recreate-sim)
                recreate=1
                ;;
            --no-gui)
                start_gui=0
                ;;
            --sim-model)
                if (($# < 2)); then
                    iii_dev_die "stack start --sim-model needs a gz_<model> value."
                    return
                fi
                sim_args+=(--sim-model "$2")
                shift
                ;;
            *)
                iii_dev_die "Unknown stack start option: ${argument}"
                return
                ;;
        esac
        shift
    done

    lock_stack_mutation || return
    ((headless)) && sim_args+=(--headless)
    if ((recreate)); then
        run_sim restart "${sim_args[@]}"
    else
        run_sim start "${sim_args[@]}"
    fi
    iii_dev_wait_until \
        "PX4/Gazebo simulation" \
        "${III_DEV_SIM_READY_TIMEOUT_SEC:-300}" \
        2 \
        simulation_ready

    run_system_mutation boot
    run_system_mutation start
    run_runtime_api start
    iii_dev_wait_until \
        "III runtime API" \
        "${III_DEV_RUNTIME_API_READY_TIMEOUT_SEC:-30}" \
        1 \
        runtime_api_ready
    if ((start_gui)); then
        run_gui start
    fi

    section "Ready"
    printf 'Simulation tmux: ./iii-dev sim attach\n'
    printf 'III tmux:        iii --runtime-target sim system attach\n'
    if ((start_gui)); then
        printf 'Operator GUI:    http://127.0.0.1:%s\n' "${III_GC_FRONTEND_PORT:-5173}"
    fi
}

stack_status() {
    local result=0

    section "Devcontainer"
    iii_dev_container_status || result=1
    section "Simulation"
    run_sim status || result=1
    section "III system"
    run_system status || result=1
    section "III runtime API"
    run_runtime_api status || result=1
    section "Ground control"
    run_gui status || result=1
    return "${result}"
}

stack_attach() {
    local target="${1:-system}"
    (($# <= 1)) || { iii_dev_die "stack attach accepts at most one target."; return; }
    case "${target}" in
        system)
            run_system attach
            ;;
        sim)
            run_sim attach
            ;;
        *)
            iii_dev_die "Unknown stack attach target: ${target}; expected system or sim."
            ;;
    esac
}

stack_stop() {
    local result=0

    require_no_args "stack stop" "$@" || return
    lock_stack_mutation || return

    section "Ground control"
    run_gui stop || result=1
    section "III system"
    run_system_mutation shutdown || result=1
    section "III runtime API"
    run_runtime_api stop || result=1
    section "Simulation"
    run_sim stop || result=1
    return "${result}"
}

run_tmux() {
    local action="${1:-}"
    shift || true
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage tmux
        return
    fi
    if help_requested "$@"; then
        command_usage tmux "${action}"
        return
    fi
    case "${action}" in
        list)
            require_no_args "tmux list" "$@" || return
            iii_dev_exec never tmux list-sessions -F '#{session_name}\t#{session_windows}\t#{session_attached}'
            ;;
        attach)
            (($# == 1)) || { iii_dev_die "Usage: ./iii-dev tmux attach <session>"; return; }
            iii_dev_exec interactive tmux attach -t "=$1"
            ;;
        *)
            iii_dev_die "Unknown tmux action: ${action:-<missing>}"
            ;;
    esac
}

main() {
    local top_command="${1:-help}"
    shift || true

    case "${top_command}" in
        help|-h|--help)
            usage
            ;;
        container)
            local action="${1:-status}"
            shift || true
            if [[ "${action}" == "-h" || "${action}" == "--help" ]]; then
                command_usage container
                return
            fi
            if help_requested "$@"; then
                command_usage container "${action}"
                return
            fi
            case "${action}" in
                status)
                    require_no_args "container status" "$@" || return
                    iii_dev_container_status
                    ;;
                up)
                    require_no_args "container up" "$@" || return
                    iii_dev_container_up
                    ;;
                down)
                    require_no_args "container down" "$@" || return
                    iii_dev_container_down
                    ;;
                *)
                    iii_dev_die "Unknown container action: ${action}"
                    ;;
            esac
            ;;
        shell)
            if help_requested "$@"; then
                command_usage shell
                return
            fi
            require_no_args "shell" "$@" || return
            iii_dev_exec interactive bash -il
            ;;
        exec)
            if (($# == 1)) && help_requested "$@"; then
                command_usage exec
                return
            fi
            (($# > 0)) || { iii_dev_die "Usage: ./iii-dev exec <command> [args...]"; return; }
            iii_dev_exec never "$@"
            ;;
        sim)
            run_sim "$@"
            ;;
        hil)
            run_hil "$@"
            ;;
        system|api|gui|rosbag)
            removed_command_guidance "${top_command}" "$@"
            ;;
        tmux)
            run_tmux "$@"
            ;;
        stack)
            local action="${1:-status}"
            shift || true
            if [[ "${action}" == "-h" || "${action}" == "--help" ]]; then
                command_usage stack
                return
            fi
            if help_requested "$@"; then
                command_usage stack "${action}"
                return
            fi
            case "${action}" in
                start)
                    stack_start "$@"
                    ;;
                status)
                    require_no_args "stack status" "$@" || return
                    stack_status
                    ;;
                attach)
                    stack_attach "$@"
                    ;;
                stop)
                    stack_stop "$@"
                    ;;
                *)
                    iii_dev_die "Unknown stack action: ${action}"
                    ;;
            esac
            ;;
        *)
            iii_dev_error "Unknown command: ${top_command}"
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
