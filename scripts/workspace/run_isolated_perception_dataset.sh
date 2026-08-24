#!/usr/bin/env bash
set -eo pipefail

# Run the perception cohort in a transport/process namespace that does not
# overlap an operator's default ROS, Gazebo, PX4, XRCE-DDS, or MAVSDK stack.
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/home/iii/ws}"
RUN_ID="${III_DATASET_RUN_ID:-perception_dataset_20260813_28topics}"
ISOLATION_ROOT="${III_DATASET_ISOLATION_ROOT:-${WORKSPACE_ROOT}/runtime/isolated/${RUN_ID}}"

source /opt/ros/jazzy/setup.bash
source "${WORKSPACE_ROOT}/install/setup.bash"
source "${WORKSPACE_ROOT}/setup/setup_dev.bash"
set -u

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
export III_SYSTEM_PROFILE=sim
export III_MICRO_ROS_AGENT_UDP_PORT="${III_DATASET_XRCE_PORT:-18888}"
export ROS_LOG_DIR="${ISOLATION_ROOT}/logs"

export III_SIM_TOOLS_SESSION="${III_DATASET_SIM_SESSION:-iii_sim_tools_dataset28}"
export III_SIM_TOOLS_USER="${III_DATASET_USER:-iii}"
export III_SIM_TOOLS_PX4_INSTANCE="${III_DATASET_PX4_INSTANCE:-8}"
export III_SIM_TOOLS_RESET_PX4_STORAGE=0
export III_SIM_TOOLS_RESET_PX4_PARAMS_ON_RECREATE=0
export III_SIM_TOOLS_GZ_IP=127.0.0.1
export III_GAZEBO_DRONE_MODEL="${III_DATASET_GAZEBO_MODEL:-d4s_dc_drone_8}"

PX4_BIN="${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default/bin/px4"
PX4_ETC="${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default/etc"
PX4_RCS="${ISOLATION_ROOT}/px4_startup/rcS"
PX4_WORK="${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default/rootfs/${III_SIM_TOOLS_PX4_INSTANCE}"
mkdir -p "${PX4_WORK}"
export III_SIM_TOOLS_PX4_COMMAND="HEADLESS=1 ROS_DOMAIN_ID=${ROS_DOMAIN_ID} ROS_LOCALHOST_ONLY=1 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST GZ_PARTITION=${GZ_PARTITION} GZ_IP=127.0.0.1 PX4_GZ_WORLD=hca_full_pylon_setup PX4_GZ_MODEL=d4s_dc_drone PX4_SYS_AUTOSTART=4010 PX4_SIM_MODEL=gz_d4s_dc_drone PX4_UXRCE_DDS_PORT=${III_MICRO_ROS_AGENT_UDP_PORT} PX4_UXRCE_DDS_NO_NS=1 PX4_PARAM_COM_DL_LOSS_T=300 ${PX4_BIN} -s ${PX4_RCS} -i ${III_SIM_TOOLS_PX4_INSTANCE} -w ${PX4_WORK} ${PX4_ETC}"

export III_DATASET_PX4_SYSTEM_ADDRESS="${III_DATASET_PX4_SYSTEM_ADDRESS:-udpin://0.0.0.0:14548}"
export III_MAVSDK_SERVER_PORT="${III_DATASET_MAVSDK_GRPC_PORT:-50081}"
export III_DATASET_CUSTOM_OPERATION_MODE_ID="${III_DATASET_CUSTOM_OPERATION_MODE_ID:-27}"
export III_DATASET_PX4_TARGET_SYSTEM="${III_DATASET_PX4_TARGET_SYSTEM:-9}"
export PYTHONPATH="${WORKSPACE_ROOT}/tools/III-Drone-MCP:${PYTHONPATH:-}"

mkdir -p "${ISOLATION_ROOT}/runner"
# The III CLI can start a persistent installed unit, but this isolated unit is
# deliberately transient and therefore must be materialized for every cold run.
if ! systemctl is-active --quiet "${III_SYSTEMD_DAEMON_SERVICE}"; then
  sudo systemd-run \
    --unit="${III_SYSTEMD_DAEMON_SERVICE}" \
    --property=User="${III_SIM_TOOLS_USER}" \
    --property=WorkingDirectory="${WORKSPACE_ROOT}" \
    --setenv="WORKSPACE_ROOT=${WORKSPACE_ROOT}" \
    --setenv="III_DATASET_RUN_ID=${RUN_ID}" \
    --setenv="III_DATASET_ISOLATION_ROOT=${ISOLATION_ROOT}" \
    --setenv="III_DATASET_ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    --setenv="III_DATASET_GZ_PARTITION=${GZ_PARTITION}" \
    --setenv="III_DATASET_XRCE_PORT=${III_MICRO_ROS_AGENT_UDP_PORT}" \
    --setenv="III_DATASET_SYSTEMD_SERVICE=${III_SYSTEMD_DAEMON_SERVICE}" \
    --setenv="III_DATASET_SYSTEM_SESSION=${III_SYSTEM_TMUX_SESSION}" \
    "${WORKSPACE_ROOT}/scripts/workspace/run_isolated_perception_daemon.sh" >/dev/null
fi
cd "${WORKSPACE_ROOT}"
exec python3 scripts/workspace/perception_dataset_flights.py \
  --output-root "${WORKSPACE_ROOT}/datasets/perception_pipeline" \
  --run-id "${RUN_ID}" \
  --resume \
  --headless \
  "$@"
