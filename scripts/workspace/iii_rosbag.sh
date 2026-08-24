#!/usr/bin/env bash

set -euo pipefail

STATUS_SERVICE="${III_DEV_ROSBAG_STATUS_SERVICE:-/mission/rosbag_recorder/recording_status}"
START_SERVICE="${III_DEV_ROSBAG_START_SERVICE:-/mission/rosbag_recorder/start_recording}"
STOP_SERVICE="${III_DEV_ROSBAG_STOP_SERVICE:-/mission/rosbag_recorder/stop_recording}"
ARTIFACT_ROOT="${III_DEV_ROSBAG_ROOT:-/tmp/iii_drone/rosbags}"
SERVICE_TIMEOUT_SEC="${III_DEV_ROSBAG_SERVICE_TIMEOUT_SEC:-10}"

usage() {
    cat <<'EOF'
Usage: iii_rosbag.sh {status|list|start|stop|delete|clear} [arguments]

  status                    Show active recorder state, owner, size, and free space
  list                      List stored recordings and their sizes
  start [options]           Start recording all topics (default) or selected topics
    --id NAME               Recording name prefix
    --topic TOPIC           Record one topic; repeat for multiple topics
    --include-hidden        Include hidden topics
  stop [options]            Stop the active recording cleanly
    --id NAME               Require the active recording to have this ID
    --timeout SECONDS       Graceful-stop timeout (default: 10)
  delete RECORDING_ID       Delete one recording; rejected while recording
  clear --force             Delete every recording; rejected while recording
EOF
}

die() {
    printf 'iii-dev rosbag: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is unavailable in the devcontainer"
}

service_call() {
    local service="$1"
    local type="$2"
    local request="$3"
    timeout "${SERVICE_TIMEOUT_SEC}" ros2 service call "${service}" "${type}" "${request}" || {
        local result=$?
        if ((result == 124)); then
            die "timed out waiting for ROS service ${service}"
        fi
        return "${result}"
    }
}

status_output() {
    service_call "${STATUS_SERVICE}" iii_drone_interfaces/srv/GetRosbagRecordingStatus '{}'
}

ensure_not_recording() {
    local status
    status="$(status_output)" || die "could not query recorder status"
    if grep -Eq 'recording([=:][[:space:]]*)[Tt]rue' <<< "${status}"; then
        printf '%s\n' "${status}" >&2
        die "a recording is active; stop it before deleting recordings"
    fi
}

validate_recording_id() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
        die "recording ID must contain only letters, digits, dot, underscore, and hyphen"
}

yaml_quote() {
    local value="${1//\'/\'\'}"
    printf "'%s'" "${value}"
}

run_start() {
    local recording_id=""
    local include_hidden=false
    local -a topics=()
    while (($#)); do
        case "$1" in
            --id)
                (($# >= 2)) || die "--id requires a value"
                recording_id="$2"
                shift 2
                ;;
            --topic)
                (($# >= 2)) || die "--topic requires a value"
                [[ "$2" == /* ]] || die "topic must be an absolute ROS name: $2"
                topics+=("$2")
                shift 2
                ;;
            --include-hidden)
                include_hidden=true
                shift
                ;;
            *)
                die "unknown start option: $1"
                ;;
        esac
    done
    [[ -z "${recording_id}" ]] || validate_recording_id "${recording_id}"

    local all_topics=true
    local topics_yaml="[]"
    if ((${#topics[@]})); then
        all_topics=false
        topics_yaml="["
        local separator=""
        local topic
        for topic in "${topics[@]}"; do
            topics_yaml+="${separator}$(yaml_quote "${topic}")"
            separator=", "
        done
        topics_yaml+="]"
    fi

    service_call "${START_SERVICE}" iii_drone_interfaces/srv/StartRosbagRecording \
        "{recording_id: $(yaml_quote "${recording_id}"), output_dir: '', all_topics: ${all_topics}, topics: ${topics_yaml}, include_hidden_topics: ${include_hidden}, owner: 'manual'}"
}

run_stop() {
    local recording_id=""
    local timeout="10"
    while (($#)); do
        case "$1" in
            --id)
                (($# >= 2)) || die "--id requires a value"
                recording_id="$2"
                shift 2
                ;;
            --timeout)
                (($# >= 2)) || die "--timeout requires a value"
                timeout="$2"
                shift 2
                ;;
            *)
                die "unknown stop option: $1"
                ;;
        esac
    done
    [[ -z "${recording_id}" ]] || validate_recording_id "${recording_id}"
    [[ "${timeout}" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "timeout must be a non-negative number"
    service_call "${STOP_SERVICE}" iii_drone_interfaces/srv/StopRosbagRecording \
        "{recording_id: $(yaml_quote "${recording_id}"), timeout_sec: ${timeout}}"
}

run_list() {
    mkdir -p "${ARTIFACT_ROOT}"
    printf 'Rosbag root: %s\n' "${ARTIFACT_ROOT}"
    local found=0
    local path
    while IFS= read -r -d '' path; do
        found=1
        du -sh -- "${path}"
    done < <(find "${ARTIFACT_ROOT}" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
    ((found)) || printf 'No recordings.\n'
}

run_delete() {
    (($# == 1)) || die "Usage: ./iii-dev rosbag delete <recording-id>"
    validate_recording_id "$1"
    ensure_not_recording
    local target="${ARTIFACT_ROOT}/$1"
    [[ -d "${target}" ]] || die "recording not found: $1"
    rm -rf -- "${target}"
    printf 'Deleted %s\n' "${target}"
}

run_clear() {
    (($# == 1)) && [[ "$1" == "--force" ]] || die "clear requires --force"
    ensure_not_recording
    mkdir -p "${ARTIFACT_ROOT}"
    find "${ARTIFACT_ROOT}" -mindepth 1 -maxdepth 1 -type d -exec rm -rf -- {} +
    printf 'Cleared recordings from %s\n' "${ARTIFACT_ROOT}"
}

main() {
    local action="${1:--h}"
    shift || true
    case "${action}" in
        -h|--help)
            usage
            ;;
        status)
            (($# == 0)) || die "status accepts no arguments"
            require_command ros2
            status_output
            ;;
        list)
            (($# == 0)) || die "list accepts no arguments"
            run_list
            ;;
        start)
            require_command ros2
            run_start "$@"
            ;;
        stop)
            require_command ros2
            run_stop "$@"
            ;;
        delete)
            require_command ros2
            run_delete "$@"
            ;;
        clear)
            require_command ros2
            run_clear "$@"
            ;;
        *)
            die "unknown action: ${action}"
            ;;
    esac
}

main "$@"
