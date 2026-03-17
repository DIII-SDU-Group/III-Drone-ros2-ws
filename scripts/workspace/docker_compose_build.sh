#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

show_help() {
    cat <<'USAGE'
NAME
  docker_compose_build.sh - build one or more workspace docker compose targets

SYNOPSIS
  scripts/workspace/docker_compose_build.sh --all
  scripts/workspace/docker_compose_build.sh --system
  scripts/workspace/docker_compose_build.sh <compose-file> [<compose-file> ...]

DESCRIPTION
  Convenience wrapper for `docker compose ... build`.

  Modes:
  - `--all`: build all compose files under `docker/`
  - `--system`: build all compose files under `docker/system/` and include the
    base compose file
  - explicit file list: build only the provided compose files

EXAMPLES
  scripts/workspace/docker_compose_build.sh --all
  scripts/workspace/docker_compose_build.sh docker/docker-compose.dev.yml
USAGE
}

# Parse args
# --help: show help message
# --all: build all compose files
# --system: build all system compose files
# [compose_file [compose_file ...]]: build specified compose files
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
# Show detailed help message
    show_help
    exit 0
elif [[ "${1:-}" == "--all" ]]; then
    COMPOSE_FILES=$(ls $WORKSPACE_DIR/docker/docker-compose*.yml)
elif [[ "${1:-}" == "--system" ]]; then
    COMPOSE_FILES=$(ls $WORKSPACE_DIR/docker/system/docker-compose*.yml)
    # Append $WORKSPACE_DIR/docker/docker-compose.base.yml to COMPOSE_FILES with new line separator
    # COMPOSE_FILES="$COMPOSE_FILES"$'\n'"$WORKSPACE_DIR/docker/docker-compose.base.yml"
else
    COMPOSE_FILES=$@
fi

echo "COMPOSE_FILES: $COMPOSE_FILES"

# If no compose file is specified, show help message
if [[ -z "${COMPOSE_FILES:-}" ]]; then
    show_help
    exit 1
fi

# Build docker-compose files
compose_args=""
for compose_file in $COMPOSE_FILES; do
    compose_args="$compose_args -f $compose_file"
done

# If --system, append -f $WORKSPACE_DIR/docker/docker-compose.base.yml to compose_args
if [[ "${1:-}" == "--system" ]]; then
    compose_args="$compose_args -f $WORKSPACE_DIR/docker/docker-compose.base.yml"
fi

echo "Building docker-compose files: $compose_args"

docker compose $compose_args build
