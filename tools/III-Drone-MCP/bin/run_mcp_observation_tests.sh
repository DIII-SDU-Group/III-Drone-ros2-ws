#!/usr/bin/env bash

set -euo pipefail

WORKSPACE_ROOT="${III_DRONE_WORKSPACE_ROOT:-/home/iii/ws}"
HOST_WORKSPACE_ROOT="${III_DRONE_HOST_WORKSPACE_ROOT:-$(pwd)}"
ARTIFACT_ROOT="${III_DRONE_MCP_OBSERVATION_TEST_ARTIFACT_ROOT:-/tmp/iii_drone/mcp_observation_suite/$(date +%Y%m%d_%H%M%S)}"
PYTHON_CACHE_ROOT="${III_DRONE_PYTHON_CACHE_ROOT:-/tmp/iii_drone/pycache}"

if [[ "${III_DRONE_MCP_OBSERVATION_TEST_IN_CONTAINER:-0}" != "1" ]] && command -v docker >/dev/null 2>&1; then
    CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=${HOST_WORKSPACE_ROOT}" --format '{{.ID}}' | head -n1)"
    if [[ -n "${CONTAINER_ID}" ]]; then
        exec docker exec -u iii \
            -e III_DRONE_MCP_OBSERVATION_TEST_IN_CONTAINER=1 \
            -e III_DRONE_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            -e III_DRONE_MCP_OBSERVATION_TEST_ARTIFACT_ROOT="${ARTIFACT_ROOT}" \
            -e PYTHONPYCACHEPREFIX="${PYTHON_CACHE_ROOT}" \
            -e PYTHONDONTWRITEBYTECODE=1 \
            "${CONTAINER_ID}" \
            bash -lc 'source /opt/ros/jazzy/setup.bash && source "${III_DRONE_WORKSPACE_ROOT}/install/setup.bash" 2>/dev/null || true; source "${III_DRONE_WORKSPACE_ROOT}/setup/setup_dev.bash" 2>/dev/null || true; export PYTHONPATH="${III_DRONE_WORKSPACE_ROOT}/tools/III-Drone-MCP:${PYTHONPATH:-}"; python3 -m iii_drone_mcp.observation_test_suite --workspace-root "${III_DRONE_WORKSPACE_ROOT}" --artifact-root "${III_DRONE_MCP_OBSERVATION_TEST_ARTIFACT_ROOT}" "$@"' \
            run_mcp_observation_tests.sh \
            "$@"
    fi
fi

source /opt/ros/jazzy/setup.bash 2>/dev/null || true
source "${WORKSPACE_ROOT}/install/setup.bash" 2>/dev/null || true
source "${WORKSPACE_ROOT}/setup/setup_dev.bash" 2>/dev/null || true
export PYTHONPATH="${WORKSPACE_ROOT}/tools/III-Drone-MCP:${PYTHONPATH:-}"
export PYTHONPYCACHEPREFIX="${PYTHON_CACHE_ROOT}"
export PYTHONDONTWRITEBYTECODE=1

python3 -m iii_drone_mcp.observation_test_suite \
    --workspace-root "${WORKSPACE_ROOT}" \
    --artifact-root "${ARTIFACT_ROOT}" \
    "$@"
