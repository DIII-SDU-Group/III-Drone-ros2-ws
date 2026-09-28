#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="${III_GC_WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
COMPOSE_FILE="$WORKSPACE_DIR/src/III-Drone-GC/docker-compose.prod.yml"
ENV_FILE="${III_GC_ENV_FILE:-$HOME/.config/iii-ground-control.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi
PROJECT_NAME="${III_GC_COMPOSE_PROJECT:-iii-ground-control}"
LOG_DIR="${III_GC_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/iii/ground-control}"
COMMAND="${1:-help}"
DRY_RUN=0
GC_LOCK_DIR="${III_GC_LOCK_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iii-ground-control}"
GC_LOCK_PATH="${III_GC_LOCK_PATH:-${GC_LOCK_DIR}/${PROJECT_NAME//[^A-Za-z0-9_.-]/_}.lock}"
GC_LOCK_ACQUIRED=0

if [[ "${2:-}" == "--dry-run" || "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  [[ "$COMMAND" == "--dry-run" ]] && COMMAND="start"
fi

compose() {
  local args=(-p "$PROJECT_NAME" -f "$COMPOSE_FILE")
  [[ -f "$ENV_FILE" ]] && args+=(--env-file "$ENV_FILE")
  docker compose "${args[@]}" "$@"
}

require_tools() {
  command -v docker >/dev/null || { echo "Ground-control startup failed: Docker is not installed." >&2; exit 2; }
  docker compose version >/dev/null 2>&1 || { echo "Ground-control startup failed: Docker Compose is unavailable." >&2; exit 2; }
  command -v curl >/dev/null || { echo "Ground-control startup failed: curl is not installed." >&2; exit 2; }
}

validate_field_identity() {
  [[ "${III_GC_EXPECTED_PROFILE:-}" == "real" ]] || return 0
  if [[ -z "${III_GC_EXPECTED_RUNTIME_ID:-}" || -z "${III_GC_EXPECTED_SYSTEM_ID:-}" ]]; then
    echo "Ground-control startup failed: real profile requires III_GC_EXPECTED_RUNTIME_ID and III_GC_EXPECTED_SYSTEM_ID." >&2
    exit 2
  fi
}

capture_logs() {
  mkdir -p "$LOG_DIR"
  local stamp output
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output="$LOG_DIR/ground-control-$stamp.log"
  compose logs --no-color >"$output" 2>&1 || true
  echo "Ground-control logs: $output"
}

wait_for_url() {
  shift
  local url="$1"
  for _attempt in $(seq 1 30); do
    curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

endpoint_healthy() {
  local url="$1"
  curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1
}

project_has_containers() {
  [[ -n "$(compose ps --all --quiet)" ]]
}

assert_project_ownership() {
  local ids id config_file checkout_file
  if ! ids="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT_NAME}" --format '{{.ID}}')"; then
    echo "Ground-control startup failed: could not inspect Compose project ownership." >&2
    return 2
  fi
  for id in $ids; do
    if ! config_file="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$id")"; then
      echo "Ground-control startup failed: could not inspect container $id." >&2
      return 2
    fi
    if [[ "$config_file" != "$COMPOSE_FILE" ]]; then
      checkout_file=""
      if [[ -f "$WORKSPACE_DIR/../install.json" ]]; then
        checkout_file="$(python3 -c 'import json,sys; from pathlib import Path; data=json.load(open(sys.argv[1])); checkout=data.get("checkout",{}).get("checkout",""); print(str(Path(checkout)/"src/III-Drone-GC/docker-compose.prod.yml") if checkout else "")' "$WORKSPACE_DIR/../install.json")" || return 2
      fi
      if [[ "$config_file" != "$checkout_file" ]]; then
        echo "Ground-control project $PROJECT_NAME belongs to another Compose checkout ($config_file); refusing to manage it." >&2
        return 3
      fi
    fi
  done
}

project_has_all_running_services() {
  local configured_services running_services
  configured_services="$(compose config --services)" || return 1
  running_services="$(compose ps --services --status running)" || return 1
  [[ -n "${configured_services}" && -n "${running_services}" ]] || return 1
  diff -u \
    <(printf '%s\n' "${configured_services}" | sort -u) \
    <(printf '%s\n' "${running_services}" | sort -u) \
    >/dev/null
}

acquire_mutation_lock() {
  [[ "${III_GC_LOCK_HELD:-0}" == "1" ]] && return 0
  command -v flock >/dev/null || { echo "Ground-control startup failed: flock is not installed." >&2; return 2; }
  mkdir -p "$(dirname "$GC_LOCK_PATH")"
  exec 8>"$GC_LOCK_PATH"
  flock -x 8
  GC_LOCK_ACQUIRED=1
}

release_mutation_lock() {
  if ((GC_LOCK_ACQUIRED)); then
    flock -u 8 2>/dev/null || true
    exec 8>&-
    GC_LOCK_ACQUIRED=0
  fi
}

start_unlocked() {
  validate_field_identity
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "Would start production ground control with project $PROJECT_NAME"
    echo "Compose file: $COMPOSE_FILE"
    echo "Environment file: $ENV_FILE"
    echo "Logs: $LOG_DIR"
    return
  fi
  local compose_result
  if require_tools; then
    :
  else
    compose_result=$?
    return "${compose_result}"
  fi
  assert_project_ownership || return $?
  if compose config --quiet; then
    :
  else
    compose_result=$?
    echo "Ground-control startup failed: Compose configuration validation failed." >&2
    return "${compose_result}"
  fi
  local proxy_port frontend_port proxy_url frontend_url
  proxy_port="${III_GC_PROXY_PORT:-8780}"
  frontend_port="${III_GC_FRONTEND_PORT:-5173}"
  proxy_url="http://127.0.0.1:$proxy_port/health"
  frontend_url="http://127.0.0.1:$frontend_port/"
  if project_has_all_running_services && endpoint_healthy "$proxy_url" && endpoint_healthy "$frontend_url"; then
    echo "Ground control already ready: http://127.0.0.1:$frontend_port"
    return 0
  fi
  if compose up -d --build --remove-orphans; then
    :
  else
    compose_result=$?
    echo "Ground-control startup failed: Compose project startup failed." >&2
    capture_logs >&2
    return "${compose_result}"
  fi
  if project_has_all_running_services; then
    :
  else
    echo "Ground-control startup failed: the exact Compose project did not reach the configured running-service set." >&2
    capture_logs >&2
    return 3
  fi
  local proxy_pid frontend_pid proxy_rc frontend_rc
  wait_for_url "GC proxy" "$proxy_url" & proxy_pid=$!
  wait_for_url "operator interface" "$frontend_url" & frontend_pid=$!
  set +e
  wait "$proxy_pid"
  proxy_rc=$?
  wait "$frontend_pid"
  frontend_rc=$?
  set -e
  if ((proxy_rc != 0 || frontend_rc != 0)); then
    local -a failed=()
    ((proxy_rc == 0)) || failed+=("GC proxy")
    ((frontend_rc == 0)) || failed+=("operator interface")
    echo "Ground-control startup failed: ${failed[*]} did not become reachable." >&2
    capture_logs >&2
    return 3
  fi
  echo "Ground control ready: http://127.0.0.1:$frontend_port"
  echo "Select and positively confirm the expected aircraft before login."
}

start() {
  acquire_mutation_lock || return
  local result=0
  set +e
  start_unlocked
  result=$?
  set -e
  release_mutation_lock
  return "$result"
}

stop_unlocked() {
  require_tools
  assert_project_ownership || return $?
  capture_logs
  compose down --remove-orphans
}

stop() {
  acquire_mutation_lock || return
  local result=0
  set +e
  stop_unlocked
  result=$?
  set -e
  release_mutation_lock
  return "$result"
}

restart() {
  acquire_mutation_lock || return
  local result=0
  set +e
  require_tools || result=$?
  if ((result == 0)); then assert_project_ownership || result=$?; fi
  if ((result == 0)); then capture_logs || result=$?; fi
  if ((result == 0)); then compose down --remove-orphans || result=$?; fi
  if ((result == 0)); then start_unlocked || result=$?; fi
  set -e
  release_mutation_lock
  return "$result"
}

case "$COMMAND" in
  start)
    start
    ;;
  stop)
    stop
    ;;
  restart|recover)
    restart
    ;;
  status)
    require_tools
    assert_project_ownership
    compose ps
    ;;
  owned)
    require_tools
    assert_project_ownership
    project_has_containers
    ;;
  logs)
    require_tools
    assert_project_ownership
    compose logs --no-color "${@:2}"
    ;;
  help|-h|--help)
    echo "Usage: $(basename "$0") {start|stop|restart|recover|status|logs} [--dry-run]"
    echo "Configuration: III_GC_ENV_FILE (default $ENV_FILE)"
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    exit 2
    ;;
esac
