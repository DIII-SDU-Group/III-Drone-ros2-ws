#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-status}"
PREACTION_HOST=""
if [[ "${ACTION}" == "--host" ]]; then
    (($# >= 2)) || { echo "--host requires a hostname or IPv4 address." >&2; exit 2; }
    PREACTION_HOST="$2"
    shift 2
    ACTION="${1:-status}"
    [[ "$#" == "0" ]] || shift
elif [[ "${ACTION}" == --host=* ]]; then
    PREACTION_HOST="${ACTION#--host=}"
    [[ -n "${PREACTION_HOST}" ]] || { echo "--host requires a hostname or IPv4 address." >&2; exit 2; }
    shift
    ACTION="${1:-status}"
    [[ "$#" == "0" ]] || shift
else
    shift || true
fi
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
HIL_PEER_ADDRESS_FILE="${III_HIL_PEER_ADDRESS_FILE:-${HIL_RUNTIME_DIR}/pi-address-${PX4_INSTANCE}}"
HIL_LIFECYCLE_LOCK="${III_HIL_LIFECYCLE_LOCK:-${HIL_RUNTIME_DIR}/lifecycle.lock}"
HIL_OWNER_RECORD="${III_HIL_OWNER_RECORD:-${WORKSPACE_ROOT}/runtime/.iii-hil-owner-${PX4_INSTANCE}.env}"
PROC_ROOT="${III_HIL_PROC_ROOT:-/proc}"
PI_ENDPOINT="${III_HIL_PI_ENDPOINT:-iii.local}"
PI_ADDRESS="${III_HIL_PI_ADDRESS:-}"
PI_USER="${III_HIL_PI_USER:-iii}"
WORKSTATION_ADDRESS="${III_HIL_WORKSTATION_ADDRESS:-}"
WORKSTATION_INTERFACE="${III_HIL_WORKSTATION_INTERFACE:-}"
# The Pi owns both HIL agents: physical PX4 stays on UDP 8889 and workstation
# SITL uses the independent Pi-local UDP 8890 agent.  Keeping the DDS agents
# on the Pi keeps the entire ROS graph local while preventing reconnects from
# either PX4 client from replacing the other's XRCE session.
# PX4 stores UXRCE_DDS_AG_IP as the IPv4 bytes interpreted in network order.
# Leave the
# override available for unusual routed setups, but derive the default from
# the resolved Pi address so the byte order cannot silently drift.
PX4_AGENT_ADDRESS_U32_OVERRIDE="${III_HIL_PX4_AGENT_ADDRESS_U32:-}"
XRCE_PORT="${III_HIL_XRCE_PORT:-8890}"
# The physical PX4 HIL baseline uses the default XRCE client key (1).  SITL
# uses a distinct agent, but retains a distinct identity for diagnosability.
PX4_DDS_CLIENT_KEY="${III_HIL_PX4_DDS_CLIENT_KEY:-2}"
# Physical PX4 sends to Pi UDP 14542 even while the HIL ROS graph is stopped.
# SITL must have its own MAVSDK listener, just as it has its own XRCE agent.
MAVLINK_REMOTE_PORT="${III_HIL_MAVLINK_REMOTE_PORT:-14544}"
MAVLINK_LOCAL_PORT="${III_HIL_MAVLINK_LOCAL_PORT:-14582}"
MAVLINK_AUDIT_REMOTE_PORT="${III_HIL_MAVLINK_AUDIT_REMOTE_PORT:-14543}"
MAVLINK_AUDIT_LOCAL_PORT="${III_HIL_MAVLINK_AUDIT_LOCAL_PORT:-14581}"
MAVLINK_PARAMETER_REMOTE_PORT="${III_HIL_MAVLINK_PARAMETER_REMOTE_PORT:-14551}"
MAVLINK_PARAMETER_LOCAL_PORT="${III_HIL_MAVLINK_PARAMETER_LOCAL_PORT:-14583}"
MAVLINK_QGC_REMOTE_PORT="${III_HIL_MAVLINK_QGC_REMOTE_PORT:-14550}"
MAVLINK_QGC_LOCAL_PORT="${III_HIL_MAVLINK_QGC_LOCAL_PORT:-14584}"
ROS_DOMAIN_ID="${III_HIL_ROS_DOMAIN_ID:-42}"
# Resolve on the host checkout, then carry exactly this identity into its
# devcontainer. Different checkouts must never discover each other's worlds.
GZ_OWNER_HELPER="${SCRIPT_WORKSPACE_ROOT}/scripts/workspace/hil_gazebo_owner.py"
GZ_PARTITION="${III_HIL_GZ_PARTITION:-$(python3 "${GZ_OWNER_HELPER}" partition --workspace "${SCRIPT_WORKSPACE_ROOT}" --instance "${PX4_INSTANCE}")}"
export III_HIL_GZ_PARTITION="${GZ_PARTITION}"
GZ_OWNER_RECORD="${WORKSPACE_ROOT}/runtime/.iii-hil-gazebo-owner-${PX4_INSTANCE}.json"
# The direct workstation-to-Pi link is 100 Mb/s in the portable HIL rig.
# One 640x480 RGB frame is about 0.9 MB, so leave ample headroom for PX4 DDS
# traffic and the other mission sensors.  This remains overrideable for a
# faster lab link without changing the launcher.
CAMERA_RATE_HZ="${III_HIL_CAMERA_RATE_HZ:-1.0}"
HIL_RENDERED="${III_HIL_RENDERED:-1}"
# Bounded waits (seconds) for the PX4 shell, one PX4 shell command, the
# deterministic MAVLink endpoint set, and one adapter readiness probe.
PX4_SHELL_TIMEOUT_SEC="${III_HIL_PX4_SHELL_TIMEOUT_SEC:-90}"
PX4_COMMAND_TIMEOUT_SEC="${III_HIL_PX4_COMMAND_TIMEOUT_SEC:-20}"
MAVLINK_START_TIMEOUT_SEC="${III_HIL_MAVLINK_START_TIMEOUT_SEC:-20}"
ADAPTER_PROBE_TIMEOUT_SEC="${III_HIL_ADAPTER_PROBE_TIMEOUT_SEC:-20}"
# Each probe is a fresh ROS 2 CLI participant with the daemon disabled, so it
# pays full DDS discovery (measured ~3.2 s for `ros2 lifecycle get` on an idle
# HIL graph; more while five probes discover concurrently and PX4/Gazebo start).
# Per-probe limits sized below that falsely reported a healthy stack as not
# ready. The probe set as a whole stays bounded by ADAPTER_PROBE_TIMEOUT_SEC.
ADAPTER_SINGLE_PROBE_TIMEOUT_SEC="${III_HIL_ADAPTER_SINGLE_PROBE_TIMEOUT_SEC:-12}"
usage() {
    cat <<EOF
Usage: $(basename "$0") {start|status|stop|battery-check} [--host <hostname-or-IPv4>] [--headless|--rendered]

Runs only workstation-owned HIL processes. The aircraft runtime remains owned by
the Raspberry Pi. Standard link: workstation ${WORKSTATION_ADDRESS}, Pi ${PI_ADDRESS:-${PI_ENDPOINT}}.
EOF
}

valid_host_target() {
    python3 - "$1" <<'PY'
import ipaddress
import re
import sys

value = sys.argv[1]
if not value or len(value) > 253 or value.startswith("-"):
    raise SystemExit(1)
try:
    ipaddress.IPv4Address(value)
except ipaddress.AddressValueError:
    if re.fullmatch(r"[0-9.]+", value):
        raise SystemExit(1)
    labels = value[:-1].split(".") if value.endswith(".") else value.split(".")
    if not labels or any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise SystemExit(1)
PY
}

explicit_host=0
remaining_arguments=()
if [[ -n "${PREACTION_HOST}" ]]; then
    PI_ENDPOINT="${PREACTION_HOST}"
    PI_ADDRESS=""
    explicit_host=1
fi
while (($#)); do
    case "$1" in
        --host)
            (($# >= 2)) || { usage >&2; exit 2; }
            PI_ENDPOINT="$2"
            PI_ADDRESS=""
            explicit_host=1
            shift 2
            ;;
        --host=*)
            PI_ENDPOINT="${1#--host=}"
            [[ -n "${PI_ENDPOINT}" ]] || { usage >&2; exit 2; }
            PI_ADDRESS=""
            explicit_host=1
            shift
            ;;
        *)
            remaining_arguments+=("$1")
            shift
            ;;
    esac
done
set -- "${remaining_arguments[@]}"
if [[ "${explicit_host}" == "1" ]]; then
    valid_host_target "${PI_ENDPOINT}" || {
        echo "Invalid HIL Pi host '${PI_ENDPOINT}'. Use a hostname or IPv4 address." >&2
        exit 2
    }
    if [[ "${PI_ENDPOINT}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        PI_ADDRESS="${PI_ENDPOINT}"
    fi
    export III_HIL_PI_ENDPOINT="${PI_ENDPOINT}"
    export III_HIL_PI_ADDRESS="${PI_ADDRESS}"
    unset III_HIL_HOST_PI_ADDRESS
fi

if [[ "${ACTION}" == "-h" || "${ACTION}" == "--help" ]]; then
    usage
    exit 0
fi
case "${ACTION}" in
    start)
        for argument in "$@"; do
            case "${argument}" in
                --headless) HIL_RENDERED=0 ;;
                --rendered) HIL_RENDERED=1 ;;
                *) usage >&2; exit 2 ;;
            esac
        done
        ;;
    status|stop|battery-check)
        (($# == 0)) || { usage >&2; exit 2; }
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
[[ "${HIL_RENDERED}" == "0" || "${HIL_RENDERED}" == "1" ]] || {
    echo "III_HIL_RENDERED must be 0 or 1." >&2
    exit 2
}
export III_HIL_RENDERED="${HIL_RENDERED}"

# The operator-facing script runs on the workstation and then re-executes the
# lifecycle inside the devcontainer. The container has its own PID/network
# namespace, so the outer wrapper must reject a host-side orphan before it
# hands control to the inner launcher. The shared owner record is advisory;
# live /proc and UDP-peer evidence remains authoritative.
host_resolve_pi_address() {
    if [[ "${explicit_host:-0}" != "1" && ( "${ACTION}" == "status" || "${ACTION}" == "stop" ) ]]; then
        local owner_peer session_peer
        owner_peer="$(owner_record_value pi_address 2>/dev/null || true)"
        session_peer="$(session_pi_address 2>/dev/null || true)"
        if [[ -n "${owner_peer}" && -n "${session_peer}" && "${owner_peer}" != "${session_peer}" ]]; then
            echo "Recorded HIL owner peer ${owner_peer} conflicts with the pinned session peer ${session_peer}; refusing ${ACTION}." >&2
            return 1
        fi
        if [[ -n "${owner_peer}" ]]; then
            printf '%s\n' "${owner_peer}"
            return 0
        fi
        if [[ -n "${session_peer}" ]]; then
            printf '%s\n' "${session_peer}"
            return 0
        fi
    fi
    if [[ -n "${PI_ADDRESS}" ]]; then
        printf '%s\n' "${PI_ADDRESS}"
        return 0
    fi
    if [[ "${explicit_host:-0}" != "1" && -n "${III_HIL_HOST_PI_ADDRESS:-}" ]]; then
        printf '%s\n' "${III_HIL_HOST_PI_ADDRESS}"
        return 0
    fi
    valid_host_target "${PI_ENDPOINT}" || {
        echo "Invalid HIL Pi host '${PI_ENDPOINT}'. Use a hostname or IPv4 address." >&2
        return 1
    }
    if [[ "${explicit_host:-0}" == "1" && ( "${ACTION}" == "status" || "${ACTION}" == "stop" ) ]]; then
        local owner_peer session_peer pinned_peer candidate
        owner_peer="$(owner_record_value pi_address 2>/dev/null || true)"
        session_peer="$(session_pi_address 2>/dev/null || true)"
        if [[ -n "${owner_peer}" && -n "${session_peer}" && "${owner_peer}" != "${session_peer}" ]]; then
            echo "Recorded HIL owner peer ${owner_peer} conflicts with the pinned session peer ${session_peer}; refusing ${ACTION}." >&2
            return 1
        fi
        pinned_peer="${owner_peer:-${session_peer}}"
        if [[ -n "${pinned_peer}" ]]; then
            while IFS= read -r candidate; do
                if [[ "${candidate}" == "${pinned_peer}" ]]; then
                    printf '%s\n' "${pinned_peer}"
                    return 0
                fi
            done < <(getent ahostsv4 "${PI_ENDPOINT}" | awk 'NF {print $1}')
            echo "Explicit HIL Pi host ${PI_ENDPOINT} does not resolve to the recorded owner peer ${pinned_peer}; refusing ${ACTION}." >&2
            return 1
        fi
    fi
    if [[ "${ACTION}" == "start" || "${ACTION}" == "status" ]]; then
        resolve_reachable_pi_address "${PI_ENDPOINT}"
        return $?
    fi
    getent ahostsv4 "${PI_ENDPOINT}" | awk 'NR == 1 {print $1; exit}'
}

host_pi_ssh_reachable() {
    python3 - "$1" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], 22), timeout=0.75):
        pass
