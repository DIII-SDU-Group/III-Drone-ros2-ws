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
HIL_SCRIPT="${III_DEV_CONTAINER_WORKSPACE}/tools/simulation/launch_hil_workstation.sh"
HIL_RESTART_SCRIPT="${III_DEV_WORKSPACE_ROOT}/scripts/workspace/coordinate_hil_restart.py"
GC_SCRIPT="${III_DEV_WORKSPACE_ROOT}/scripts/workspace/iii_ground_control.sh"
RUNTIME_API_SERVICE="${III_DEV_RUNTIME_API_SERVICE:-iii-runtime-api.service}"
RUNTIME_API_HEALTH_URL="${III_DEV_RUNTIME_API_HEALTH_URL:-http://127.0.0.1:8765/health}"
ROSBAG_SCRIPT="${III_DEV_CONTAINER_WORKSPACE}/scripts/workspace/iii_rosbag.sh"

usage() {
    cat <<'EOF'
Usage: ./iii-dev <command> [arguments]

Host workspace commands:
  container status|up       Inspect or start the workspace devcontainer
  shell                     Open a configured interactive container shell
  exec <command> [args...]  Run an arbitrary configured container command

  sim start [options]       Ensure rendered PX4/Gazebo/QGC simulation is running
  sim restart [options]     Recreate the simulation without attaching
  sim attach                Attach to the simulation tmux session
  sim status|stop           Inspect or stop the simulation

  hil start|status|stop     Operate workstation-owned split-host HIL processes
  hil restart              Safely restart Pi runtime and workstation HIL together

  system <iii arguments>    Forward arguments to in-container `iii system`
  api start|stop|restart|status|logs [--follow]
                             Operate the devcontainer runtime API service
  tmux list                 List container tmux sessions
  tmux attach <session>     Attach to an exact container tmux session

  gui start|stop|restart|status|logs [args...]
                             Operate the host-side ground-control stack

  rosbag status|list         Inspect recorder state and stored recordings
  rosbag start [options]     Start a manual recording
  rosbag stop [options]      Stop the active recording cleanly
  rosbag delete <id>         Delete one inactive stored recording
  rosbag clear --force       Delete all inactive stored recordings

  stack start [options]     Start simulation, III runtime, and GUI
  stack status              Show aggregate container/sim/system/GUI status
  stack attach [system|sim] Attach to an operator tmux view
  stack stop                Stop GUI, III runtime, and simulation

Stack start options:
  --headless                Do not start Gazebo GUI or QGroundControl
  --recreate-sim            Recreate SITL and clear its persistent parameters
  --no-gui                  Do not start the ground-control web application

Environment overrides:
  III_DEV_SIM_READY_TIMEOUT_SEC  Simulation readiness timeout (default: 300)
  III_DEV_CONTAINER_USER         Container user (default: iii)
  III_DEV_CONTAINER_WORKSPACE    Container workspace (default: /home/iii/ws)
  III_DEV_DEVCONTAINER_BIN       Dev Container CLI executable
  III_DEV_RUNTIME_API_HEALTH_URL Runtime API health URL
EOF
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
            printf 'Usage: ./iii-dev container {status|up}\n'
            ;;
        container:status|container:up)
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
            printf 'Usage: ./iii-dev sim %s [--headless]\n' "${action}"
            ;;
        sim:attach|sim:status|sim:stop)
            printf 'Usage: ./iii-dev sim %s\n' "${action}"
            ;;
        hil:)
            printf 'Usage: ./iii-dev hil {start|status|stop|restart}\n'
            ;;
        hil:start|hil:status|hil:stop|hil:restart)
            printf 'Usage: ./iii-dev hil %s\n' "${action}"
            ;;
        system:)
            printf 'Usage: ./iii-dev system <iii system arguments>\n'
            printf 'Run ./iii-dev system <subcommand> --help for canonical III CLI help.\n'
            ;;
        api:)
            printf 'Usage: ./iii-dev api {start|stop|restart|status|logs}\n'
            ;;
        api:start|api:stop|api:restart|api:status)
            printf 'Usage: ./iii-dev api %s\n' "${action}"
            ;;
        api:logs)
            printf 'Usage: ./iii-dev api logs [--follow]\n'
            ;;
        gui:)
            printf 'Usage: ./iii-dev gui {start|stop|restart|recover|status|logs|open}\n'
            ;;
        gui:start|gui:stop|gui:restart|gui:recover|gui:status|gui:open)
            printf 'Usage: ./iii-dev gui %s\n' "${action}"
            ;;
        gui:logs)
            printf 'Usage: ./iii-dev gui logs [docker-compose-log-arguments]\n'
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
        rosbag:)
            printf 'Usage: ./iii-dev rosbag {status|list|start|stop|delete|clear}\n'
            ;;
        rosbag:status|rosbag:list)
            printf 'Usage: ./iii-dev rosbag %s\n' "${action}"
            ;;
        rosbag:start)
            printf 'Usage: ./iii-dev rosbag start [--id NAME] [--topic TOPIC ...] [--include-hidden]\n'
            ;;
        rosbag:stop)
            printf 'Usage: ./iii-dev rosbag stop [--id NAME] [--timeout SECONDS]\n'
            ;;
        rosbag:delete)
            printf 'Usage: ./iii-dev rosbag delete <recording-id>\n'
            ;;
        rosbag:clear)
            printf 'Usage: ./iii-dev rosbag clear --force\n'
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

