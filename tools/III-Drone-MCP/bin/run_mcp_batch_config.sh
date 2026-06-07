#!/usr/bin/env bash

set -eo pipefail

WORKSPACE_ROOT="${III_DRONE_WORKSPACE_ROOT:-/home/iii/ws}"
HOST_WORKSPACE_ROOT="${III_DRONE_HOST_WORKSPACE_ROOT:-$(pwd)}"
DEFAULT_CONFIG="tools/III-Drone-MCP/config/full_mission_rendered_reach_cable.json"
PYTHON_CACHE_ROOT="${III_DRONE_PYTHON_CACHE_ROOT:-/tmp/iii_drone/pycache}"

CONFIG_PATH="${1:-$DEFAULT_CONFIG}"
if [[ $# -gt 0 && "${1}" != -* ]]; then
    shift
else
    CONFIG_PATH="$DEFAULT_CONFIG"
fi

if [[ "${III_DRONE_MCP_BATCH_IN_CONTAINER:-0}" != "1" ]] && command -v docker >/dev/null 2>&1; then
    CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=${HOST_WORKSPACE_ROOT}" --format '{{.ID}}' | head -n1)"
    if [[ -n "${CONTAINER_ID}" ]]; then
        CONFIG_FOR_CONTAINER="${CONFIG_PATH}"
        if [[ "${CONFIG_PATH}" = /* ]]; then
            HOST_WORKSPACE_REAL="$(realpath "${HOST_WORKSPACE_ROOT}" 2>/dev/null || echo "${HOST_WORKSPACE_ROOT}")"
            CONFIG_REAL="$(realpath "${CONFIG_PATH}" 2>/dev/null || echo "${CONFIG_PATH}")"
            if [[ "${CONFIG_REAL}" == "${HOST_WORKSPACE_REAL}"/* ]]; then
                CONFIG_FOR_CONTAINER="${CONFIG_REAL#${HOST_WORKSPACE_REAL}/}"
            fi
        fi
        exec docker exec -u iii \
            -e III_DRONE_MCP_BATCH_IN_CONTAINER=1 \
            -e III_DRONE_WORKSPACE_ROOT="${WORKSPACE_ROOT}" \
            -e III_DRONE_HOST_WORKSPACE_ROOT="${HOST_WORKSPACE_ROOT}" \
            -e III_DRONE_MCP_CONFIG_PATH="${CONFIG_FOR_CONTAINER}" \
            -e III_DRONE_MCP_ARTIFACT_ROOT="${III_DRONE_MCP_ARTIFACT_ROOT:-/tmp/iii_drone}" \
            -e PYTHONPYCACHEPREFIX="${PYTHON_CACHE_ROOT}" \
            -e PYTHONDONTWRITEBYTECODE=1 \
            "${CONTAINER_ID}" \
            bash -lc 'cd "${III_DRONE_WORKSPACE_ROOT}" && tools/III-Drone-MCP/bin/run_mcp_batch_config.sh "${III_DRONE_MCP_CONFIG_PATH}" "$@"' \
            run_mcp_batch_config.sh \
            "$@"
    fi
fi

if [[ "${CONFIG_PATH}" = /* ]]; then
    CONFIG_IN_CONTAINER="${CONFIG_PATH}"
else
    CONFIG_IN_CONTAINER="${WORKSPACE_ROOT}/${CONFIG_PATH}"
fi

if [[ ! -f "${CONFIG_IN_CONTAINER}" ]]; then
    echo "Batch config not found: ${CONFIG_IN_CONTAINER}" >&2
    exit 2
fi

SCENARIO_NAME="$(basename "${CONFIG_IN_CONTAINER}" .json)"
ARTIFACT_ROOT="${III_DRONE_MCP_ARTIFACT_ROOT:-/tmp/iii_drone}"
ARTIFACT_DIR="${III_DRONE_MCP_ARTIFACT_DIR:-${ARTIFACT_ROOT}/${SCENARIO_NAME}_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "${ARTIFACT_DIR}"

# ROS setup files are not nounset-safe, so this script intentionally avoids
# `set -u` and sources them in the same permissive style as the other MCP runners.
source /opt/ros/jazzy/setup.bash 2>/dev/null || true
source "${WORKSPACE_ROOT}/install/setup.bash" 2>/dev/null || true
source "${WORKSPACE_ROOT}/setup/setup_dev.bash" 2>/dev/null || true
export PYTHONPATH="${WORKSPACE_ROOT}/tools/III-Drone-MCP:${PYTHONPATH:-}"
export PYTHONPYCACHEPREFIX="${PYTHON_CACHE_ROOT}"
export PYTHONDONTWRITEBYTECODE=1

MCP_ARGS=("$@")
if [[ "${III_DRONE_MCP_BATCH_DEFAULT_STRICT:-1}" == "1" ]]; then
    if [[ " ${MCP_ARGS[*]} " != *" --strict-safety "* ]]; then
        MCP_ARGS+=("--strict-safety")
    fi
    if [[ " ${MCP_ARGS[*]} " != *" --always-run-cleanup "* ]]; then
        MCP_ARGS+=("--always-run-cleanup")
    fi
fi

set +e
python3 -m iii_drone_mcp.mcp_batch \
    "${CONFIG_IN_CONTAINER}" \
    --artifact-dir "${ARTIFACT_DIR}" \
    "${MCP_ARGS[@]}" \
    > "${ARTIFACT_DIR}/batch_output.json"
EXIT_CODE=$?
set -e

echo "ARTIFACT_DIR=${ARTIFACT_DIR}"
echo "EXIT_CODE=${EXIT_CODE}"
exit "${EXIT_CODE}"
