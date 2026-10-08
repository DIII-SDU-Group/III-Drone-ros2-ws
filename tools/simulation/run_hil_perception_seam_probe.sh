#!/usr/bin/env bash
# Passive, stationary, dual-host HIL perception-seam observer.
#
# This script owns only the processes it starts and the new runtime artifact it
# creates.  It deliberately sends no PX4 vehicle command.  The sole ROS service
# call is one PL mapper START+RESET after the pre-roll.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PI_HOST="${III_HIL_PI_ENDPOINT:-${III_HIL_PI_ADDRESS:-iii.local}}"
PI_USER="${III_HIL_PI_USER:-iii}"
ROS_DOMAIN_ID="${III_HIL_ROS_DOMAIN_ID:-42}"
GZ_PARTITION="${III_HIL_GZ_PARTITION:-$(python3 "${WORKSPACE_ROOT}/scripts/workspace/hil_gazebo_owner.py" partition --workspace "${WORKSPACE_ROOT}" --instance "${III_HIL_PX4_INSTANCE:-0}")}"
WORKSTATION_ADDRESS="${III_HIL_WORKSTATION_ADDRESS:-}"
PRE_ROLL_SEC=5
OBSERVE_SEC=45
REMOTE_ROOT="${HIL_SEAM_REMOTE_ROOT:-/tmp/iii_drone}"
OPERATION_REGISTRY_HELPER="${WORKSPACE_ROOT}/scripts/workspace/hil_operation_registry.py"
REPORT_RENDERER="${WORKSPACE_ROOT}/scripts/workspace/hil_perception_seam_report.py"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--host HOST]

Runs exactly one stationary perception-seam observation when the canonical HIL
runtime is demonstrably stopped.  The probe records no flight or mission data
and calls only /perception/pl_mapper/pl_mapper_command once (start + reset).
EOF
}

while (($#)); do
    case "$1" in
        --host|--pi-host)
            (($# >= 2)) || { usage >&2; exit 2; }
            PI_HOST="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

HIL_ROUTE="$(python3 - "${PI_HOST}" "${WORKSTATION_ADDRESS}" <<'PY'
import re
import socket
import subprocess
import sys

host = sys.argv[1]
source_override = sys.argv[2]
try:
    peers = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)))
except socket.gaierror as exc:
    raise SystemExit(f"cannot resolve HIL Pi host {host!r}: {exc}")
if not peers:
    raise SystemExit(f"HIL Pi host {host!r} has no IPv4 address")
mismatched_sources = []
matching_source = False
for peer in peers:
    try:
        route = subprocess.run(["ip", "-4", "route", "get", peer], text=True, capture_output=True, check=False, timeout=1.0)
    except subprocess.TimeoutExpired:
        continue
    match = re.search(r"(?:^|\s)src\s+(\S+)", route.stdout)
    if route.returncode == 0 and match:
        route_source = match.group(1)
        if source_override and source_override != route_source:
            mismatched_sources.append(route_source)
            continue
        matching_source = True
        try:
            with socket.create_connection((peer, 22), timeout=0.75):
                print(peer, route_source)
                break
        except OSError:
            continue
else:
    if mismatched_sources and source_override and not matching_source:
        raise SystemExit(
            f"III_HIL_WORKSTATION_ADDRESS {source_override} does not match route to HIL Pi "
            f"{host!r} via {mismatched_sources[0]}"
        )
    raise SystemExit(
        f"no reachable IPv4 workstation route to HIL Pi host {host!r} on SSH TCP/22 "
        f"({', '.join(peers)})"
    )
PY
 )"
read -r PI_ADDRESS ROUTE_WORKSTATION_ADDRESS <<<"${HIL_ROUTE}"
if [[ -n "${WORKSTATION_ADDRESS}" && "${WORKSTATION_ADDRESS}" != "${ROUTE_WORKSTATION_ADDRESS}" ]]; then
    echo "III_HIL_WORKSTATION_ADDRESS ${WORKSTATION_ADDRESS} does not match route to HIL Pi ${PI_HOST} via ${ROUTE_WORKSTATION_ADDRESS}" >&2
    exit 2
fi
WORKSTATION_ADDRESS="${ROUTE_WORKSTATION_ADDRESS}"
export III_HIL_PI_ENDPOINT="${PI_HOST}" III_HIL_PI_ADDRESS="${PI_ADDRESS}"

RUN_ID="hil_wo010_perception_seam_$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_DIR="${WORKSPACE_ROOT}/runtime/${RUN_ID}"
if [[ -e "${ARTIFACT_DIR}" ]]; then
    echo "Refusing to reuse existing artifact path: ${ARTIFACT_DIR}" >&2
    exit 1
fi
mkdir -p "${ARTIFACT_DIR}" "${ARTIFACT_DIR}/topic_info" "${ARTIFACT_DIR}/logs"
chmod 700 "${ARTIFACT_DIR}"

MANIFEST_PATH="${ARTIFACT_DIR}/manifest.json"
EVENTS_PATH="${ARTIFACT_DIR}/events.jsonl"
REPORT_PATH="${ARTIFACT_DIR}/REPORT.md"
PIDS_PATH="${ARTIFACT_DIR}/pids.tsv"
FINAL_STATUS="IN_PROGRESS"
STATUS_REASON=""
HASH_FINALIZED=0
ANALYSIS_CLASSIFICATION="not-run"
: >"${EVENTS_PATH}"
: >"${PIDS_PATH}"

render_report() {
    local phase="$1"
    python3 -B "${REPORT_RENDERER}" \
        --output="${REPORT_PATH}" \
        --run-id="${RUN_ID}" \
        --status="${FINAL_STATUS}" \
        --reason="${STATUS_REASON}" \
        --classification="${ANALYSIS_CLASSIFICATION:-not-run}" \
        --phase="${phase}"
}

python3 - "${MANIFEST_PATH}" "${RUN_ID}" "${PI_HOST}" "${PI_ADDRESS}" "${WORKSTATION_ADDRESS}" "${ARTIFACT_DIR}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "schema": "hil-perception-seam-manifest/v1",
    "run_id": sys.argv[2],
    "status": "IN_PROGRESS",
    "attempt": 1,
    "max_attempts": 1,
    "pi_host": sys.argv[3],
    "pi_resolved_ipv4": sys.argv[4],
    "workstation_source_ipv4": sys.argv[5],
    "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "artifact_dir": sys.argv[6],
    "immutable_prior_artifact": "runtime/hil_wo010_perception_seam_20260922T062727Z-412317",
}, indent=2, sort_keys=True) + "\n")
PY
render_report initial

CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=${WORKSPACE_ROOT}" --format '{{.ID}}' | head -n1)"
WS_ROOT_IN_CONTAINER="/home/iii/ws"
REL_ARTIFACT="${ARTIFACT_DIR#${WORKSPACE_ROOT}/}"
WS_ARTIFACT="${WS_ROOT_IN_CONTAINER}/${REL_ARTIFACT}"
REMOTE_ARTIFACT="${REMOTE_ROOT}/${RUN_ID}"