system_needs_tty() {
    local argument
    for argument in "$@"; do
        case "${argument}" in
            attach|--attach|--follow|--watch)
                return 0
                ;;
        esac
    done
    return 1
}

run_system() {
    local tty_mode=never
    system_needs_tty "$@" && tty_mode=interactive
    if [[ "${1:-}" == "boot" ]]; then
        iii_dev_repair_generated_ownership
    fi
    iii_dev_exec "${tty_mode}" iii system "$@"
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
        start)
            for argument in "$@"; do
                [[ "${argument}" == "--headless" ]] || {
                    iii_dev_die "sim start accepts only --headless."
                    return
                }
            done
            iii_dev_exec never "${SIM_SCRIPT}" --no-attach "$@"
            ;;
        restart)
            for argument in "$@"; do
                [[ "${argument}" == "--headless" ]] || {
                    iii_dev_die "sim restart accepts only --headless."
                    return
                }
            done
            iii_dev_exec never "${SIM_SCRIPT}" --recreate --no-attach "$@"
            ;;
        attach)
            require_no_args "sim attach" "$@" || return
            iii_dev_exec interactive "${SIM_SCRIPT}" --attach
            ;;
        status)
            require_no_args "sim status" "$@" || return
            iii_dev_exec never "${SIM_SCRIPT}" --status
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

run_hil() {
    local action="${1:-}"
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage hil
        return
    fi
    shift
    if help_requested "$@"; then
        command_usage hil "${action}"
        return
    fi
    case "${action}" in
        start|status|stop)
            require_no_args "hil ${action}" "$@" || return
            iii_dev_exec never "${HIL_SCRIPT}" "${action}"
            ;;
        restart)
            require_no_args "hil restart" "$@" || return
            lock_stack_mutation || return
            (
                # shellcheck source=setup/setup_hil.bash
                source "${III_DEV_WORKSPACE_ROOT}/setup/setup_hil.bash"
                exec python3 "${HIL_RESTART_SCRIPT}"
            )
            ;;
        *)
            iii_dev_die "Unknown HIL action: ${action}"
            ;;
    esac
}

run_gui() {
    local action="${1:-}"
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage gui
        return
    fi
    shift
    if help_requested "$@"; then
        command_usage gui "${action}"
        return
    fi

    case "${action}" in
        start|stop|restart|recover|status|logs)
            "${GC_SCRIPT}" "${action}" "$@"
            ;;
        open)
            require_no_args "gui open" "$@" || return
            command -v xdg-open >/dev/null 2>&1 || { iii_dev_die "xdg-open is unavailable."; return; }
            xdg-open "http://127.0.0.1:${III_GC_FRONTEND_PORT:-5173}" >/dev/null 2>&1
            ;;
        *)
            iii_dev_die "Unknown GUI action: ${action}"
            ;;
    esac
}

run_runtime_api() {
    local action="${1:-}"
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage api
        return
    fi
    shift
    if help_requested "$@"; then
        command_usage api "${action}"
        return
    fi

    case "${action}" in
        start|stop|restart)
            require_no_args "api ${action}" "$@" || return
            iii_dev_exec never sudo -n systemctl "${action}" "${RUNTIME_API_SERVICE}"
            ;;
        status)
            require_no_args "api status" "$@" || return
            iii_dev_exec never systemctl is-active "${RUNTIME_API_SERVICE}"
            ;;
        logs)
            if (($# == 0)); then
                iii_dev_exec never sudo -n journalctl -u "${RUNTIME_API_SERVICE}" --no-pager -n 200
            elif (($# == 1)) && [[ "$1" == "--follow" ]]; then
                iii_dev_exec interactive sudo -n journalctl -u "${RUNTIME_API_SERVICE}" --follow
            else
                iii_dev_die "Usage: ./iii-dev api logs [--follow]"
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
    iii_dev_exec never curl --fail --silent --show-error --max-time 2 "${RUNTIME_API_HEALTH_URL}" >/dev/null
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

    for argument in "$@"; do
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
            *)
                iii_dev_die "Unknown stack start option: ${argument}"
                return
                ;;
        esac
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
    printf 'III tmux:        ./iii-dev system attach\n'
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

run_rosbag() {
    local action="${1:-}"
    if [[ -z "${action}" || "${action}" == "-h" || "${action}" == "--help" ]]; then
        command_usage rosbag
        return
    fi
    shift
    if help_requested "$@"; then
        command_usage rosbag "${action}"
        return
    fi
    case "${action}" in
        status|list|start|stop|delete|clear)
            iii_dev_exec never "${ROSBAG_SCRIPT}" "${action}" "$@"
            ;;
        *)
            iii_dev_die "Unknown rosbag action: ${action}"
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
        system)
            if (($# == 0)); then
                command_usage system
                return
            fi
            if (($# == 1)) && help_requested "$@"; then
                command_usage system
                return
            fi
            run_system "$@"
            ;;
        api)
            run_runtime_api "$@"
            ;;
        gui)
            run_gui "$@"
            ;;
        tmux)
            run_tmux "$@"
            ;;
        rosbag)
            run_rosbag "$@"
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
