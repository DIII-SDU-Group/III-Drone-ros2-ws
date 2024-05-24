#!/bin/bash

set -e

SCRIPT_DIR=$(dirname $0)
WORKSPACE_DIR=$(cd $SCRIPT_DIR/..; pwd)

# Show help function:
show_help() {
    echo "Usage: $0 [--all] [--system] [compose_file [compose_file ...]]"
    echo "Build docker-compose files"
    echo "Options:"
    echo "  --all: build all non-system compose files"
    echo "  --system: build all system compose files"
    echo "  [compose_file [compose_file ...]]: build specified compose files"
}

# Parse args
# --help: show help message
# --all: build all compose files
# --system: build all system compose files
# [compose_file [compose_file ...]]: build specified compose files
if [ "$1" == "--help" ]; then
# Show detailed help message
    show_help
    exit 0
elif [ "$1" == "--all" ]; then
    COMPOSE_FILES=$(ls $WORKSPACE_DIR/docker/docker-compose*.yml)
elif [ "$1" == "--system" ]; then
    COMPOSE_FILES=$(ls $WORKSPACE_DIR/docker/system/docker-compose*.yml)
else
    COMPOSE_FILES=$@
fi

# If no compose file is specified, show help message
if [ -z "$COMPOSE_FILES" ]; then
    show_help
    exit 1
fi

# Build docker-compose files
compose_args=""
for compose_file in $COMPOSE_FILES; do
    compose_args="$compose_args -f $compose_file"
done

docker compose $compose_args build