WS_RECORD_TOPICS=(
    "/simulation/local/cable_camera/image_raw"
    "/sensor/cable_camera/image_raw"
    "/sensor/mmwave/points"
    "/simulation/ground_truth/drone/odometry"
    "/tf"
    "/tf_static"
    "/clock"
    "/rosout"
)
PI_RECORD_TOPICS=(
    "/sensor/cable_camera/image_raw"
    "/sensor/mmwave/points"
    "/perception/hough_transformer/cable_yaw_angle"
    "/perception/pl_dir_computer/status"
    "/perception/pl_dir_computer/powerline_direction_quat"
    "/perception/pl_mapper/powerline"
    "/perception/pl_mapper/points_est"
    "/perception/pl_mapper/transformed_points"
    "/perception/pl_mapper/projected_points"
    "/fmu/out/vehicle_status_v1"
    "/fmu/out/vehicle_land_detected"
    "/fmu/out/vehicle_odometry"
    "/tf"
    "/tf_static"
    "/clock"
    "/rosout"
)
RESET_BARRIER_PATH="${ARTIFACT_DIR}/reset_barrier.json"
QOS_OVERRIDE_PATH="${ARTIFACT_DIR}/rosbag_qos_overrides.yaml"
REMOTE_QOS_OVERRIDE_PATH="${REMOTE_ARTIFACT}/rosbag_qos_overrides.yaml"

cat >"${QOS_OVERRIDE_PATH}" <<'EOF'
/simulation/local/cable_camera/image_raw:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/sensor/cable_camera/image_raw:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/sensor/mmwave/points:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/hough_transformer/cable_yaw_angle:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_dir_computer/status:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_dir_computer/powerline_direction_quat:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_mapper/powerline:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_mapper/points_est:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_mapper/transformed_points:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/perception/pl_mapper/projected_points:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/fmu/out/vehicle_status_v1:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/fmu/out/vehicle_land_detected:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/fmu/out/vehicle_odometry:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/tf:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/tf_static:
  reliability: reliable
  durability: transient_local
  history: keep_last
  depth: 1
/clock:
  reliability: best_effort
  durability: volatile
  history: keep_last
  depth: 1
/rosout:
  reliability: reliable
  durability: volatile
  history: keep_last
  depth: 1
EOF

printf -v WS_RECORD_ARGS ' %q' "${WS_RECORD_TOPICS[@]}"
printf -v PI_RECORD_ARGS ' %q' "${PI_RECORD_TOPICS[@]}"

WS_PIDS=()
PI_PIDS=()
STARTED_WORKSTATION=0
STARTED_PI=0
PROBE_CLEANED=0

write_event() {
    local event_name="$1"
    local command_text="${2:-}"
    local operation_id="${3:-}"
    local path="${4:-}"
    local detail="${5:-}"
    python3 - "${EVENTS_PATH}" "${event_name}" "${command_text}" "${operation_id}" "${path}" "${detail}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

Path(sys.argv[1]).open("a", encoding="utf-8").write(json.dumps({
    "utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "event": sys.argv[2],
    "command": sys.argv[3] or None,
    "operation_id": sys.argv[4] or None,
    "path": sys.argv[5] or None,
    "detail": sys.argv[6] or None,
}, sort_keys=True) + "\n")
PY
}

update_manifest_status() {
    local status="$1"
    local reason="${2:-}"
    FINAL_STATUS="${status}"
    STATUS_REASON="${reason}"
    python3 - "${MANIFEST_PATH}" "${status}" "${reason}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = sys.argv[2]
payload["status_reason"] = sys.argv[3] or None
payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    write_event "status" "" "" "" "${status}${reason:+: ${reason}}"
}

finalize_hashes() {
    [[ "${HASH_FINALIZED}" == 1 ]] && return 0
    local pending_sums=".SHA256SUMS.pending"
    (
        cd "${ARTIFACT_DIR}"
        find . -type f ! -name SHA256SUMS.txt ! -name "${pending_sums}" -print0 |
            sort -z |
            xargs -0 sha256sum >"${pending_sums}"
        mv -f "${pending_sums}" SHA256SUMS.txt
    )
    HASH_FINALIZED=1
}

ws_ros() {
    local command_text="$1"
    [[ -n "${CONTAINER_ID}" ]] || return 1
    docker exec -u iii "${CONTAINER_ID}" bash -lc \
        "source /opt/ros/jazzy/setup.bash; source ${WS_ROOT_IN_CONTAINER}/setup/setup_dev.bash; export ROS_DOMAIN_ID=${ROS_DOMAIN_ID} ROS_LOCALHOST_ONLY=0 ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET RMW_IMPLEMENTATION=rmw_fastrtps_cpp FASTDDS_BUILTIN_TRANSPORTS=UDPv4 GZ_PARTITION=${GZ_PARTITION}; unset CYCLONEDDS_URI; ${command_text}"
}

pi_ros() {
    local command_text="$1"
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" \
        "source /opt/ros/jazzy/setup.bash; source /home/iii/ws/setup/setup_hil.bash; source /home/iii/ws/install/setup.bash; ${command_text}"
}

record_command() {
    printf '%s\n' "$*" >>"${ARTIFACT_DIR}/commands.txt"
    write_event "command" "$*"
}