except OSError:
    raise SystemExit(1)
PY
}

valid_ipv4() {
    local address="$1"
    local octet
    local -a octets
    [[ "${address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r -a octets <<<"${address}"
    for octet in "${octets[@]}"; do
        ((10#${octet} <= 255)) || return 1
    done
}

resolve_reachable_pi_address() {
    local endpoint="$1" candidate
    local -a candidates=()
    while IFS= read -r candidate; do
        [[ -n "${candidate}" ]] || continue
        valid_ipv4 "${candidate}" || continue
        if [[ ! " ${candidates[*]} " == *" ${candidate} "* ]]; then
            candidates+=("${candidate}")
        fi
    done < <(getent ahostsv4 "${endpoint}" | awk 'NF {print $1}')

    for candidate in "${candidates[@]}"; do
        if select_workstation_route "${candidate}" >/dev/null 2>&1 && host_pi_ssh_reachable "${candidate}"; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    echo "No reachable IPv4 address for HIL Pi host ${endpoint} (checked SSH TCP port 22); refusing start." >&2
    return 1
}

select_workstation_route() {
    local pi_address="$1" route source interface
    local explicit_source="${III_HIL_WORKSTATION_ADDRESS:-}"
    local explicit_interface="${III_HIL_WORKSTATION_INTERFACE:-}"
    route="$(ip -4 route get "${pi_address}" 2>/dev/null | head -n1 || true)"
    source="$(awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' <<<"${route}")"
    interface="$(awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}' <<<"${route}")"
    if [[ -z "${source}" ]]; then
        source="$(python3 - "${pi_address}" <<'PY'
import socket
import sys

route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    route.connect((sys.argv[1], 9))
    print(route.getsockname()[0])
finally:
    route.close()
PY
)" || return 1
    fi
    valid_ipv4 "${source}" || {
        echo "Unable to determine a workstation IPv4 route to Pi ${pi_address}." >&2
        return 1
    }
    if [[ -n "${explicit_source}" && "${explicit_source}" != "${source}" ]]; then
        echo "III_HIL_WORKSTATION_ADDRESS ${explicit_source} does not match route to Pi ${pi_address} (source ${source}${interface:+, interface ${interface}})." >&2
        return 1
    fi
    if [[ -n "${explicit_interface}" && -n "${interface}" && "${explicit_interface}" != "${interface}" ]]; then
        echo "III_HIL_WORKSTATION_INTERFACE ${explicit_interface} does not match route to Pi ${pi_address} (interface ${interface})." >&2
        return 1
    fi
    WORKSTATION_ADDRESS="${source}"
    WORKSTATION_INTERFACE="${interface}"
    export III_HIL_WORKSTATION_ADDRESS="${source}"
    export III_HIL_WORKSTATION_INTERFACE="${interface}"
}

host_endpoint_pids() {
    local line output peer="${PI_ADDRESS}:${XRCE_PORT}"
    command -v ss >/dev/null 2>&1 || return 1
    if ! output="$(ss -4 -H -u -a -n -p "dst ${peer}" 2>/dev/null)"; then
        return 1
    fi
    while IFS= read -r line; do
        while [[ "${line}" =~ pid=([0-9]+) ]]; do
            printf '%s\n' "${BASH_REMATCH[1]}"
            line="${line#*pid=${BASH_REMATCH[1]}}"
        done
    done <<<"${output}"
    return 0
}

host_px4_records() {
    local endpoint_pids proc pid comm cmdline exe executable_state start_ticks endpoint=0 state recorded_executable
    local -a proc_roots=()
    endpoint_pids="${1-}"
    if [[ "$#" == "0" ]]; then
        endpoint_pids="$(host_endpoint_pids)" || return 1
    fi
    recorded_executable="$(owner_record_value executable 2>/dev/null || true)"
    if [[ "${PROC_ROOT}" == "/proc" ]] && command -v pgrep >/dev/null 2>&1; then
        while IFS= read -r pid; do
            [[ -n "${pid}" ]] && proc_roots+=("${PROC_ROOT}/${pid}")
        done < <(pgrep -x px4 2>/dev/null || true)
    else
        proc_roots=("${PROC_ROOT}"/[0-9]*)
    fi
    for proc in "${proc_roots[@]}"; do
        pid="${proc##*/}"
        [[ -r "${proc}/comm" && -r "${proc}/cmdline" ]] || continue
        state="$(awk '{print $3}' "${proc}/stat" 2>/dev/null || true)"
        [[ "${state}" != "Z" ]] || continue
        comm="$(<"${proc}/comm")"
        [[ "${comm}" == "px4" ]] || continue
        exe="$(readlink "${proc}/exe" 2>/dev/null || true)"
        case "${exe}" in
            "${PX4_BUILD_DIR}/bin/px4") executable_state=present ;;
            "${PX4_BUILD_DIR}/bin/px4 (deleted)") executable_state=deleted ;;
            *)
                if [[ -n "${recorded_executable}" && "${exe}" == "${recorded_executable}" ]]; then
                    executable_state=present
                elif [[ -n "${recorded_executable}" && "${exe}" == "${recorded_executable} (deleted)" ]]; then
                    executable_state=deleted
                else
                    continue
                fi
                ;;
        esac
        cmdline="$(tr '\0' ' ' <"${proc}/cmdline")"
        [[ "${cmdline}" == *"/px4-rcS-${PX4_INSTANCE} "* ]] || continue
        [[ "${cmdline}" == *" -i ${PX4_INSTANCE} "* ]] || continue
        endpoint=0
        if grep -Fxq "${pid}" <<<"${endpoint_pids}"; then
            endpoint=1
        fi
        start_ticks="$(awk '{print $22}' "${proc}/stat" 2>/dev/null)"
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "${pid}" "${start_ticks}" "${endpoint}" "${executable_state}" "${cmdline}"
    done
    return 0
}

owner_record_value() {
    local key="$1"
    [[ -f "${HIL_OWNER_RECORD}" && ! -L "${HIL_OWNER_RECORD}" ]] || return 1
    sed -n "s/^${key}=//p" "${HIL_OWNER_RECORD}" | head -n1
}

owner_record_static_identity_matches() {
    [[ "$(owner_record_value session 2>/dev/null || true)" == "${SIM_SESSION}" ]] &&
        [[ "$(owner_record_value pi_address 2>/dev/null || true)" == "${PI_ADDRESS}" ]] &&
        [[ "$(owner_record_value xrce_port 2>/dev/null || true)" == "${XRCE_PORT}" ]] &&
        [[ "$(owner_record_value gz_partition 2>/dev/null || true)" == "${GZ_PARTITION}" ]]
}

workspace_container_id() {
    local containers
    containers="$(docker ps \
        --filter "label=devcontainer.local_folder=${SCRIPT_WORKSPACE_ROOT}" \
        --format '{{.ID}}')" || return 1
    [[ "$(awk 'NF {count++} END {print count + 0}' <<<"${containers}")" == "1" ]] || return 1
    awk 'NF {print; exit}' <<<"${containers}"
}

