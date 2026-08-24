#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$WORKSPACE_DIR/src/III-Drone-GC/docker-compose.prod.yml"
ENV_FILE="${III_GC_ENV_FILE:-$HOME/.config/iii-ground-control.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi
PROJECT_NAME="${III_GC_COMPOSE_PROJECT:-iii-ground-control}"
LOG_DIR="${III_GC_LOG_DIR:-$WORKSPACE_DIR/runtime_logs/ground-control}"
COMMAND="${1:-help}"
DRY_RUN=0

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
  local label="$1" url="$2"
  for _attempt in $(seq 1 30); do
    curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "Ground-control startup failed: $label did not become reachable at $url." >&2
  capture_logs >&2
  exit 3
}

start() {
  validate_field_identity
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "Would start production ground control with project $PROJECT_NAME"
    echo "Compose file: $COMPOSE_FILE"
    echo "Environment file: $ENV_FILE"
    echo "Logs: $LOG_DIR"
    return
  fi
  require_tools
  compose config --quiet
  compose up -d --build --remove-orphans
  local proxy_port frontend_port
  proxy_port="${III_GC_PROXY_PORT:-8780}"
  frontend_port="${III_GC_FRONTEND_PORT:-5173}"
  wait_for_url "GC proxy" "http://127.0.0.1:$proxy_port/health"
  wait_for_url "operator interface" "http://127.0.0.1:$frontend_port/"
  echo "Ground control ready: http://127.0.0.1:$frontend_port"
  echo "Select and positively confirm the expected aircraft before login."
}

case "$COMMAND" in
  start)
    start
    ;;
  stop)
    require_tools
    capture_logs
    compose down --remove-orphans
    ;;
  restart|recover)
    require_tools
    capture_logs
    compose down --remove-orphans
    start
    ;;
  status)
    require_tools
    compose ps
    ;;
  logs)
    require_tools
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