pi_system_retained_operation() {
    local action="$1"
    shift
    local command_text="iii system ${action}"
    local arg quoted
    for arg in "$@"; do
        printf -v quoted '%q' "${arg}"
        command_text+=" ${quoted}"
    done

    local preview_command="${command_text} --non-interactive"
    local preview_path="${ARTIFACT_DIR}/logs/pi_system_${action}_preview.txt"
    local apply_path="${ARTIFACT_DIR}/logs/pi_system_${action}_apply.txt"
    local preview_rc operation_id applied_id
    record_command "Pi preview: ${preview_command}"
    set +e
    pi_ros "${preview_command}" >"${preview_path}" 2>&1
    preview_rc=$?
    set -e
    if (( preview_rc != 0 && preview_rc != 2 )); then
        write_event "pi_operation_preview_failed" "${preview_command}" "" "${preview_path}" "exit=${preview_rc}"
        return 1
    fi

    operation_id="$(python3 - "${preview_path}" <<'PY'
import re
import sys
from pathlib import Path

ids = set(re.findall(r"(?<![A-Za-z0-9_-])iii-[a-z0-9-]{7,63}(?![A-Za-z0-9-])",
                     Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")))
if len(ids) != 1:
    raise SystemExit(f"expected exactly one retained operation ID, found {sorted(ids)!r}")
print(next(iter(ids)))
PY
)" || return 1
    write_event "pi_operation_preview" "${preview_command}" "${operation_id}" "${preview_path}" "exit=${preview_rc}"

    local apply_command="${command_text} --operation-id ${operation_id} --confirm --non-interactive"
    record_command "Pi apply: ${apply_command}"
    set +e
    pi_ros "${apply_command}" >"${apply_path}" 2>&1
    local apply_rc=$?
    set -e
    if ((apply_rc != 0)); then
        write_event "pi_operation_apply_failed" "${apply_command}" "${operation_id}" "${apply_path}" "exit=${apply_rc}"
        return 1
    fi

    applied_id="$(python3 - "${apply_path}" "${operation_id}" <<'PY'
import re
import sys
from pathlib import Path

expected = sys.argv[2]
ids = set(re.findall(r"(?<![A-Za-z0-9_-])iii-[a-z0-9-]{7,63}(?![A-Za-z0-9-])",
                     Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")))
if ids != {expected}:
    raise SystemExit(f"confirmed operation ID mismatch: expected {expected!r}, found {sorted(ids)!r}")
print(expected)
PY
)" || return 1
    write_event "pi_operation_applied" "${apply_command}" "${applied_id}" "${apply_path}"
    printf '%s\n' "${applied_id}"
}

capture_git_state() {
    {
        printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'cwd=%s\n' "${WORKSPACE_ROOT}"
        printf 'head='; git -C "${WORKSPACE_ROOT}" rev-parse HEAD
        git -C "${WORKSPACE_ROOT}" status --short --branch
    } >"${1}"
}

capture_process_state() {
    {
        printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' '--- workstation launcher status ---'
        set +e
        "${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}" status
        printf 'launcher_status_exit=%s\n' "$?"
        set -e
        printf '%s\n' '--- container processes ---'
        docker exec "${CONTAINER_ID}" ps -eo pid,ppid,pgid,stat,args 2>&1 || true
        printf '%s\n' '--- pi processes and UDP ownership ---'
        ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" 'ps -eo pid,ppid,pgid,stat,args; printf "--- UDP ---\n"; ss -lunp 2>/dev/null || true' 2>&1 || true
    } >"${1}" 2>&1
}

refuse() {
    local reason="$1"
    update_manifest_status "REFUSED" "${reason}"
    write_event "refusal" "" "" "" "${reason}"
    {
        printf '# HIL-SEAM-%s refusal\n\n' "${RUN_ID}"
        printf 'Status: REFUSED\nReason: %s\n\n' "${reason}"
        printf 'This probe stopped at the stated safety boundary. See events.jsonl, commands.txt, process snapshots, and cleanup.txt for every action that occurred before refusal.\n'
    } >"${ARTIFACT_DIR}/REFUSAL.md"
    capture_git_state "${ARTIFACT_DIR}/git_state_refusal.txt"
    capture_process_state "${ARTIFACT_DIR}/process_state_refusal.txt"
    echo "HIL-SEAM-${RUN_ID}: refused: ${reason}" >&2
    exit 2
}

kill_local_pid() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 0
    local pgid
    pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]')"
    [[ "${pgid}" == "${pid}" ]] || return 0
    kill -INT -- "-${pid}" 2>/dev/null || true
    for _ in {1..20}; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.25
    done
    pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]')"
    [[ "${pgid}" == "${pid}" ]] && kill -TERM -- "-${pid}" 2>/dev/null || true
    for _ in {1..20}; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.25
    done
    pgid="$(ps -o pgid= -p "${pid}" 2>/dev/null | tr -d '[:space:]')"
    [[ "${pgid}" == "${pid}" ]] && kill -KILL -- "-${pid}" 2>/dev/null || true
}

kill_ws_pid() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 0
    ws_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -INT -- -${pid}" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        ws_ros "kill -0 ${pid}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    ws_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -TERM -- -${pid}" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        ws_ros "kill -0 ${pid}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    ws_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -KILL -- -${pid}" >/dev/null 2>&1 || true
}

kill_pi_pid() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 0
    pi_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -INT -- -${pid}" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        pi_ros "kill -0 ${pid}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    pi_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -TERM -- -${pid}" >/dev/null 2>&1 || true
    for _ in {1..20}; do
        pi_ros "kill -0 ${pid}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    pi_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -KILL -- -${pid}" >/dev/null 2>&1 || true
}

cleanup_probe_processes() {
    [[ "${PROBE_CLEANED}" == 1 ]] && return 0
    PROBE_CLEANED=1
    local pid
    for pid in "${WS_PIDS[@]}"; do kill_ws_pid "${pid}"; done
    for pid in "${PI_PIDS[@]}"; do kill_pi_pid "${pid}"; done
}

recorder_ws_pid_alive() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    ws_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -0 ${pid}" \
        >/dev/null 2>&1
}

recorder_pi_pid_alive() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    pi_ros "test \"\$(ps -o pgid= -p ${pid} 2>/dev/null | tr -d '[:space:]')\" = \"${pid}\" && kill -0 ${pid}" \
        >/dev/null 2>&1
}

cleanup_on_exit() {
    local rc=$?
    local workstation_cleanup_rc=0
    local pi_shutdown_rc=0
    trap - EXIT INT TERM
    set +e
    cleanup_probe_processes || true
    if [[ -n "${PI_BAG_PID:-}" ]]; then
        mkdir -p "${ARTIFACT_DIR}/pi_recording"
        scp -q -r "${PI_USER}@${PI_HOST}:${REMOTE_ARTIFACT}/." \
            "${ARTIFACT_DIR}/pi_recording/" \
            >>"${ARTIFACT_DIR}/logs/pi_artifact_copy_on_exit.log" 2>&1 || true
    fi
    if [[ "${STARTED_WORKSTATION}" == 1 ]]; then
        if "${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}" stop \
            >>"${ARTIFACT_DIR}/logs/workstation_hil_stop_on_exit.log" 2>&1; then
            workstation_cleanup_rc=0
            STARTED_WORKSTATION=0
        else
            workstation_cleanup_rc=$?
        fi
    fi
    if [[ "${STARTED_PI}" == 1 ]]; then
        if pi_system_retained_operation shutdown >"${ARTIFACT_DIR}/logs/pi_system_shutdown_on_exit.log" 2>&1; then
            pi_shutdown_rc=0
            STARTED_PI=0
        else
            pi_shutdown_rc=$?
        fi
    fi
    set +e
    if (( workstation_cleanup_rc != 0 )); then
        write_event "cleanup_failed" "launch_hil_workstation.sh stop" "" \
            "${ARTIFACT_DIR}/logs/workstation_hil_stop_on_exit.log" \
            "exit=${workstation_cleanup_rc}"
    fi
    if (( pi_shutdown_rc != 0 )); then
        write_event "cleanup_failed" "iii system shutdown" "" \
            "${ARTIFACT_DIR}/logs/pi_system_shutdown_on_exit.log" \
            "exit=${pi_shutdown_rc}"
    fi
    if [[ "${rc}" != 0 ]]; then
        printf 'probe_exit_code=%s\n' "${rc}" >>"${ARTIFACT_DIR}/cleanup.txt"
    fi
    if [[ "${FINAL_STATUS}" == IN_PROGRESS ]]; then
        if [[ "${rc}" == 0 && "${workstation_cleanup_rc}" == 0 && "${pi_shutdown_rc}" == 0 ]]; then
            update_manifest_status "SUCCESS" "probe completed" || true
        elif (( workstation_cleanup_rc != 0 || pi_shutdown_rc != 0 )); then
            update_manifest_status "FAILED" "canonical runtime cleanup failed" || true
        else
            update_manifest_status "FAILED" "probe exited with code ${rc}" || true
        fi
    fi

    render_report exit
    finalize_hashes || true
    printf 'run_id=%s\nstatus=%s\nartifact=%s\n' "${RUN_ID}" "${FINAL_STATUS}" "${ARTIFACT_DIR}" || true
    exit "${rc}"
}
trap cleanup_on_exit EXIT INT TERM