host_container_owner_matches() {
    local container_id="$1"
    local allow_deleted="$2"
    local recorded_container recorded_pid recorded_ticks recorded_executable recorded_hash
    recorded_container="$(owner_record_value container_id 2>/dev/null || true)"
    recorded_pid="$(owner_record_value pid 2>/dev/null || true)"
    recorded_ticks="$(owner_record_value start_ticks 2>/dev/null || true)"
    recorded_executable="$(owner_record_value executable 2>/dev/null || true)"
    recorded_hash="$(owner_record_value cmdline_sha256 2>/dev/null || true)"
    [[ -n "${recorded_container}" && -n "${recorded_pid}" && -n "${recorded_ticks}" ]] || return 1
    [[ -n "${recorded_executable}" && -n "${recorded_hash}" ]] || return 1
    [[ "${container_id}" == "${recorded_container}" || "${container_id}" == "${recorded_container}"* || "${recorded_container}" == "${container_id}"* ]] || return 1
    docker exec -u iii "${container_id}" bash -s -- \
        "${recorded_pid}" "${recorded_ticks}" "${SIM_SESSION}" \
        "${recorded_executable}" "${recorded_hash}" "${allow_deleted}" <<'BASH'
set -euo pipefail
pid="$1"
expected_ticks="$2"
session="$3"
expected_executable="$4"
expected_hash="$5"
allow_deleted="$6"
tmux has-session -t "=${session}" 2>/dev/null
[[ -r "/proc/${pid}/stat" && -r "/proc/${pid}/comm" && -r "/proc/${pid}/cmdline" ]]
[[ "$(awk '{print $3}' "/proc/${pid}/stat")" != "Z" ]]
[[ "$(awk '{print $22}' "/proc/${pid}/stat")" == "${expected_ticks}" ]]
[[ "$(<"/proc/${pid}/comm")" == "px4" ]]
executable="$(readlink "/proc/${pid}/exe" 2>/dev/null || true)"
if [[ "${executable}" != "${expected_executable}" ]]; then
    [[ "${allow_deleted}" == "1" && "${executable}" == "${expected_executable} (deleted)" ]]
fi
cmdline_hash="$(tr '\0' ' ' <"/proc/${pid}/cmdline" | sha256sum | awk '{print $1}')"
[[ "${cmdline_hash}" == "${expected_hash}" ]]
BASH
}

host_owner_record_matches() {
    local records="$1"
    local allow_deleted="${2:-0}"
    local container_id pid start_ticks endpoint executable_state cmdline
    local recorded_ticks recorded_hash matched_pid="" matches=0
    owner_record_static_identity_matches || return 1
    container_id="$(workspace_container_id)" || return 1
    host_container_owner_matches "${container_id}" "${allow_deleted}" || return 1
    recorded_ticks="$(owner_record_value start_ticks)" || return 1
    recorded_hash="$(owner_record_value cmdline_sha256)" || return 1
    while IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline; do
        [[ -n "${pid}" && "${start_ticks}" == "${recorded_ticks}" ]] || continue
        [[ "$(printf '%s' "${cmdline}" | sha256sum | awk '{print $1}')" == "${recorded_hash}" ]] || continue
        if [[ "${executable_state}" == "deleted" && "${allow_deleted}" != "1" ]]; then
            continue
        fi
        [[ "${executable_state}" == "present" || "${executable_state}" == "deleted" ]] || continue
        if [[ "${allow_deleted}" != "1" && "${endpoint}" != "1" ]]; then
            continue
        fi
        matched_pid="${pid}"
        matches=$((matches + 1))
    done <<<"${records}"
    [[ "${matches}" == "1" ]] || return 1
    printf '%s\n' "${matched_pid}"
}

host_report_conflict() {
    local records="$1"
    local endpoint_pids="$2"
    local pid start_ticks endpoint executable_state cmdline
    echo "Refusing host-side HIL lifecycle operation: canonical ownership conflict at ${PI_ADDRESS}:${XRCE_PORT}." >&2
    while IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline; do
        [[ -n "${pid}" ]] || continue
        echo "  PX4 PID=${pid} start_ticks=${start_ticks} endpoint=${endpoint} executable=${executable_state} cmdline=${cmdline}" >&2
    done <<<"${records}"
    while IFS= read -r pid; do
        [[ -n "${pid}" ]] || continue
        if ! awk -F '\t' -v wanted="${pid}" '$1 == wanted {found=1} END {exit !found}' <<<"${records}"; then
            echo "  endpoint occupant PID=${pid} (not a recognized canonical PX4)" >&2
        fi
    done <<<"${endpoint_pids}"
}

host_report_unrelated_endpoint_occupants() {
    local endpoint_pids="$1"
    local owned_pid="$2"
    local pid
    while IFS= read -r pid; do
        [[ -n "${pid}" && "${pid}" != "${owned_pid}" ]] || continue
        echo "Preserving unrelated canonical endpoint occupant PID=${pid} while stopping the recorded HIL instance." >&2
    done <<<"${endpoint_pids}"
}

host_lifecycle_preflight() {
    [[ "${ACTION}" == "start" || "${ACTION}" == "stop" || "${ACTION}" == "status" ]] || return 0
    if ! PI_ADDRESS="$(host_resolve_pi_address)"; then
        if [[ "${explicit_host:-0}" == "1" ]]; then
            echo "Unable to resolve selected HIL Pi host ${PI_ENDPOINT}; refusing ${ACTION}." >&2
        else
            echo "Unable to select a consistent HIL Pi peer; refusing ${ACTION}." >&2
        fi
        return 1
    fi
    [[ -n "${PI_ADDRESS}" ]] || {
        if [[ "${ACTION}" == "start" || "${explicit_host:-0}" == "1" ]]; then
            echo "Unable to resolve selected HIL Pi host ${PI_ENDPOINT}; refusing ${ACTION}." >&2
            return 1
        fi
        return 0
    }
    export III_HIL_PI_ADDRESS="${PI_ADDRESS}"
    if [[ "${explicit_host:-0}" == "1" && ( "${ACTION}" == "status" || "${ACTION}" == "stop" ) ]]; then
        local recorded_pi_address recorded_peer_address
        recorded_pi_address="$(owner_record_value pi_address 2>/dev/null || true)"
        recorded_peer_address="$(session_pi_address 2>/dev/null || true)"
        if [[ -n "${recorded_pi_address}" && -n "${recorded_peer_address}" && "${recorded_pi_address}" != "${recorded_peer_address}" ]]; then
            echo "Recorded HIL owner peer ${recorded_pi_address} conflicts with the pinned session peer ${recorded_peer_address}; refusing ${ACTION}." >&2
            return 1
        fi
        [[ -n "${recorded_pi_address}" ]] || recorded_pi_address="${recorded_peer_address}"
        if [[ -n "${recorded_pi_address}" && "${recorded_pi_address}" != "${PI_ADDRESS}" ]]; then
            echo "Explicit HIL Pi host ${PI_ADDRESS} does not match the recorded owner peer ${recorded_pi_address}; refusing ${ACTION}." >&2
            return 1
        fi
    fi
    if ! select_workstation_route "${PI_ADDRESS}"; then
        [[ "${ACTION}" != "start" ]] || return 1
        # Status and stop still need the recorded peer and local process
        # ownership evidence when the Pi route is down. Do not forward a stale
        # source address into the container in that case.
        WORKSTATION_ADDRESS=""
        WORKSTATION_INTERFACE=""
        export III_HIL_WORKSTATION_ADDRESS=""
        export III_HIL_WORKSTATION_INTERFACE=""
    fi
    [[ -n "${III_HIL_NO_CONTAINER_REEXEC:-}" ]] && return 0
    local records endpoint_pids px4_count endpoint_count
    endpoint_pids="$(host_endpoint_pids)" || {
        [[ "${ACTION}" == "start" ]] && echo "Unable to inspect host HIL UDP ownership; refusing to start." >&2
        return $([[ "${ACTION}" == "start" ]] && echo 1 || echo 0)
    }
    records="$(host_px4_records "${endpoint_pids}")" || {
        [[ "${ACTION}" == "start" ]] && echo "Unable to inspect host HIL PX4 ownership; refusing to start." >&2
        return $([[ "${ACTION}" == "start" ]] && echo 1 || echo 0)
    }
    px4_count="$(awk 'NF {count++} END {print count + 0}' <<<"${records}")"
    endpoint_count="$(awk 'NF {count++} END {print count + 0}' <<<"${endpoint_pids}")"
    [[ "${px4_count}" == "0" && "${endpoint_count}" == "0" ]] && return 0

    case "${ACTION}" in
        start)
            if [[ "${px4_count}" == "1" && "${endpoint_count}" == "1" ]] && host_owner_record_matches "${records}" 0 >/dev/null; then
                return 0
            fi
            host_report_conflict "${records}" "${endpoint_pids}"
            return 1
            ;;
        status)
            if [[ "${px4_count}" == "1" ]] && host_owner_record_matches "${records}" 1 >/dev/null; then
                return 0
            fi
            echo "canonical_tmux_session: container-runtime (host-side ownership evidence)"
            echo "canonical_px4_process_alive: $([[ "${px4_count}" != "0" ]] && echo yes || echo no)"
            echo "canonical_px4_pid: $(awk -F '\t' '{printf "%s ", $1}' <<<"${records}")"
            echo "canonical_xrce_endpoint_ownership: $([[ "${endpoint_count}" == "1" ]] && echo yes || echo no)"
            echo "conflicting_px4_owners: count=${px4_count} pids=$(awk -F '\t' '{printf "%s ", $1}' <<<"${records}")"
            echo "hil_owner_state: error_multiple_or_orphan_host_owner"
            echo "hil_adapters: unavailable (container status not queried)"
            echo "hil_pi_reachability: available"
            echo "hil_readiness: degraded"
            return 1
            ;;
        stop)
            local owned_host_pid=""
            if [[ "${px4_count}" == "1" ]]; then
                owned_host_pid="$(host_owner_record_matches "${records}" 1 2>/dev/null || true)"
            fi
            if [[ -n "${owned_host_pid}" ]]; then
                host_report_unrelated_endpoint_occupants "${endpoint_pids}" "${owned_host_pid}"
                return 0
            fi
            host_report_conflict "${records}" "${endpoint_pids}"
            echo "Use the managed container lifecycle or an explicit, positively identified cleanup for this host-side owner; start/stop will not kill it implicitly." >&2
            return 1
            ;;
    esac
}

host_lifecycle_preflight || exit $?

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
    forwarded_environment+=(--env "III_HIL_NO_CONTAINER_REEXEC=1")
    forwarded_environment+=(--env "III_HIL_CONTAINER_ID=${container_id}")
    exec docker exec -u iii "${forwarded_environment[@]}" "${container_id}" \
        /home/iii/ws/tools/simulation/launch_hil_workstation.sh "${ACTION}" "$@"
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
    # A lifecycle lock is held by the launcher shell for the complete
    # start/stop operation.  Do not pass that descriptor into the tmux server:
    # a server that survives the launcher would retain the lock and deadlock
    # the next lifecycle operation (notably stop after start).
    if [[ -n "${HIL_LIFECYCLE_LOCK_FD:-}" ]]; then
        local lock_fd="${HIL_LIFECYCLE_LOCK_FD}"
        session_user_command bash -c '
            close_fd="$1"
            shift
            eval "exec ${close_fd}>&- 2>/dev/null || true"
            exec tmux "$@"
        ' bash "${lock_fd}" "$@"
    else
        session_user_command tmux "$@"
    fi
}

session_exists() {
    tmux_command has-session -t "=$1" 2>/dev/null
}

sim_session_healthy() {
    session_exists "${SIM_SESSION}" &&
        [[ "$(tmux_command display-message -p -t "${SIM_SESSION}:simulation.0" '#{pane_dead}' 2>/dev/null)" == "0" ]]
}

