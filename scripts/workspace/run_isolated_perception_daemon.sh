#!/usr/bin/env bash
set -eo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/home/iii/ws}"
RUN_ID="${III_DATASET_RUN_ID:-perception_dataset_20260813_28topics}"
ISOLATION_ROOT="${III_DATASET_ISOLATION_ROOT:-${WORKSPACE_ROOT}/runtime/isolated/${RUN_ID}}"

source "${WORKSPACE_ROOT}/setup/setup_dev.bash"
export HOME="${III_DATASET_HOME:-/home/iii}"
export ROS_DOMAIN_ID="${III_DATASET_ROS_DOMAIN_ID:-74}"
export ROS_LOCALHOST_ONLY=1
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export GZ_PARTITION="${III_DATASET_GZ_PARTITION:-iii_dataset_28topics_20260813}"
export GZ_IP=127.0.0.1
export CONFIG_BASE_DIR="${ISOLATION_ROOT}/config"
export NODE_MANAGEMENT_CONFIG_DIR="${WORKSPACE_ROOT}/src/III-Drone-Supervision/node_management_config"
export MISSION_SPECIFICATION_DIR="${WORKSPACE_ROOT}/src/III-Drone-Mission/mission_specification"
export BEHAVIOR_TREES_DIR="${WORKSPACE_ROOT}/src/III-Drone-Mission/behavior_trees"
export III_SYSTEM_RUNTIME_DIR="${ISOLATION_ROOT}/system"
export III_SYSTEM_DAEMON_SOCKET="${III_SYSTEM_RUNTIME_DIR}/system_manager.sock"
export III_SYSTEM_DAEMON_LOG="${III_SYSTEM_RUNTIME_DIR}/system_manager.log"
export III_SYSTEMD_DAEMON_SERVICE="${III_DATASET_SYSTEMD_SERVICE:-iii-system-daemon-dataset28.service}"
export III_SYSTEM_TMUX_SESSION="${III_DATASET_SYSTEM_SESSION:-iii_sim_dataset28}"
export III_MICRO_ROS_AGENT_UDP_PORT="${III_DATASET_XRCE_PORT:-18888}"
export ROS_LOG_DIR="${ISOLATION_ROOT}/logs"

mkdir -p "${III_SYSTEM_RUNTIME_DIR}" "${ROS_LOG_DIR}"
exec python3 -m iii_drone_supervision.system_daemon --socket "${III_SYSTEM_DAEMON_SOCKET}"