if [[ -v HIL_SEAM_PRE_ROLL_SEC || -v HIL_SEAM_OBSERVE_SEC ]]; then
    refuse "the Step-1 probe has fixed bounds: 5-second pre-roll and 45-second post-reset observation"
fi
if [[ "${HIL_SEAM_FIXTURE_CONFIRMED:-}" != "YES" ||
      "${HIL_SEAM_PROP_BATTERY_REMOVED:-}" != "YES" ]]; then
    refuse "operator must set HIL_SEAM_FIXTURE_CONFIRMED=YES and HIL_SEAM_PROP_BATTERY_REMOVED=YES before stationary non-flight observation"
fi

capture_git_state "${ARTIFACT_DIR}/git_state_pre.txt"
capture_process_state "${ARTIFACT_DIR}/process_state_pre.txt"
record_command "preflight: --host ${PI_HOST} launch_hil_workstation.sh status"
set +e
PRE_STATUS="$("${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}" status 2>&1)"
PRE_STATUS_RC=$?
set -e
printf '%s\n' "${PRE_STATUS}" >"${ARTIFACT_DIR}/launcher_status_pre.txt"
if [[ "${PRE_STATUS}" != *"canonical_px4_process_alive: no"* ||
      "${PRE_STATUS}" != *"canonical_xrce_endpoint_ownership: no"* ||
      "${PRE_STATUS}" != *"canonical_tmux_session: stopped"* ||
      "${PRE_STATUS}" != *"hil_adapters: stopped"* ]]; then
    refuse "canonical workstation HIL was not unambiguously stopped (status exit ${PRE_STATUS_RC})"
fi
if [[ -z "${CONTAINER_ID}" ]]; then
    refuse "III workspace devcontainer is not running"
fi

PI_PREFLIGHT="$(pi_ros 'printf "profile_env=%s\n" "${III_SYSTEM_PROFILE:-}"; iii system status; printf "\nports=\n"; ss -lunp 2>/dev/null || true; printf "\npx4=\n"; ps -eo pid,args | grep -E "(^|/)(px4|MicroXRCEAgent)( |$)" | grep -v grep || true' 2>&1)" || refuse "Pi preflight SSH/profile inspection failed"
printf '%s\n' "${PI_PREFLIGHT}" >"${ARTIFACT_DIR}/pi_preflight.txt"
if [[ "${PI_PREFLIGHT}" != *"profile_env=hil"* || "${PI_PREFLIGHT}" != *"Booted: False"* ]]; then
    refuse "Pi HIL profile was not demonstrably stopped or setup_hil was not active"
fi
if grep -Eq '(^|[[:space:]])(px4|MicroXRCEAgent)([[:space:]]|$)' <<<"${PI_PREFLIGHT}"; then
    refuse "Pi preflight found an existing PX4 or MicroXRCEAgent owner; physical endpoint was left untouched"
fi

REMOTE_PARENT_DIR="$(dirname "${REMOTE_ROOT}")"
printf -v REMOTE_PARENT_QUOTED '%q' "${REMOTE_PARENT_DIR}"

WS_FREE_KIB="$(df -Pk "${WORKSPACE_ROOT}" | awk 'NR == 2 {print $4}')"
PI_FREE_KIB="$(pi_ros "df -Pk ${REMOTE_PARENT_QUOTED} | awk 'NR == 2 {print \$4}'")" ||
    refuse "Pi free-space preflight failed"
printf 'workstation_free_kib=%s\npi_free_kib=%s\n' "${WS_FREE_KIB}" "${PI_FREE_KIB}" \
    >"${ARTIFACT_DIR}/disk_preflight.txt"
[[ "${WS_FREE_KIB}" =~ ^[0-9]+$ && "${PI_FREE_KIB}" =~ ^[0-9]+$ ]] ||
    refuse "free-space preflight did not return numeric KiB values"
(( WS_FREE_KIB > 1048576 && PI_FREE_KIB > 1048576 )) ||
    refuse "less than 1 GiB free space is available on a Step-1 recording host"

ACTIVE_WORK_SCAN="$(cat <<'EOS'
python3 - <<'PYTHON'
import re
import subprocess

records = subprocess.run(
    ["ps", "-eo", "pid=,args="],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()

patterns = (
    re.compile(
        r"(^|\s)" + "ros2" + r"\s+" + "bag" + r"\s+" + "record" + r"(\s|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(^|[^A-Za-z0-9_])"
        + "("
        + "mission" + "_executor"
        + "|"
        + "custom" + "_operation"
        + "|"
        + "maneuver" + "_controller"
        + "|"
        + "flight" + "_maneuver_executor"
        + ")"
        + r"([^A-Za-z0-9_]|$)",
        re.IGNORECASE,
    ),
)

for record in records:
    if any(pattern.search(record) for pattern in patterns):
        print(record)
PYTHON
EOS
)"
WS_ACTIVE_WORK="$(ws_ros "${ACTIVE_WORK_SCAN}")" ||
    refuse "workstation active-work preflight failed"
PI_ACTIVE_WORK="$(pi_ros "${ACTIVE_WORK_SCAN}")" ||
    refuse "Pi active-work preflight failed"
{
    printf '%s\n' '--- workstation active-work scan ---'
    printf '%s\n' "${WS_ACTIVE_WORK}"
    printf '%s\n' '--- pi active-work scan ---'
    printf '%s\n' "${PI_ACTIVE_WORK}"
} >"${ARTIFACT_DIR}/active_work_preflight.txt"
[[ -z "${WS_ACTIVE_WORK}" && -z "${PI_ACTIVE_WORK}" ]] ||
    refuse "active rosbag recorder, mission, custom operation, or maneuver work was found"

WS_RETAINED_OPERATIONS_PATH="${ARTIFACT_DIR}/retained_operations_workstation.json"
PI_RETAINED_OPERATIONS_PATH="${ARTIFACT_DIR}/retained_operations_pi.json"
RETAINED_OPERATIONS_PREFLIGHT_PATH="${ARTIFACT_DIR}/retained_operations_preflight.json"
WS_RETAINED_OPERATIONS_LOG="${ARTIFACT_DIR}/logs/retained_operations_workstation.log"
PI_RETAINED_OPERATIONS_LOG="${ARTIFACT_DIR}/logs/retained_operations_pi.log"

set +e
ws_ros "export WORKSPACE_DIR='${WS_ROOT_IN_CONTAINER}'; python3 -B '${WS_ROOT_IN_CONTAINER}/scripts/workspace/hil_operation_registry.py'" \
    >"${WS_RETAINED_OPERATIONS_PATH}" 2>"${WS_RETAINED_OPERATIONS_LOG}"