ros_environment() {
    printf 'export ROS_DOMAIN_ID=%q ROS_LOCALHOST_ONLY=0 ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET ROS2CLI_DISABLE_DAEMON=1 RMW_IMPLEMENTATION=rmw_fastrtps_cpp FASTDDS_BUILTIN_TRANSPORTS=UDPv4 GZ_PARTITION=%q' \
        "${ROS_DOMAIN_ID}" "${GZ_PARTITION}"
}

resolve_pi_address() {
    if [[ -n "${PI_ADDRESS}" ]]; then
        printf '%s\n' "${PI_ADDRESS}"
        return 0
    fi
    local resolved
    resolved="$(getent ahostsv4 "${PI_ENDPOINT}" | awk 'NR == 1 { print $1; exit }')"
    if [[ -n "${resolved}" ]]; then
        printf '%s\n' "${resolved}"
        return 0
    fi
    if session_pi_address; then
        return 0
    fi
    echo "Unable to resolve HIL Pi endpoint ${PI_ENDPOINT}; set III_HIL_PI_ADDRESS to an explicit IPv4 address." >&2
    return 1
}

px4_agent_address_u32() {
    if [[ -n "${PX4_AGENT_ADDRESS_U32_OVERRIDE}" ]]; then
        printf '%s\n' "${PX4_AGENT_ADDRESS_U32_OVERRIDE}"
        return 0
    fi
    local pi_address
    pi_address="$(resolve_pi_address)" || return 1
    python3 - "${pi_address}" <<'PY'
import ipaddress
import sys

# PX4's integer parameter is the address bytes interpreted in network order.
print(int.from_bytes(ipaddress.IPv4Address(sys.argv[1]).packed, "big"))
PY
}

session_pi_address() {
    local address
    local mode
    [[ -f "${HIL_PEER_ADDRESS_FILE}" && ! -L "${HIL_PEER_ADDRESS_FILE}" && -O "${HIL_PEER_ADDRESS_FILE}" ]] || return 1
    mode="$(stat -c '%a' "${HIL_PEER_ADDRESS_FILE}")" || return 1
    (( (8#${mode} & 077) == 0 )) || return 1
    address="$(<"${HIL_PEER_ADDRESS_FILE}")"
    valid_ipv4 "${address}" || return 1
    printf '%s\n' "${address}"
}

record_pi_address() {
    local temporary
    valid_ipv4 "${PI_ADDRESS}" || return 1
    mkdir -p "${HIL_RUNTIME_DIR}"
    chmod 700 "${HIL_RUNTIME_DIR}"
    temporary="$(mktemp "${HIL_PEER_ADDRESS_FILE}.tmp.XXXXXX")"
    chmod 600 "${temporary}"
    printf '%s\n' "${PI_ADDRESS}" >"${temporary}"
    mv -f "${temporary}" "${HIL_PEER_ADDRESS_FILE}"
}

link_probe() {
    local pi_address
    pi_address="$(resolve_pi_address)" || return 1
    python3 - "${WORKSTATION_ADDRESS}" "${pi_address}" <<'PY'
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

proc_is_alive() {
    local pid="$1"
    local state
    [[ -r "${PROC_ROOT}/${pid}/stat" ]] || return 1
    state="$(awk '{print $3}' "${PROC_ROOT}/${pid}/stat" 2>/dev/null)" || return 1
    [[ "${state}" != "Z" ]]
}

proc_start_ticks() {
    local pid="$1"
    awk '{print $22}' "${PROC_ROOT}/${pid}/stat" 2>/dev/null
}

proc_cmdline() {
    local pid="$1"
    [[ -r "${PROC_ROOT}/${pid}/cmdline" ]] || return 1
    tr '\0' ' ' <"${PROC_ROOT}/${pid}/cmdline"
}

canonical_endpoint_pids() {
    local line output
    local peer="${PI_ADDRESS}:${XRCE_PORT}"
    if command -v ss >/dev/null 2>&1; then
        if ! output="$(ss -4 -H -u -a -n -p "dst ${peer}" 2>/dev/null)"; then
            return 1
        fi
        while IFS= read -r line; do
            while [[ "${line}" =~ pid=([0-9]+) ]]; do
                printf '%s\n' "${BASH_REMATCH[1]}"
                line="${line#*pid=${BASH_REMATCH[1]}}"
            done
        done <<<"${output}"
        return 0
    fi

    # The devcontainer does not include iproute2's ss. Fall back to the kernel
    # UDP table and socket inodes, which keeps the inspection in the same PID
    # and network namespace as the PX4 process.
    local remote_hex port_hex inodes proc fd link inode found=0
    remote_hex="$(python3 - "${PI_ADDRESS}" <<'PY'
import ipaddress
import sys
print(ipaddress.IPv4Address(sys.argv[1]).packed[::-1].hex().upper())
PY
)" || return 1
    printf -v port_hex '%04X' "${XRCE_PORT}"
    inodes="$(awk -v remote="${remote_hex}:${port_hex}" '$3 == remote {print $10}' "${PROC_ROOT}/net/udp" 2>/dev/null)"
    [[ -n "${inodes}" ]] || return 0
    for proc in "${PROC_ROOT}"/[0-9]*; do
        [[ -r "${proc}/comm" ]] || continue
        [[ "$(<"${proc}/comm")" == "px4" ]] || continue
        for fd in "${proc}"/fd/*; do
            link="$(readlink "${fd}" 2>/dev/null || true)"
            for inode in ${inodes}; do
                if [[ "${link}" == "socket:[${inode}]" ]]; then
                    printf '%s\n' "${proc##*/}"
                    found=1
                    break 2
                fi
            done
        done
    done
    if [[ "${found}" == "0" ]]; then
        printf '%s\n' '__unattributed__'
    fi
}

canonical_px4_records() {
    local proc pid comm exe cmdline executable_state start_ticks endpoint=0
    local endpoint_pids="${1-}"
    local -a proc_roots=()
    # Callers that already resolved endpoint ownership (notably status) pass
    # it through so the /proc socket walk is performed only once.
    if [[ "$#" == "0" ]]; then
        endpoint_pids="$(canonical_endpoint_pids)" || return 1
    fi
    if [[ "${PROC_ROOT}" == "/proc" ]] && command -v pgrep >/dev/null 2>&1; then
        while IFS= read -r pid; do
            [[ -n "${pid}" ]] && proc_roots+=("${PROC_ROOT}/${pid}")
        done < <(pgrep -x px4 2>/dev/null || true)
    else
        proc_roots=("${PROC_ROOT}"/[0-9]*)
    fi
    for proc in "${proc_roots[@]}"; do
        pid="${proc##*/}"
        [[ -r "${proc}/comm" && -r "${proc}/cmdline" && -r "${proc}/stat" ]] || continue
        [[ "$(awk '{print $3}' "${proc}/stat" 2>/dev/null)" != "Z" ]] || continue
        comm="$(<"${proc}/comm")"
        [[ "${comm}" == "px4" ]] || continue
        exe="$(readlink "${proc}/exe" 2>/dev/null || true)"
        case "${exe}" in
            "${PX4_BUILD_DIR}/bin/px4") executable_state=present ;;
            "${PX4_BUILD_DIR}/bin/px4 (deleted)") executable_state=deleted ;;
            *) continue ;;
        esac
        cmdline="$(proc_cmdline "${pid}" 2>/dev/null || true)"
        [[ "${cmdline}" == *" -s ${PX4_STARTUP_SCRIPT} "* ]] || continue
        [[ "${cmdline}" == *" -i ${PX4_INSTANCE} "* ]] || continue
        endpoint=0
        if grep -Fxq "${pid}" <<<"${endpoint_pids}"; then
            endpoint=1
        fi
        start_ticks="$(proc_start_ticks "${pid}")"
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "${pid}" "${start_ticks}" "${endpoint}" "${executable_state}" "${cmdline}"
    done
    return 0
}

managed_px4_pid() {
    local candidate="$1"
    local expected_start_ticks="${2:-}"
    local candidate_pgid pane_pid pane_pgid
    proc_is_alive "${candidate}" || return 1
    if [[ -n "${expected_start_ticks}" ]] && [[ "$(proc_start_ticks "${candidate}")" != "${expected_start_ticks}" ]]; then
        return 1
    fi
    session_exists "${SIM_SESSION}" || return 1
    candidate_pgid="$(ps -o pgid= -p "${candidate}" 2>/dev/null | tr -d ' ')"
    [[ -n "${candidate_pgid}" ]] || return 1
    while IFS= read -r pane_pid; do
        [[ -n "${pane_pid}" ]] || continue
        pane_pgid="$(ps -o pgid= -p "${pane_pid}" 2>/dev/null | tr -d ' ')"
        if [[ -n "${pane_pgid}" && "${pane_pgid}" == "${candidate_pgid}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done < <(tmux_command list-panes -t "${SIM_SESSION}:simulation" -F '#{pane_pid}' 2>/dev/null || true)
    return 1
}

current_container_id() {
    printf '%s\n' "${III_HIL_CONTAINER_ID:-${HOSTNAME:-unknown}}"
}

owner_record_container_matches() {
    local current recorded
    current="$(current_container_id)"
    recorded="$(owner_record_value container_id 2>/dev/null || true)"
    [[ -n "${current}" && -n "${recorded}" ]] || return 1
    [[ "${current}" == "${recorded}" || "${current}" == "${recorded}"* || "${recorded}" == "${current}"* ]]
}

owned_px4_record() {
    local records="$1"
    local allow_deleted="${2:-0}"
    local pid start_ticks endpoint executable_state cmdline
    local recorded_pid recorded_ticks recorded_executable recorded_hash
    local matched_record="" matches=0
    owner_record_static_identity_matches || return 1
    owner_record_container_matches || return 1
    recorded_pid="$(owner_record_value pid 2>/dev/null || true)"
    recorded_ticks="$(owner_record_value start_ticks 2>/dev/null || true)"
    recorded_executable="$(owner_record_value executable 2>/dev/null || true)"
    recorded_hash="$(owner_record_value cmdline_sha256 2>/dev/null || true)"
    [[ "${recorded_executable}" == "${PX4_BUILD_DIR}/bin/px4" ]] || return 1
    [[ -n "${recorded_pid}" && -n "${recorded_ticks}" && -n "${recorded_hash}" ]] || return 1
    while IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline; do
        [[ -n "${pid}" && "${pid}" == "${recorded_pid}" ]] || continue
        [[ "${start_ticks}" == "${recorded_ticks}" ]] || continue
        [[ "$(printf '%s' "${cmdline}" | sha256sum | awk '{print $1}')" == "${recorded_hash}" ]] || continue
        if [[ "${executable_state}" == "deleted" && "${allow_deleted}" != "1" ]]; then
            continue
        fi
        [[ "${executable_state}" == "present" || "${executable_state}" == "deleted" ]] || continue
        managed_px4_pid "${pid}" "${start_ticks}" >/dev/null 2>&1 || continue
        matched_record="${pid}"$'\t'"${start_ticks}"$'\t'"${endpoint}"$'\t'"${executable_state}"$'\t'"${cmdline}"
        matches=$((matches + 1))
    done <<<"${records}"
    [[ "${matches}" == "1" ]] || return 1
    printf '%s\n' "${matched_record}"
}

owned_session_without_process() {
    local recorded_pid
    owner_record_static_identity_matches || return 1
    owner_record_container_matches || return 1
    [[ "$(owner_record_value executable 2>/dev/null || true)" == "${PX4_BUILD_DIR}/bin/px4" ]] || return 1
    [[ -n "$(owner_record_value cmdline_sha256 2>/dev/null || true)" ]] || return 1
    recorded_pid="$(owner_record_value pid 2>/dev/null || true)"
    [[ -n "${recorded_pid}" ]] || return 1
    # A live PID that no longer matches the record is ambiguous and must never
    # authorize cleanup of a possibly reused tmux session.
    proc_is_alive "${recorded_pid}" && return 1
    session_exists "${SIM_SESSION}"
}

write_owner_record() {
    local pid="$1"
    local start_ticks="$2"
    local temporary executable cmdline_hash container_id
    executable="${PX4_BUILD_DIR}/bin/px4"
    cmdline_hash="$(proc_cmdline "${pid}" | sha256sum | awk '{print $1}')" || return 1
    container_id="$(current_container_id)"
    [[ -n "${cmdline_hash}" && -n "${container_id}" ]] || return 1
    mkdir -p "${HIL_RUNTIME_DIR}"
    chmod 700 "${HIL_RUNTIME_DIR}"
    temporary="${HIL_OWNER_RECORD}.tmp.$$"
    umask 077
    {
        printf 'pid=%s\n' "${pid}"
        printf 'start_ticks=%s\n' "${start_ticks}"
        printf 'executable=%s\n' "${executable}"
        printf 'cmdline_sha256=%s\n' "${cmdline_hash}"
        printf 'container_id=%s\n' "${container_id}"
        printf 'session=%s\n' "${SIM_SESSION}"
        printf 'pi_address=%s\n' "${PI_ADDRESS}"
        printf 'xrce_port=%s\n' "${XRCE_PORT}"
        printf 'gz_partition=%s\n' "${GZ_PARTITION}"
        printf 'rendered=%s\n' "${HIL_RENDERED}"
        printf 'started_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"${temporary}"
    chmod 600 "${temporary}"
    mv -f "${temporary}" "${HIL_OWNER_RECORD}"
}

update_owner_render_mode() {
    local temporary="${HIL_OWNER_RECORD}.tmp.$$"
    [[ -f "${HIL_OWNER_RECORD}" ]] || return 1
    [[ "$(grep -c '^rendered=' "${HIL_OWNER_RECORD}")" == "1" ]] || return 1
    umask 077
    sed "s/^rendered=.*/rendered=${HIL_RENDERED}/" "${HIL_OWNER_RECORD}" >"${temporary}" || {
        rm -f -- "${temporary}"
        return 1
    }
    chmod 600 "${temporary}"
    mv -f -- "${temporary}" "${HIL_OWNER_RECORD}"
}

owner_render_mode() {
    local rendered
    [[ -f "${HIL_OWNER_RECORD}" ]] || {
        printf '%s\n' unknown
        return
    }
    rendered="$(sed -n 's/^rendered=//p' "${HIL_OWNER_RECORD}" | head -n1)"
    case "${rendered}" in
        1) printf '%s\n' rendered ;;
        0) printf '%s\n' headless ;;
        *) printf '%s\n' unknown ;;
    esac
}

gazebo_gui_process_running() {
    local pane_pid="$1" pane_command="$2" command_line
    [[ "${pane_command}" != "bash" && "${pane_pid}" =~ ^[0-9]+$ ]] || return 1
    [[ -r "${PROC_ROOT}/${pane_pid}/cmdline" ]] || return 1
    command_line="$(tr '\0' ' ' <"${PROC_ROOT}/${pane_pid}/cmdline")"
    [[ "${command_line}" =~ (^|[[:space:]/])gz[[:space:]]sim[[:space:]]-g([[:space:]]|$) ||
       "${command_line}" =~ (^|[[:space:]/])gz-sim-gui([[:space:]]|$) ]]
}

gazebo_viewer_state() {
    local pane_index pane_dead pane_title pane_command pane_pid found_dead=0 found_starting=0
    if ! session_exists "${SIM_SESSION}"; then
        printf '%s\n' stopped
        return
    fi
    while IFS=$'\t' read -r pane_index pane_dead pane_title pane_command pane_pid; do
        [[ "${pane_title}" == "Gazebo GUI" ]] || continue
        if [[ "${pane_dead}" == "0" ]]; then
            if gazebo_gui_process_running "${pane_pid}" "${pane_command}"; then
                printf '%s\n' running
                return
            fi
            found_starting=1
            continue
        fi
        found_dead=1
    done < <(tmux_command list-panes -t "${SIM_SESSION}:simulation" \
        -F $'#{pane_index}\t#{pane_dead}\t#{pane_title}\t#{pane_current_command}\t#{pane_pid}' 2>/dev/null || true)
    if ((found_starting)); then
        printf '%s\n' starting
    elif ((found_dead)); then
        printf '%s\n' dead
    else
        printf '%s\n' missing
    fi
}

run_simulation_launcher() {
    local -a arguments=(--no-attach)
    if [[ "${HIL_RENDERED}" == "1" ]]; then
        arguments+=(--rendered)
    else
        arguments+=(--headless)
    fi
    env \
        III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
        III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
        III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
        III_SIM_TOOLS_PX4_BUILD_DIR="${PX4_BUILD_DIR}" \
        GZ_PARTITION="${GZ_PARTITION}" \
        "${SIM_LAUNCHER}" "${arguments[@]}"
}

report_owner_conflict() {
    local records="$1"
    local endpoint_pids="$2"
    local pid start_ticks endpoint executable_state cmdline
    echo "Refusing to start: canonical HIL ownership conflict at ${PI_ADDRESS}:${XRCE_PORT}." >&2
    while IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline; do
        [[ -n "${pid}" ]] || continue
        echo "  PX4 PID=${pid} start_ticks=${start_ticks} endpoint=${endpoint} executable=${executable_state} cmdline=${cmdline}" >&2
    done <<<"${records}"
    while IFS= read -r pid; do
        [[ -n "${pid}" ]] || continue
        if ! awk -F '\t' -v wanted="${pid}" '$1 == wanted {found=1} END {exit !found}' <<<"${records}"; then
            echo "  endpoint occupant PID=${pid} (not the canonical PX4 executable)" >&2
        fi
    done <<<"${endpoint_pids}"
}

report_unrelated_endpoint_occupants() {
    local endpoint_pids="$1"
    local owned_pid="${2:-}"
    local pid
    while IFS= read -r pid; do
        [[ -n "${pid}" && "${pid}" != "${owned_pid}" ]] || continue
        echo "Preserving unrelated canonical endpoint occupant PID=${pid}." >&2
    done <<<"${endpoint_pids}"
}

terminate_managed_px4() {
    local pid="$1"
    local deadline
    proc_is_alive "${pid}" || {
        echo "Managed PX4 PID ${pid} disappeared before termination." >&2
        return 0
    }
    # The owner record and live start-ticks/cmdline checks identify this exact
    # process. Signal only that PID; the recorded tmux session and simulation
    # launcher are stopped separately, so a shared process group can never
    # turn an HIL cleanup into a broad kill.
    echo "Stopping managed canonical HIL PX4 PID ${pid}."
    kill -TERM "${pid}" 2>/dev/null || true
    deadline=$((SECONDS + ${III_HIL_STOP_TIMEOUT_SEC:-15}))
    while ((SECONDS < deadline)) && proc_is_alive "${pid}"; do
        sleep 1
    done
    if proc_is_alive "${pid}"; then
        echo "Managed PX4 PID ${pid} did not exit after graceful termination; escalating to SIGKILL." >&2
        kill -KILL "${pid}" 2>/dev/null || true
        deadline=$((SECONDS + 5))
        while ((SECONDS < deadline)) && proc_is_alive "${pid}"; do
            sleep 1
        done
    fi
    if proc_is_alive "${pid}"; then
        echo "Managed PX4 PID ${pid} is still alive after termination escalation." >&2
        return 1
    fi
}

acquire_lifecycle_lock() {
    local lock_fd
    command -v flock >/dev/null 2>&1 || {
        echo "flock is required to serialize HIL lifecycle operations." >&2
        return 1
    }
    mkdir -p "${HIL_RUNTIME_DIR}"
    chmod 700 "${HIL_RUNTIME_DIR}"
    exec {lock_fd}>"${HIL_LIFECYCLE_LOCK}"
    flock -x "${lock_fd}"
    HIL_LIFECYCLE_LOCK_FD="${lock_fd}"
}

release_lifecycle_lock() {
    if [[ -n "${HIL_LIFECYCLE_LOCK_FD:-}" ]]; then
        flock -u "${HIL_LIFECYCLE_LOCK_FD}" 2>/dev/null || true
        eval "exec ${HIL_LIFECYCLE_LOCK_FD}>&- 2>/dev/null || true"
        HIL_LIFECYCLE_LOCK_FD=""
    fi
}

wait_for_single_px4_owner() {
    local records endpoint_pids count pid start_ticks endpoint executable_state cmdline
    for attempt in {1..30}; do
        endpoint_pids="$(canonical_endpoint_pids)" || return 1
        records="$(canonical_px4_records "${endpoint_pids}")" || return 1
        count="$(awk 'NF {count++} END {print count + 0}' <<<"${records}")"
        if [[ "${count}" == "1" ]]; then
            IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline <<<"${records}"
            if [[ "${endpoint}" == "1" && "${executable_state}" == "present" ]]; then
                printf '%s\t%s\t%s\t%s\t%s\n' \
                    "${pid}" "${start_ticks}" "${endpoint}" "${executable_state}" "${cmdline}"
                return 0
            fi
        fi
        sleep 1
    done
    echo "Timed out waiting for exactly one canonical PX4 owner on ${PI_ADDRESS}:${XRCE_PORT}." >&2
    return 1
}