WS_RETAINED_OPERATIONS_RC=$?
set -e
if (( WS_RETAINED_OPERATIONS_RC != 0 && WS_RETAINED_OPERATIONS_RC != 3 )); then
    refuse "workstation retained-operation preflight failed"
fi

set +e
pi_ros "python3 -B -" <"${OPERATION_REGISTRY_HELPER}" \
    >"${PI_RETAINED_OPERATIONS_PATH}" 2>"${PI_RETAINED_OPERATIONS_LOG}"
PI_RETAINED_OPERATIONS_RC=$?
set -e
if (( PI_RETAINED_OPERATIONS_RC != 0 && PI_RETAINED_OPERATIONS_RC != 3 )); then
    refuse "Pi retained-operation preflight failed"
fi

validate_retained_operations_scan() {
    local host="$1"
    local scan_path="$2"
    local scan_rc="$3"
    python3 - "${scan_path}" "${scan_rc}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
observed_rc = int(sys.argv[2])
value = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(value, dict):
    raise SystemExit("scan output is not a JSON object")
if value.get("schema") != "hil-operation-registry-scan/v1":
    raise SystemExit("scan output has an unsupported schema")
if not isinstance(value.get("roots"), list) or not all(isinstance(item, str) for item in value["roots"]):
    raise SystemExit("scan roots are not a list of strings")
for field in ("terminal", "nonterminal"):
    entries = value.get(field)
    if not isinstance(entries, list):
        raise SystemExit(f"scan {field} is not a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"scan {field} contains a non-object entry")
        for key in ("kind", "operation_id", "status", "path", "root"):
            if not isinstance(entry.get(key), str):
                raise SystemExit(f"scan {field} entry has no string {key}")
expected_rc = 0 if not value["nonterminal"] else 3
if observed_rc != expected_rc:
    raise SystemExit(f"scan exit code {observed_rc} does not match nonterminal contents")
PY
}

validate_retained_operations_scan workstation \
    "${WS_RETAINED_OPERATIONS_PATH}" "${WS_RETAINED_OPERATIONS_RC}" ||
    refuse "workstation retained-operation preflight failed"
validate_retained_operations_scan Pi \
    "${PI_RETAINED_OPERATIONS_PATH}" "${PI_RETAINED_OPERATIONS_RC}" ||
    refuse "Pi retained-operation preflight failed"

python3 - "${WS_RETAINED_OPERATIONS_PATH}" "${PI_RETAINED_OPERATIONS_PATH}" \
    "${RETAINED_OPERATIONS_PREFLIGHT_PATH}" <<'PY'
import json
import sys
from pathlib import Path

workstation = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
pi = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
Path(sys.argv[3]).write_text(
    json.dumps(
        {
            "schema": "hil-operation-registry-preflight/v1",
            "workstation": workstation,
            "pi": pi,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

if (( WS_RETAINED_OPERATIONS_RC == 3 || PI_RETAINED_OPERATIONS_RC == 3 )); then
    refuse "another planned or running retained operation exists"
fi

if ! pi_ros "test ! -e '${REMOTE_ARTIFACT}'"; then
    refuse "remote Step-1 artifact path already exists"
fi

record_command "Pi: iii system boot --profile hil (retained preview/apply)"
# From this point, the probe owns cleanup because a failed apply may have
# partially started the canonical Pi runtime.
STARTED_PI=1
if ! pi_system_retained_operation boot --profile hil >"${ARTIFACT_DIR}/logs/pi_system_boot.log" 2>&1; then
    update_manifest_status "FAILED" "canonical Pi HIL boot failed"
    exit 1
fi

record_command "Workstation: --host ${PI_HOST} tools/simulation/launch_hil_workstation.sh start"
# From this point, the probe owns cleanup because a failed launcher invocation
# may have partially created canonical workstation processes.
STARTED_WORKSTATION=1
if ! "${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}" start >"${ARTIFACT_DIR}/logs/workstation_hil_start.log" 2>&1; then
    update_manifest_status "FAILED" "canonical workstation HIL startup failed"
    exit 1
fi

record_command "Pi: iii system start (retained preview/apply)"
if ! pi_system_retained_operation start >"${ARTIFACT_DIR}/logs/pi_system_start.log" 2>&1; then
    update_manifest_status "FAILED" "canonical Pi HIL system start failed"
    exit 1
fi
capture_process_state "${ARTIFACT_DIR}/process_state_started.txt"

WS_TOPICS="${WS_ARTIFACT}/workstation_topic_list.txt"
PI_TOPICS="${REMOTE_ARTIFACT}/pi_topic_list.txt"
record_command "Workstation: ros2 topic list -t; ros2 node list; ros2 service list -t"
ws_ros 'ros2 topic list -t' >"${ARTIFACT_DIR}/workstation_topic_list.txt" 2>&1 || true
ws_ros 'ros2 node list' >"${ARTIFACT_DIR}/workstation_node_list.txt" 2>&1 || true
ws_ros 'ros2 service list -t' >"${ARTIFACT_DIR}/workstation_service_list.txt" 2>&1 || true
record_command "Pi: ros2 topic list -t; ros2 node list; ros2 service list -t"
if ! pi_ros "mkdir -p '${REMOTE_ROOT}' && mkdir '${REMOTE_ARTIFACT}'"; then
    update_manifest_status "FAILED" "remote Step-1 artifact path already exists or could not be created"
    exit 1
fi
pi_ros "ros2 topic list -t >'${REMOTE_ARTIFACT}/pi_topic_list.txt' 2>&1 || true; ros2 node list >'${REMOTE_ARTIFACT}/pi_node_list.txt' 2>&1 || true; ros2 service list -t >'${REMOTE_ARTIFACT}/pi_service_list.txt' 2>&1 || true"

RECORD_TOPIC_UNION=()
declare -A SEEN_RECORD_TOPIC=()
for topic in "${WS_RECORD_TOPICS[@]}" "${PI_RECORD_TOPICS[@]}"; do
    if [[ -z "${SEEN_RECORD_TOPIC[${topic}]+x}" ]]; then
        SEEN_RECORD_TOPIC["${topic}"]=1
        RECORD_TOPIC_UNION+=("${topic}")
    fi
done

for topic in "${RECORD_TOPIC_UNION[@]}"; do
    safe_name="${topic#/}"
    safe_name="${safe_name//\//__}"
    record_command "Workstation: ros2 topic info -v ${topic}"
    ws_ros "ros2 topic info -v '${topic}'" >"${ARTIFACT_DIR}/topic_info/workstation_${safe_name}.txt" 2>&1 || true
    record_command "Pi: ros2 topic info -v ${topic}"
    pi_ros "ros2 topic info -v '${topic}'" >"${ARTIFACT_DIR}/topic_info/pi_${safe_name}.txt" 2>&1 || true
done

require_stationary_fixture() {
    local phase="$1"
    local status_path="${ARTIFACT_DIR}/fixture_status_${phase}.txt"
    local landed_path="${ARTIFACT_DIR}/fixture_landed_${phase}.txt"

    pi_ros 'timeout 5 ros2 topic echo --once /fmu/out/vehicle_status_v1 px4_msgs/msg/VehicleStatus' >"${status_path}" 2>&1
    pi_ros 'timeout 5 ros2 topic echo --once /fmu/out/vehicle_land_detected px4_msgs/msg/VehicleLandDetected' >"${landed_path}" 2>&1
    grep -Eiq 'arming_state:[[:space:]]*(1|ARMING_STATE_DISARMED)' "${status_path}"
    grep -Eiq 'landed:[[:space:]]*true' "${landed_path}"
}

if ! require_stationary_fixture pre; then
    refuse "stationary fixture was not positively disarmed and landed before recording"
fi

MAPPER_SERVICE_TYPE=""
for _ in {1..15}; do
    MAPPER_SERVICE_TYPE="$(pi_ros 'ros2 service type /perception/pl_mapper/pl_mapper_command' 2>/dev/null || true)"
    [[ "${MAPPER_SERVICE_TYPE}" == "iii_drone_interfaces/srv/PLMapperCommand" ]] && break
    sleep 1
done
printf '%s\n' "${MAPPER_SERVICE_TYPE}" >"${ARTIFACT_DIR}/mapper_service_type.txt"
[[ "${MAPPER_SERVICE_TYPE}" == "iii_drone_interfaces/srv/PLMapperCommand" ]] ||
    refuse "PL mapper command service did not appear with the required type"

resolve_tf_frame_parameter() {
    local parameter_name="$1"
    local value
    value="$(pi_ros "ros2 param get /perception/pl_dir_computer ${parameter_name}" 2>/dev/null |
        sed -nE 's/^String value is:[[:space:]]*(.+)$/\1/p' | tail -n1)"
    value="${value#\'}"
    value="${value%\'}"
    value="${value#\"}"
    value="${value%\"}"
    [[ "${value}" =~ ^[A-Za-z][A-Za-z0-9_/]*$ ]] || return 1
    printf '%s\n' "${value}"
}

DRONE_FRAME_ID="$(resolve_tf_frame_parameter /tf/drone_frame_id)" ||
    refuse "could not resolve /tf/drone_frame_id from /perception/pl_dir_computer"
WORLD_FRAME_ID="$(resolve_tf_frame_parameter /tf/world_frame_id)" ||
    refuse "could not resolve /tf/world_frame_id from /perception/pl_dir_computer"
[[ "${DRONE_FRAME_ID}" != "${WORLD_FRAME_ID}" ]] ||
    refuse "resolved drone_frame_id and world_frame_id must differ"

python3 - "${MANIFEST_PATH}" "${DRONE_FRAME_ID}" "${WORLD_FRAME_ID}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["tf_lookup"] = {
    "drone_frame_id": sys.argv[2],
    "world_frame_id": sys.argv[3],
    "lookup_transform_target_frame_id": sys.argv[2],
    "lookup_transform_source_frame_id": sys.argv[3],
    "tf2_echo_source_frame_id": sys.argv[3],
    "tf2_echo_target_frame_id": sys.argv[2],
    "command": ["ros2", "run", "tf2_ros", "tf2_echo", sys.argv[3], sys.argv[2]],
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
write_event "tf_frames_resolved" \
    "ros2 run tf2_ros tf2_echo ${WORLD_FRAME_ID} ${DRONE_FRAME_ID}"

scp -q "${QOS_OVERRIDE_PATH}" "${PI_USER}@${PI_HOST}:${REMOTE_QOS_OVERRIDE_PATH}"

WS_BAG_DIR="${WS_ARTIFACT}/workstation_bag"
PI_BAG_DIR="${REMOTE_ARTIFACT}/pi_bag"
WS_RECORDER_LOG="${WS_ARTIFACT}/logs/workstation_rosbag.log"
PI_RECORDER_LOG="${REMOTE_ARTIFACT}/pi_rosbag.log"
record_command "Workstation bag: declared Step-1 topic set with QoS overrides"
WS_BAG_PID="$(ws_ros "mkdir -p '${WS_ARTIFACT}'; nohup setsid bash -c 'source /opt/ros/jazzy/setup.bash; source /home/iii/ws/install/setup.bash; export ROS_DOMAIN_ID=${ROS_DOMAIN_ID} ROS_LOCALHOST_ONLY=0 ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET RMW_IMPLEMENTATION=rmw_fastrtps_cpp FASTDDS_BUILTIN_TRANSPORTS=UDPv4; exec ros2 bag record --storage mcap --qos-profile-overrides-path \"${WS_ARTIFACT}/rosbag_qos_overrides.yaml\"${WS_RECORD_ARGS} -o \"${WS_BAG_DIR}\"' >'${WS_RECORDER_LOG}' 2>&1 < /dev/null & echo \$!" | tr -d '[:space:]')"
[[ "${WS_BAG_PID}" =~ ^[0-9]+$ ]] || refuse "workstation rosbag recorder did not return a probe-owned PID"
WS_PIDS+=("${WS_BAG_PID}")
printf 'workstation\t%s\t%s\tworkstation_rosbag\n' "${WS_BAG_PID}" "${WS_BAG_PID}" >>"${PIDS_PATH}"
record_command "Pi bag: declared Step-1 topic set with QoS overrides"
PI_BAG_PID="$(pi_ros "nohup setsid bash -c 'source /opt/ros/jazzy/setup.bash; source /home/iii/ws/setup/setup_hil.bash; source /home/iii/ws/install/setup.bash; exec ros2 bag record --storage mcap --qos-profile-overrides-path \"${REMOTE_QOS_OVERRIDE_PATH}\"${PI_RECORD_ARGS} -o \"${PI_BAG_DIR}\"' >'${PI_RECORDER_LOG}' 2>&1 < /dev/null & echo \$!" | tr -d '[:space:]')"
[[ "${PI_BAG_PID}" =~ ^[0-9]+$ ]] || refuse "Pi rosbag recorder did not return a probe-owned PID"
PI_PIDS+=("${PI_BAG_PID}")
printf 'pi\t%s\t%s\tpi_rosbag\n' "${PI_BAG_PID}" "${PI_BAG_PID}" >>"${PIDS_PATH}"

PRE_ROLL_START_NS="$(python3 -c 'import time; print(time.monotonic_ns())')"
sleep 2
if ! recorder_ws_pid_alive "${WS_BAG_PID}"; then
    update_manifest_status "FAILED" "workstation rosbag recorder exited during startup; see logs/workstation_rosbag.log"
    exit 1
fi
if ! recorder_pi_pid_alive "${PI_BAG_PID}"; then
    update_manifest_status "FAILED" "Pi rosbag recorder exited during startup; see pi_recording/pi_rosbag.log after copyback"
    exit 1
fi

PRE_ROLL_NOW_NS="$(python3 -c 'import time; print(time.monotonic_ns())')"
PRE_ROLL_REMAINING_NS=$(( PRE_ROLL_SEC * 1000000000 - (PRE_ROLL_NOW_NS - PRE_ROLL_START_NS) ))
if (( PRE_ROLL_REMAINING_NS <= 0 )); then
    update_manifest_status "FAILED" "recorder liveness checks exceeded the fixed 5-second pre-roll bound"
    exit 1
fi
printf -v PRE_ROLL_REMAINING_SEC '%d.%09d' \
    $(( PRE_ROLL_REMAINING_NS / 1000000000 )) \
    $(( PRE_ROLL_REMAINING_NS % 1000000000 ))
sleep "${PRE_ROLL_REMAINING_SEC}"
record_command "Pi: exactly one ros2 service call /perception/pl_mapper/pl_mapper_command START reset=true"
set +e
pi_ros "$(cat <<'EOS'
set +e
response="$(timeout 12 ros2 service call /perception/pl_mapper/pl_mapper_command iii_drone_interfaces/srv/PLMapperCommand '{pl_mapper_cmd: {command: 0, reset: true}}' 2>&1)"
service_rc=$?
printf '%s\n' "${response}"
printf 'mapper_call_exit=%s\n' "${service_rc}"
if (( service_rc != 0 )); then
    exit 1
fi
if ! python3 -c '
import re
import sys

matches = re.findall(
    r"(?<![A-Za-z0-9_])pl_mapper_ack\s*[:=]\s*([0-9]+)(?![0-9])",
    sys.stdin.read(),
)
raise SystemExit(0 if matches == ["0"] else 1)
' <<<"${response}"; then
    exit 1
fi
printf 'pi_bag_timestamp_ns=%s\n' "$(date -u +%s%N)"
exit 0
EOS
)" >"${ARTIFACT_DIR}/mapper_start_reset.txt" 2>&1
MAPPER_CALL_RC=$?
set -e
if (( MAPPER_CALL_RC != 0 )); then
    update_manifest_status "FAILED" "PL mapper START+RESET did not return pl_mapper_ack: 0"
    exit 1
fi

PI_BARRIER_BAG_TIMESTAMP_NS="$(
    sed -nE 's/^pi_bag_timestamp_ns=([0-9]+)$/\1/p' \
        "${ARTIFACT_DIR}/mapper_start_reset.txt" | tail -n1
)"
[[ "${PI_BARRIER_BAG_TIMESTAMP_NS}" =~ ^[0-9]{16,20}$ ]] || {
    update_manifest_status "FAILED" "Pi post-reset bag-receipt timestamp was absent or invalid"
    exit 1
}

WS_BARRIER_BAG_TIMESTAMP_NS="$(date -u +%s%N)"
[[ "${WS_BARRIER_BAG_TIMESTAMP_NS}" =~ ^[0-9]{16,20}$ ]] || {
    update_manifest_status "FAILED" "workstation post-reset bag-receipt timestamp was invalid"
    exit 1
}

python3 - "${RESET_BARRIER_PATH}" "${RUN_ID}" \
    "${WS_BARRIER_BAG_TIMESTAMP_NS}" "${PI_BARRIER_BAG_TIMESTAMP_NS}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "schema": "hil-perception-seam-reset-barrier/v2",
    "run_id": sys.argv[2],
    "mapper_service": "/perception/pl_mapper/pl_mapper_command",
    "request": {"command": 0, "reset": True},
    "mapper_ack": 0,
    "workstation_bag_timestamp_ns": int(sys.argv[3]),
    "pi_bag_timestamp_ns": int(sys.argv[4]),
    "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
write_event "mapper_reset_barrier" "" "" "${RESET_BARRIER_PATH}" \
    "mapper_ack=0 workstation_bag_timestamp_ns=${WS_BARRIER_BAG_TIMESTAMP_NS} pi_bag_timestamp_ns=${PI_BARRIER_BAG_TIMESTAMP_NS}"

TF_POST_RESET_LOG="${REMOTE_ARTIFACT}/tf_lookup_post_reset.txt"
TF_PID="$(pi_ros "nohup setsid bash -c 'source /opt/ros/jazzy/setup.bash; source /home/iii/ws/setup/setup_hil.bash; source /home/iii/ws/install/setup.bash; timeout 10 ros2 run tf2_ros tf2_echo \"${WORLD_FRAME_ID}\" \"${DRONE_FRAME_ID}\"' >'${TF_POST_RESET_LOG}' 2>&1 < /dev/null & echo \$!" | tr -d '[:space:]')"
[[ "${TF_PID}" =~ ^[0-9]+$ ]] || {
    update_manifest_status "FAILED" "post-reset TF lookup did not return a probe-owned PID"
    exit 1
}
PI_PIDS+=("${TF_PID}")
printf 'pi\t%s\t%s\tpost_reset_tf_lookup\n' "${TF_PID}" "${TF_PID}" >>"${PIDS_PATH}"

sleep "${OBSERVE_SEC}"

cleanup_probe_processes
record_command "Copy Pi-owned observation artifacts back to local artifact"
mkdir -p "${ARTIFACT_DIR}/pi_recording"
scp -q -r "${PI_USER}@${PI_HOST}:${REMOTE_ARTIFACT}/." "${ARTIFACT_DIR}/pi_recording/"
capture_git_state "${ARTIFACT_DIR}/git_state_post_observation.txt"
capture_process_state "${ARTIFACT_DIR}/process_state_post_observation.txt"
if ! require_stationary_fixture post; then
    FIXTURE_REASON="post-observation fixture was not positively disarmed and landed"
else
    FIXTURE_REASON=""
fi

python3 - "${ARTIFACT_DIR}/workstation_topic_list.txt" "${ARTIFACT_DIR}/pi_recording/pi_topic_list.txt" "${ARTIFACT_DIR}/topic_manifest.json" <<'PY'
import json
import re
import sys
from pathlib import Path

def parse(path):
    result = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        match = re.match(r"^(\/\S+)\s+\[?([^\]]+)\]?\s*$", line.strip())
        if match:
            result[match.group(1)] = match.group(2)
    return result

Path(sys.argv[3]).write_text(json.dumps({"workstation": parse(sys.argv[1]), "pi": parse(sys.argv[2])}, indent=2, sort_keys=True) + "\n")
PY

TF_LOOKUP_SUCCESS=false
if grep -Eq 'Translation:|At time' "${ARTIFACT_DIR}/pi_recording/tf_lookup_post_reset.txt" 2>/dev/null &&
   ! grep -Eiq 'failed|extrapolat|lookup.*error' "${ARTIFACT_DIR}/pi_recording/tf_lookup_post_reset.txt"; then
    TF_LOOKUP_SUCCESS=true
fi

SUMMARY_PATH="${WS_ARTIFACT}/seam_summary.json"
ANALYSIS_PATH="${WS_ARTIFACT}/seam_analysis.json"
ANALYZER_ARGS="--workstation-bag '${WS_ARTIFACT}/workstation_bag' --pi-bag '${WS_ARTIFACT}/pi_recording/pi_bag' --manifest '${WS_ARTIFACT}/topic_manifest.json' --barrier-json '${WS_ARTIFACT}/reset_barrier.json' --mapper-ack-success true --tf-lookup-success ${TF_LOOKUP_SUCCESS} --run-id '${RUN_ID}'"
if [[ -n "${FIXTURE_REASON}" ]]; then
    ANALYZER_ARGS+=" --fixture-invalid-reason '${FIXTURE_REASON//\'/\'\\\'\'}'"
fi
record_command "Analyzer: ${ANALYZER_ARGS}"
if ! ws_ros "python3 ${WS_ROOT_IN_CONTAINER}/scripts/workspace/analyze_hil_perception_seam_bag.py '${SUMMARY_PATH}' ${ANALYZER_ARGS} --output '${ANALYSIS_PATH}'" >"${ARTIFACT_DIR}/logs/analyzer.log" 2>&1; then
    update_manifest_status "FAILED" "seam analyzer failed"
    exit 1
fi

if [[ "${STARTED_WORKSTATION}" == 1 ]]; then
    record_command "Workstation cleanup: exact canonical owner launch_hil_workstation.sh stop"
    if "${WORKSPACE_ROOT}/tools/simulation/launch_hil_workstation.sh" --host "${PI_HOST}" stop >"${ARTIFACT_DIR}/logs/workstation_hil_stop.log" 2>&1; then
        WORKSTATION_STOP_RC=0
        STARTED_WORKSTATION=0
    else
        WORKSTATION_STOP_RC=$?
    fi
else
    WORKSTATION_STOP_RC=0
fi
if [[ "${STARTED_PI}" == 1 ]]; then
    record_command "Pi cleanup: iii system shutdown (probe-started canonical runtime only; retained preview/apply)"
    if pi_system_retained_operation shutdown >"${ARTIFACT_DIR}/logs/pi_system_shutdown.log" 2>&1; then
        PI_SHUTDOWN_RC=0
        STARTED_PI=0
    else
        PI_SHUTDOWN_RC=$?
    fi
else
    PI_SHUTDOWN_RC=0
fi
if (( WORKSTATION_STOP_RC != 0 )); then
    write_event "cleanup_failed" "launch_hil_workstation.sh stop" "" "${ARTIFACT_DIR}/logs/workstation_hil_stop.log" "exit=${WORKSTATION_STOP_RC}"
fi
if (( PI_SHUTDOWN_RC != 0 )); then
    write_event "cleanup_failed" "iii system shutdown" "" "${ARTIFACT_DIR}/logs/pi_system_shutdown.log" "exit=${PI_SHUTDOWN_RC}"
fi
printf 'workstation_cleanup=exact_canonical_owner exit=%s\npi_cleanup=probe_started_canonical_runtime exit=%s\n' \
    "${WORKSTATION_STOP_RC}" "${PI_SHUTDOWN_RC}" >"${ARTIFACT_DIR}/cleanup.txt"
capture_process_state "${ARTIFACT_DIR}/process_state_final.txt"

ARTIFACT_ACCEPTANCE_PATH="${ARTIFACT_DIR}/artifact_acceptance.json"
set +e
python3 - \
    "${ARTIFACT_DIR}/seam_summary.json" \
    "${ARTIFACT_DIR}/seam_analysis.json" \
    "${ARTIFACT_DIR}/workstation_bag/metadata.yaml" \
    "${ARTIFACT_DIR}/pi_recording/pi_bag/metadata.yaml" \
    "${RUN_ID}" \
    "${ARTIFACT_ACCEPTANCE_PATH}" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
analysis_path = Path(sys.argv[2])
workstation_metadata_path = Path(sys.argv[3])
pi_metadata_path = Path(sys.argv[4])
run_id = sys.argv[5]
output_path = Path(sys.argv[6])
terminal_classifications = {
    "H1 source stall",
    "H2 cross-host relay loss",
    "H3 camera present but no Hough output",
    "H4 Hough present but no direction/TF",
    "H5 direction+mmWave present but mapper cannot form/advance",
    "positive seam",
    "inconclusive",
}
errors = []

for label, path in (
    ("workstation rosbag metadata", workstation_metadata_path),
    ("Pi rosbag metadata", pi_metadata_path),
):
    if not path.is_file():
        errors.append(f"{label} is missing")

def read_object(label, path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{label} is unreadable: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} is not a JSON object")
        return None
    return value

summary = read_object("seam_summary.json", summary_path)
analysis = read_object("seam_analysis.json", analysis_path)
classification = "not-run"

if summary is not None:
    if summary.get("schema") != "hil-perception-seam-summary/v1":
        errors.append("seam_summary.json schema is invalid")
    if summary.get("run_id") != run_id:
        errors.append("seam_summary.json run_id does not match this run")
    if summary.get("recording_complete") is not True:
        errors.append("seam_summary.json recording_complete is not true")
    if summary.get("fixture_valid") is not True:
        errors.append("seam_summary.json fixture_valid is not true")

if analysis is not None:
    if analysis.get("schema") != "hil-perception-seam-analysis/v1":
        errors.append("seam_analysis.json schema is invalid")
    if analysis.get("run_id") != run_id:
        errors.append("seam_analysis.json run_id does not match this run")
    value = analysis.get("classification")
    if isinstance(value, str):
        classification = value
    if classification not in terminal_classifications:
        errors.append(
            "seam_analysis.json classification is not an accepted Step-1 terminal classification"
        )

result = {
    "schema": "hil-perception-seam-artifact-acceptance/v1",
    "run_id": run_id,
    "accepted": not errors,
    "classification": classification,
    "errors": errors,
}
output_path.write_text(
    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
raise SystemExit(0 if result["accepted"] else 1)
PY
ARTIFACT_ACCEPTANCE_RC=$?
set -e
ANALYSIS_CLASSIFICATION="$(
    python3 -c '
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get("classification")
print(value if isinstance(value, str) else "not-run")
' "${ARTIFACT_ACCEPTANCE_PATH}" 2>/dev/null || printf '%s\n' 'not-run'
)"
if (( WORKSTATION_STOP_RC != 0 || PI_SHUTDOWN_RC != 0 )); then
    update_manifest_status "FAILED" "canonical runtime cleanup failed"
fi
if [[ -n "${FIXTURE_REASON}" ]]; then
    update_manifest_status "FAILED" "${FIXTURE_REASON}"
fi
if (( ARTIFACT_ACCEPTANCE_RC != 0 )); then
    update_manifest_status "FAILED" "Step-1 artifact acceptance criteria were not satisfied"
fi

if [[ "${FINAL_STATUS}" == "IN_PROGRESS" ]]; then
    update_manifest_status "SUCCESS" "probe completed"
fi

render_report final

finalize_hashes
printf 'run_id=%s\nclassification=%s\nartifact=%s\n' "${RUN_ID}" "${ANALYSIS_CLASSIFICATION}" "${ARTIFACT_DIR}"
trap - EXIT INT TERM
if [[ "${FINAL_STATUS}" == "SUCCESS" ]]; then
    exit 0
fi
exit 1