px4_command() {
    # PX4's POSIX rcS rewrites UXRCE_DDS_DOM_ID from ROS_DOMAIN_ID
    # immediately before it starts the client. A PX4_PARAM_ override alone is
    # silently overwritten with domain 0.
    # In split-host HIL the agent uses workstation wall time while PX4 SITL
    # advances lockstep simulation time. PX4 timestamp synchronisation would
    # repeatedly jump between those clocks and can starve command handling.
    # Leave Cable deliberately disarms while attached to the cable and then
    # arms again for takeoff.  Keep a long landed-disarm grace in HIL so PX4
    # does not undo that explicit arm during the cable handoff; the mission's
    # explicit disarm remains the acceptance/safety boundary.
    # SITL has no physical ESC telemetry.  Leaving PX4's runtime ESC failure
    # detector enabled makes a force-arm immediately trigger an ESC failsafe
    # and disarm, even though Gazebo is publishing valid actuator dynamics.
    # This is HIL-only; the physical PX4 path keeps the detector unchanged.
    local agent_address_u32
    agent_address_u32="$(px4_agent_address_u32)" || return 1
    printf '%s' "source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && exec env HEADLESS=1 GZ_IP=127.0.0.1 GZ_PARTITION='${GZ_PARTITION}' PX4_SIM_MODEL=gz_d4s_dc_drone ROS_DOMAIN_ID='${ROS_DOMAIN_ID}' PX4_UXRCE_DDS_PORT='${XRCE_PORT}' PX4_PARAM_UXRCE_DDS_AG_IP='${agent_address_u32}' PX4_PARAM_UXRCE_DDS_KEY='${PX4_DDS_CLIENT_KEY}' PX4_PARAM_UXRCE_DDS_SYNCT=0 PX4_PARAM_COM_DL_LOSS_T=300 PX4_PARAM_COM_LOW_BAT_ACT=0 PX4_PARAM_FD_ESCS_EN=0 '${PX4_BUILD_DIR}/bin/px4' -s '${PX4_STARTUP_SCRIPT}' -i '${PX4_INSTANCE}' -w '${PX4_BUILD_DIR}/rootfs' '${PX4_BUILD_DIR}/etc'"
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
    # PX4's canonical POSIX rcS unconditionally derives MAV_SYS_ID and the
    # uXRCE client key from the instance after applying environment parameter
    # overrides. Keep the startup copy canonical except for these two HIL
    # identities: MAV_SYS_ID must match the deterministic MAVLink endpoint and
    # the DDS client key must not collide with the physical PX4 on the Pi.
    awk -v system_id="${PX4_SYSTEM_ID}" -v dds_client_key="${PX4_DDS_CLIENT_KEY}" '
        $0 == "param set MAV_SYS_ID $((px4_instance+1))" {
            print "param set MAV_SYS_ID " system_id
            system_id_replacements += 1
            next
        }
        $0 == "param set UXRCE_DDS_KEY $((px4_instance+1))" {
            print "param set UXRCE_DDS_KEY " dds_client_key
            dds_key_replacements += 1
            next
        }
        { print }
        END { if (system_id_replacements != 1 || dds_key_replacements != 1) exit 42 }
    ' "${PX4_CANONICAL_RCS}" >"${temporary_script}" || {
        rm -f "${temporary_script}"
        echo "PX4 rcS identity assignments changed; refusing an unvalidated HIL startup." >&2
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
    # HIL carries only the canonical inspection inputs across the Pi link.
    # Full radar/depth/label diagnostics stay opt-in so they cannot starve the
    # control-plane uXRCE session on the 100 Mb/s direct Ethernet path.
    bridge_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && ${ros_env} && exec ros2 launch iii_drone_simulation sim_assets.launch.py include_diagnostics:=false use_camera_rate_limiter:=true camera_output_topic:=/simulation/local/cable_camera/image_raw camera_rate_hz:=${CAMERA_RATE_HZ}"
    # The payload uses the same PX4 BatteryStatus input and SimBatteryCharge
    # output as SIM. Both topics cross the Pi/workstation Fast DDS link.
    payload_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' || exit; ${ros_env}; ros2 run iii_drone_simulation sim_charger_gripper_node --ros-args -p use_sim_time:=true & node_pid=\$!; for attempt in {1..60}; do ros2 lifecycle get /payload/charger_gripper/charger_gripper >/dev/null 2>&1 && break; sleep 0.5; done; ros2 lifecycle set /payload/charger_gripper/charger_gripper configure && ros2 lifecycle set /payload/charger_gripper/charger_gripper activate; wait \${node_pid}"
    # The Pi owns dynamic world-to-drone TF from PX4 odometry. This adapter
    # publishes only the simulated payload statics, preventing Gazebo
    # ground-truth odometry from competing with the controller's PX4 state.
    tf_command="source '${WORKSPACE_ROOT}/setup/setup_dev.bash' && ${ros_env} && exec ros2 launch iii_drone_simulation tf_sim.launch.py use_ground_truth_odometry:=true publish_world_to_drone:=false"

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

battery_check() {
    local ros_env
    ros_env="$(ros_environment)"
    if ! session_user_command bash -lc "source '${WORKSPACE_ROOT}/setup/setup_dev.bash'; ${ros_env}; for attempt in {1..4}; do if timeout 2 ros2 topic echo --once /payload/charger_gripper/px4_battery_source_fresh std_msgs/msg/Bool --field data | grep -q '^True$'; then exit 0; fi; done; exit 1"; then
        echo "HIL battery link unavailable: workstation charger has no fresh Pi PX4 BatteryStatus; refusing mission readiness." >&2
        return 1
    fi
    echo "HIL battery link ready: workstation charger receives fresh Pi PX4 BatteryStatus."
}

adapter_probe_script() {
    # One login shell sources the workspace once and runs every workstation
    # adapter check concurrently; run_adapter_probes bounds the whole probe.
    printf "source '%s/setup/setup_dev.bash'; %s\n" "${WORKSPACE_ROOT}" "$(ros_environment)"
    printf 'probe_timeout=%q\n' "${ADAPTER_SINGLE_PROBE_TIMEOUT_SEC}"
    cat <<'EOF'
probe_pids=()
result=0
timeout "${probe_timeout}" ros2 lifecycle get /payload/charger_gripper/charger_gripper | grep -q '^active ' & probe_pids+=("$!")
timeout "${probe_timeout}" ros2 topic echo --once /clock rosgraph_msgs/msg/Clock & probe_pids+=("$!")
# Check the workstation-owned static branches directly. The dynamic
# world->drone heartbeat is Pi-owned and cannot exist before Pi boot.
timeout "${probe_timeout}" bash -c 'ros2 run tf2_ros tf2_echo drone cable_gripper 2>&1 | grep -m1 "Translation:"' & probe_pids+=("$!")
timeout "${probe_timeout}" bash -c 'ros2 run tf2_ros tf2_echo drone mmwave 2>&1 | grep -m1 "Translation:"' & probe_pids+=("$!")
# A live process is insufficient here: the Python rate limiter can remain
# discoverable after it stops forwarding frames. Verify the actual bounded,
# compressed output that the Pi consumes.
timeout "${probe_timeout}" ros2 topic echo /sensor/cable_camera/image_raw/compressed --once --field header --qos-reliability best_effort & probe_pids+=("$!")
for probe_pid in "${probe_pids[@]}"; do
    wait "${probe_pid}" || result=1
done
exit "${result}"
EOF
}

run_adapter_probes() {
    local probe_timeout="${ADAPTER_PROBE_TIMEOUT_SEC}"
    [[ "${probe_timeout}" =~ ^[1-9][0-9]*$ ]] || probe_timeout=20
    session_user_command timeout -k 2 "${probe_timeout}" bash -lc "$(adapter_probe_script)" >/dev/null 2>&1
}

adapters_locally_ready() {
    adapter_panes_healthy || return 1
    run_adapter_probes
}

adapters_ready() {
    adapter_panes_healthy || return 1
    local result=0
    # Static drone-to-payload transforms are supplied by this workstation;
    # don't wait for the Pi-owned dynamic broadcaster during local readiness.
    # PX4 vehicle status is published by the Pi-local XRCE agent. The Pi
    # Runtime API checks its heartbeat and arming state directly; requiring
    # this DDS publisher to cross back to the workstation rejects an otherwise
    # healthy split-host graph. These probes cover workstation-owned inputs.
    run_adapter_probes || result=1
    if ((result == 0)); then
        ADAPTER_READINESS_CONFIRMED=1
    else
        ADAPTER_READINESS_CONFIRMED=0
    fi
    return "${result}"
}

ensure_adapters_ready() {
    # Reuse and fresh-start paths share the same recovery gate. A verified
    # PX4/Gazebo owner can be retained while an unhealthy local adapter epoch
    # is replaced, but adapter readiness is still required before success.
    if session_exists "${ADAPTER_SESSION}" && ! adapters_locally_ready; then
        tmux_command kill-session -t "${ADAPTER_SESSION}"
    fi
    if ! session_exists "${ADAPTER_SESSION}"; then
        start_adapters
    fi
    ADAPTER_READINESS_CONFIRMED=0
    local adapters_confirmed=0
    for attempt in {1..90}; do
        if adapters_ready; then
            adapters_confirmed=1
            break
        fi
        sleep 1
    done
    if ((adapters_confirmed == 0)); then
        echo "HIL workstation adapters did not become ready." >&2
        return 1
    fi
}

gazebo_owner() {
    python3 "${GZ_OWNER_HELPER}" "$1" --workspace "${WORKSPACE_ROOT}" \
        --partition "${GZ_PARTITION}" --record "${GZ_OWNER_RECORD}" "${@:2}"
}

require_empty_gazebo_partition() {
    local topics services
    # Discover at the Gazebo transport boundary as well as in /proc: host
    # networking can expose a server hidden in another container's PID space.
    topics="$(session_user_command env GZ_IP=127.0.0.1 GZ_PARTITION="${GZ_PARTITION}" timeout 5 gz topic -l)" || {
        echo "Unable to inspect Gazebo partition ${GZ_PARTITION}; refusing start." >&2
        return 1
    }
    services="$(session_user_command env GZ_IP=127.0.0.1 GZ_PARTITION="${GZ_PARTITION}" timeout 5 gz service -l)" || {
        echo "Unable to inspect Gazebo services in partition ${GZ_PARTITION}; refusing start." >&2
        return 1
    }
    if grep -Eq '^/world/[^/]+(/|$)' <<<"${topics}"$'\n'"${services}"; then
        echo "Gazebo partition ${GZ_PARTITION} already contains a world without this HIL PX4 owner; stop its verified owner before starting." >&2
        return 1
    fi
}

print_status() {
    local result=0
    local records=""
    local endpoint_pids=""
    local endpoint_probe=1
    local px4_count=0
    local canonical_endpoint_count=0
    local pid start_ticks endpoint executable_state cmdline owned_record=""
    echo "hil_gz_partition: ${GZ_PARTITION}"
    echo "hil_render_mode: $(owner_render_mode)"
    echo "hil_gazebo_viewer: $(gazebo_viewer_state)"
    if gazebo_owner inspect >/dev/null 2>&1; then
        echo "hil_gazebo_ownership: ready"
    else
        echo "hil_gazebo_ownership: unavailable"
        result=1
    fi
    PI_ADDRESS="$(resolve_pi_address 2>/dev/null || true)"
    if [[ -n "${PI_ADDRESS}" ]]; then
        if ! endpoint_pids="$(canonical_endpoint_pids)"; then
            endpoint_probe=0
        fi
        records="$(canonical_px4_records "${endpoint_pids}" 2>/dev/null || true)"
    else
        endpoint_probe=0
    fi
    if [[ "${endpoint_probe}" == "1" ]]; then
        echo "canonical_endpoint_snapshot: ready"
    else
        echo "canonical_endpoint_snapshot: unavailable"
    fi
    echo "hil_pi_peer: ${PI_ADDRESS:-unknown}"
    echo "hil_workstation_source: ${WORKSTATION_ADDRESS:-unavailable}"
    px4_count="$(awk 'NF {count++} END {print count + 0}' <<<"${records}")"
    if [[ -n "${endpoint_pids}" ]]; then
        canonical_endpoint_count="$(awk 'NF {count++} END {print count + 0}' <<<"${endpoint_pids}")"
    fi

    if [[ "${endpoint_probe}" == "1" && "${canonical_endpoint_count}" == "1" ]]; then
        echo "canonical_xrce_endpoint_ownership: yes"
    else
        echo "canonical_xrce_endpoint_ownership: no"
        [[ "${endpoint_probe}" == "1" ]] || result=1
    fi
    if [[ "${px4_count}" != "0" ]]; then
        echo "canonical_px4_process_alive: yes"
        echo "canonical_px4_pid: $(awk -F '\t' '{printf "%s ", $1}' <<<"${records}")"
    else
        echo "canonical_px4_process_alive: no"
        echo "canonical_px4_pid: none"
    fi

    if session_exists "${SIM_SESSION}"; then
        echo "canonical_tmux_session: running"
        echo "hil_simulation: running"
        tmux_command list-panes -t "${SIM_SESSION}:simulation" -F 'sim_pane=#{pane_index} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}'
    else
        echo "canonical_tmux_session: stopped"
        echo "hil_simulation: stopped"
        result=1
    fi

    if [[ "${px4_count}" == "1" ]]; then
        IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline <<<"${records}"
        owned_record="$(owned_px4_record "${records}" 1 2>/dev/null || true)"
        if [[ -n "${owned_record}" && "${endpoint}" == "1" && "${executable_state}" == "present" && "${canonical_endpoint_count}" == "1" ]]; then
            echo "hil_owner_state: healthy"
        elif [[ -n "${owned_record}" && "${executable_state}" == "deleted" ]]; then
            echo "hil_owner_state: owned_executable_deleted"
            result=1
        else
            echo "hil_owner_state: orphan_or_unmanaged"
            result=1
        fi
    elif [[ "${px4_count}" -gt 1 || "${canonical_endpoint_count}" -gt 1 ]]; then
        echo "hil_owner_state: error_multiple_px4_hil_owners"
        result=1
    elif [[ "${canonical_endpoint_count}" != "0" ]]; then
        echo "hil_owner_state: error_foreign_endpoint_occupant"
        result=1
    else
        echo "hil_owner_state: stopped"
    fi
    if [[ "${px4_count}" == "1" && "${canonical_endpoint_count}" == "1" && -n "${owned_record}" && "${executable_state}" == "present" ]]; then
        echo "conflicting_px4_owners: count=0"
    else
        echo "conflicting_px4_owners: count=${px4_count} pids=$(awk -F '\t' '{printf "%s ", $1}' <<<"${records}")"
        [[ "${px4_count}" == "0" && "${canonical_endpoint_count}" == "0" ]] || result=1
    fi

    if session_exists "${ADAPTER_SESSION}"; then
        echo "hil_adapters: running"
        tmux_command list-panes -t "${ADAPTER_SESSION}:adapters" -F 'adapter_pane=#{pane_index} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}'
        if [[ "${ADAPTER_READINESS_CONFIRMED:-0}" == "1" ]] && adapter_panes_healthy || adapters_ready; then
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
    if ((result == 0)); then
        echo "hil_readiness: ready"
    else
        echo "hil_readiness: degraded"
    fi
    return "${result}"
}

px4_pane_history() {
    # -J joins wrapped lines; -S - includes the full scrollback.
    tmux_command capture-pane -p -J -t "${SIM_SESSION}:simulation.0" -S - 2>/dev/null
}

px4_shell_marker() {
    # PX4's shell answers an unknown command with "Invalid command: <name>".
    # A per-call token therefore proves a live answer; a stale prompt or
    # earlier output in the pane history can never match it.
    printf 'iii_hil_sync_%s_%s_%s' "$$" "$(date +%s%N)" "$1"
}

px4_shell_run() {
    # Usage: px4_shell_run <timeout-seconds> [command...]
    # Sends the commands between two unique markers and prints only the
    # output produced between them once PX4 has processed all of them.
    local timeout_seconds="$1" begin end history command deadline
    shift
    begin="$(px4_shell_marker begin)"
    end="$(px4_shell_marker end)"
    tmux_command send-keys -t "${SIM_SESSION}:simulation.0" "${begin}" C-m
    for command in "$@"; do
        tmux_command send-keys -t "${SIM_SESSION}:simulation.0" "${command}" C-m
    done
    tmux_command send-keys -t "${SIM_SESSION}:simulation.0" "${end}" C-m
    deadline=$((SECONDS + timeout_seconds))
    while :; do
        history="$(px4_pane_history)" || history=""
        if grep -Fq "Invalid command: ${end}" <<<"${history}"; then
            awk -v begin="Invalid command: ${begin}" -v end="Invalid command: ${end}" '
                index($0, end) { exit }
                started { print }
                index($0, begin) { started = 1 }
            ' <<<"${history}"
            return 0
        fi
        ((SECONDS < deadline)) || return 1
        sleep 0.5
    done
}

wait_for_px4_shell() {
    local deadline=$((SECONDS + PX4_SHELL_TIMEOUT_SEC)) history sync_timeout
    while ((SECONDS < deadline)); do
        if ! sim_session_healthy; then
            echo "PX4 exited before its shell became ready; see tmux session ${SIM_SESSION}." >&2
            return 1
        fi
        # Only probe once PX4 has printed a prompt, so no keystrokes reach the
        # launcher shell; readiness itself is the live answer to a marker.
        history="$(px4_pane_history)" || history=""
        sync_timeout=$((deadline - SECONDS))
        ((sync_timeout <= 5)) || sync_timeout=5
        ((sync_timeout >= 1)) || sync_timeout=1
        if grep -q 'pxh>' <<<"${history}" && px4_shell_run "${sync_timeout}" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "PX4 shell did not become ready within ${PX4_SHELL_TIMEOUT_SEC} s." >&2
    return 1
}

verify_px4_mavlink_endpoints() {
    local deadline=$((SECONDS + MAVLINK_START_TIMEOUT_SEC)) status pair
    local -a missing=()
    local -a expected=(
        "${MAVLINK_LOCAL_PORT}:${MAVLINK_REMOTE_PORT}"
        "${MAVLINK_AUDIT_LOCAL_PORT}:${MAVLINK_AUDIT_REMOTE_PORT}"
        "${MAVLINK_PARAMETER_LOCAL_PORT}:${MAVLINK_PARAMETER_REMOTE_PORT}"
        "${MAVLINK_QGC_LOCAL_PORT}:${MAVLINK_QGC_REMOTE_PORT}"
    )
    while :; do
        missing=()
        status="$(px4_shell_run "${PX4_COMMAND_TIMEOUT_SEC}" "mavlink status")" || status=""
        for pair in "${expected[@]}"; do
            grep -Fq "UDP (${pair%%:*}, remote port: ${pair#*:})" <<<"${status}" ||
                missing+=("${pair%%:*}->${pair#*:}")
        done
        ((${#missing[@]} == 0)) && return 0
        ((SECONDS < deadline)) || break
        sleep 1
    done
    echo "PX4 MAVLink endpoints did not start within ${MAVLINK_START_TIMEOUT_SEC} s; missing UDP local->remote: ${missing[*]}." >&2
    return 1
}

configure_px4_mavlink() {
    local output
    # rcS starts environment-dependent default MAVLink instances. Replace
    # them with the complete, deterministic HIL endpoint set so repeated
    # launches cannot exhaust PX4's instance limit or leave tools attached
    # to an accidental port.
    output="$(px4_shell_run "${PX4_COMMAND_TIMEOUT_SEC}" "mavlink stop-all")" || {
        echo "PX4 did not complete 'mavlink stop-all' within ${PX4_COMMAND_TIMEOUT_SEC} s." >&2
        return 1
    }
    grep -Fq "all instances stopped" <<<"${output}" || {
        echo "PX4 did not confirm 'mavlink stop-all'; refusing an unverified MAVLink endpoint set." >&2
        return 1
    }
    px4_shell_run "${PX4_COMMAND_TIMEOUT_SEC}" \
        "mavlink start -x -u ${MAVLINK_LOCAL_PORT} -o ${MAVLINK_REMOTE_PORT} -t ${PI_ADDRESS} -r 4000000 -f -m onboard" \
        "mavlink start -x -u ${MAVLINK_AUDIT_LOCAL_PORT} -o ${MAVLINK_AUDIT_REMOTE_PORT} -t ${PI_ADDRESS} -r 4000000 -f -m onboard" \
        "mavlink start -x -u ${MAVLINK_PARAMETER_LOCAL_PORT} -o ${MAVLINK_PARAMETER_REMOTE_PORT} -t 127.0.0.1 -r 4000000 -f -m onboard" \
        "mavlink start -x -u ${MAVLINK_QGC_LOCAL_PORT} -o ${MAVLINK_QGC_REMOTE_PORT} -t 127.0.0.1 -r 4000000 -f -m onboard" \
        >/dev/null || {
        echo "PX4 did not process the MAVLink start commands within ${PX4_COMMAND_TIMEOUT_SEC} s." >&2
        return 1
    }
    verify_px4_mavlink_endpoints
}

start() {
    local records endpoint_pids px4_count endpoint_count
    require_standard_link
    # ``link_probe`` resolves the mDNS endpoint, but PX4 itself needs a literal
    # IPv4 peer for every MAVLink stream.  Resolve once and retain that exact
    # address so an unset optional override cannot become an empty ``-t``.
    PI_ADDRESS="$(resolve_pi_address)"
    endpoint_pids="$(canonical_endpoint_pids)" || return 1
    records="$(canonical_px4_records "${endpoint_pids}")" || return 1
    px4_count="$(awk 'NF {count++} END {print count + 0}' <<<"${records}")"
    endpoint_count="$(awk 'NF {count++} END {print count + 0}' <<<"${endpoint_pids}")"
    if [[ "${px4_count}" != "0" || "${endpoint_count}" != "0" ]]; then
        if [[ "${px4_count}" == "1" && "${endpoint_count}" == "1" ]]; then
            local pid start_ticks endpoint executable_state cmdline existing_owner
            IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline <<<"${records}"
            existing_owner="$(owned_px4_record "${records}" 0 2>/dev/null || true)"
            if [[ -n "${existing_owner}" && "${endpoint}" == "1" && "${executable_state}" == "present" ]] && sim_session_healthy && gazebo_owner inspect >/dev/null 2>&1; then
                run_simulation_launcher >/dev/null
                # The GUI repair can wait for a world. Recheck the exact PX4
                # owner before changing the requested render mode in its
                # record; a different or stopped PX4 must never inherit it.
                local refreshed_endpoints refreshed_records refreshed_owner
                refreshed_endpoints="$(canonical_endpoint_pids)" || return 1
                refreshed_records="$(canonical_px4_records "${refreshed_endpoints}")" || return 1
                refreshed_owner="$(owned_px4_record "${refreshed_records}" 0 2>/dev/null || true)"
                [[ -n "${refreshed_owner}" && "${refreshed_owner}" == "${existing_owner}" ]] || {
                    echo "HIL PX4 ownership changed during viewer repair; refusing to update render mode." >&2
                    return 1
                }
                update_owner_render_mode || return 1
                echo "HIL simulation already running: canonical PX4 PID ${pid} owns ${PI_ADDRESS}:${XRCE_PORT}."
                ensure_adapters_ready || {
                    print_status || true
                    return 1
                }
                print_status
                return $?
            fi
        fi
        report_owner_conflict "${records}" "${endpoint_pids}"
        return 1
    fi
    require_empty_gazebo_partition || return 1
    record_pi_address
    [[ -x "${PX4_BUILD_DIR}/bin/px4" ]] || {
        echo "Cached PX4 SITL binary is missing: ${PX4_BUILD_DIR}/bin/px4" >&2
        return 1
    }
    prepare_px4_startup_script
    if session_exists "${SIM_SESSION}"; then
        env \
            III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
            III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
            III_SIM_TOOLS_PX4_BUILD_DIR="${PX4_BUILD_DIR}" \
            "${SIM_LAUNCHER}" --stop >/dev/null
    fi
    if ! session_exists "${SIM_SESSION}"; then
        local -a sim_arguments=(--no-attach)
        if [[ "${HIL_RENDERED}" == "1" ]]; then
            sim_arguments+=(--rendered)
        else
            sim_arguments+=(--headless)
        fi
        env \
            III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
            III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
            GZ_PARTITION="${GZ_PARTITION}" \
            III_SIM_TOOLS_RESET_PX4_PARAMS_ON_RECREATE=1 \
            III_SIM_TOOLS_ENSURE_ASSETS_WITH_CUSTOM_COMMAND=1 \
            III_SIM_TOOLS_GZ_IP=127.0.0.1 \
            III_SIM_TOOLS_PX4_COMMAND="$(px4_command)" \
            "${SIM_LAUNCHER}" "${sim_arguments[@]}"
        wait_for_px4_shell || return 1
        configure_px4_mavlink || return 1
    fi
    local owner_record
    owner_record="$(wait_for_single_px4_owner)" || return 1
    local owner_pid owner_start_ticks owner_endpoint owner_executable_state owner_cmdline
    IFS=$'\t' read -r owner_pid owner_start_ticks owner_endpoint owner_executable_state owner_cmdline <<<"${owner_record}"
    # Keep PX4 stoppable through the canonical owner even if the world check
    # fails. Overall readiness also requires the separate Gazebo owner record.
    write_owner_record "${owner_pid}" "${owner_start_ticks}"
    gazebo_owner claim --px4-pid "${owner_pid}" --not-before-ticks "${owner_start_ticks}" || return 1
    if ! ensure_adapters_ready; then
        print_status || true
        return 1
    fi
    print_status
}

read_stop_owner() {
    local owned_record="" px4_count
    local pid start_ticks endpoint executable_state cmdline
    PI_ADDRESS="$(resolve_pi_address 2>/dev/null || true)"
    if [[ -z "${PI_ADDRESS}" ]]; then
        echo "Unable to resolve the Pi address; refusing to stop without endpoint verification." >&2
        return 1
    fi
    STOP_ENDPOINT_PIDS="$(canonical_endpoint_pids)" || return 1
    STOP_RECORDS="$(canonical_px4_records "${STOP_ENDPOINT_PIDS}")" || return 1
    px4_count="$(awk 'NF {count++} END {print count + 0}' <<<"${STOP_RECORDS}")"
    if [[ "${px4_count}" -gt 1 ]]; then
        echo "Multiple canonical PX4 candidates found; refusing ambiguous stop." >&2
        report_owner_conflict "${STOP_RECORDS}" "${STOP_ENDPOINT_PIDS}"
        return 1
    fi

    if [[ "${px4_count}" == "1" ]]; then
        owned_record="$(owned_px4_record "${STOP_RECORDS}" 1 2>/dev/null || true)"
        if [[ -z "${owned_record}" ]]; then
            echo "Refusing to stop an unowned or ambiguously identified canonical HIL PX4." >&2
            report_owner_conflict "${STOP_RECORDS}" "${STOP_ENDPOINT_PIDS}"
            return 1
        fi
        IFS=$'\t' read -r pid start_ticks endpoint executable_state cmdline <<<"${owned_record}"
        STOP_MANAGED_PID="${pid}"
        STOP_HAVE_OWNED_INSTANCE=1
    elif owned_session_without_process; then
        STOP_HAVE_OWNED_INSTANCE=1
    elif session_exists "${SIM_SESSION}" || session_exists "${ADAPTER_SESSION}"; then
        echo "Refusing to stop HIL sessions without a matching owner record and process identity." >&2
        report_owner_conflict "${STOP_RECORDS}" "${STOP_ENDPOINT_PIDS}"
        return 1
    fi

    if [[ "${STOP_HAVE_OWNED_INSTANCE}" == "0" && -n "${STOP_ENDPOINT_PIDS}" ]]; then
        echo "Refusing workstation HIL stop with an unowned XRCE endpoint occupant." >&2
        report_owner_conflict "${STOP_RECORDS}" "${STOP_ENDPOINT_PIDS}"
        return 1
    fi

    # A recreated devcontainer can retain the shared record from the previous
    # container. Only retire it after the locked process, session, and endpoint
    # checks above prove there is no owner to stop. This also releases its
    # recorded peer so the next start resolves iii.local afresh.
    if [[ "${STOP_HAVE_OWNED_INSTANCE}" == "0" && -f "${HIL_OWNER_RECORD}" ]]; then
        [[ ! -L "${HIL_OWNER_RECORD}" ]] || {
            echo "Refusing to retire a symlinked HIL owner record." >&2
            return 1
        }
        rm -f "${HIL_OWNER_RECORD}" "${HIL_PEER_ADDRESS_FILE}"
        echo "Retired inactive workstation HIL owner record."
    fi

    report_unrelated_endpoint_occupants "${STOP_ENDPOINT_PIDS}" "${STOP_MANAGED_PID}"
}

stop() {
    local records endpoint_pids managed_pid
    # Validate and stop under one lifecycle lock so identity cannot change
    # between ownership checking and process/session termination.
    STOP_RECORDS=""
    STOP_ENDPOINT_PIDS=""
    STOP_MANAGED_PID=""
    STOP_HAVE_OWNED_INSTANCE=0
    read_stop_owner || return 1
    records="${STOP_RECORDS}"
    endpoint_pids="${STOP_ENDPOINT_PIDS}"
    managed_pid="${STOP_MANAGED_PID}"
    if [[ "${STOP_HAVE_OWNED_INSTANCE}" == "0" ]]; then
        echo "No recorded workstation HIL instance is running."
        print_status || true
        return 0
    fi

    # Ownership is established before the first mutation. From here onward,
    # every action targets the recorded tmux sessions, process group, or Gazebo
    # owner record; unrelated endpoint users remain untouched.
    if session_exists "${ADAPTER_SESSION}"; then
        tmux_command kill-session -t "${ADAPTER_SESSION}"
    fi
    if [[ -n "${managed_pid}" ]]; then
        terminate_managed_px4 "${managed_pid}" || return 1
    fi
    if session_exists "${SIM_SESSION}"; then
        env \
            III_SIM_TOOLS_SESSION="${SIM_SESSION}" \
            III_SIM_TOOLS_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            III_SIM_TOOLS_PX4_INSTANCE="${PX4_INSTANCE}" \
            III_SIM_TOOLS_PX4_BUILD_DIR="${PX4_BUILD_DIR}" \
            "${SIM_LAUNCHER}" --stop >/dev/null
    fi
    # Gazebo can outlive PX4's process group. Retain and verify its own PID
    # identity so a controlled stop also closes this simulation epoch.
    gazebo_owner stop || return 1
    rm -f "${HIL_PEER_ADDRESS_FILE}"
    rm -f "${HIL_OWNER_RECORD}"
    local remaining_records remaining_endpoints
    remaining_endpoints="$(canonical_endpoint_pids)" || return 1
    remaining_records="$(canonical_px4_records "${remaining_endpoints}")" || return 1
    if [[ -n "${remaining_records}" ]] || session_exists "${SIM_SESSION}" || session_exists "${ADAPTER_SESSION}"; then
        echo "Canonical HIL stop failed verification: recorded process or session ownership remains." >&2
        report_owner_conflict "${remaining_records}" "${remaining_endpoints}"
        return 1
    fi
    report_unrelated_endpoint_occupants "${remaining_endpoints}" ""
    print_status || true
}

case "${ACTION}" in
    start) acquire_lifecycle_lock; trap release_lifecycle_lock EXIT; start ;;
    status) print_status ;;
    stop) acquire_lifecycle_lock; trap release_lifecycle_lock EXIT; stop ;;
    battery-check) battery_check ;;
    *) usage >&2; exit 2 ;;
esac
