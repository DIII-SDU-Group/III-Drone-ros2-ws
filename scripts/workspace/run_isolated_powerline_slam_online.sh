#!/usr/bin/env bash
set -eo pipefail

# Fly the powerline SLAM corridor flights with the powerline_slam backend
# consuming the live streams (powerline_slam_online_flights.py): the isolated
# launcher of run_isolated_powerline_slam_flights.sh with the canonical system
# booted on /perception/processing_stack powerline_slam and the backend's
# runtime configuration III_POWERLINE_RUNTIME_CONFIG (required). Nothing is
# recorded. Original description:
#
# Fly powerline SLAM corridor flights (powerline_slam_flights.py) on the
# d4s_dc_drone_powerline_eval sensor layout in an isolated transport/process
# namespace: own DDS domain (localhost discovery), Gazebo partition, config
# root (seeded with /tf/sim/sensor_layout d4s_dc_drone_powerline_eval), tmux
# sessions and transient supervision daemon.
#
# The PX4 instance and ports default to the canonical instance 0, because the
# custom-operation activation addresses MAVLink system 1. Run it where no other
# simulation uses instance 0, e.g. a dedicated worktree devcontainer.
#
# The launcher owns the simulation session and the transient daemon: it
# recycles ones left over from an earlier run and stops them after the flights
# unless --keep-running is given.
#
# DDS discovery: the evaluation layout runs about 37 DDS participants, more
# than the 32 that localhost-only discovery reaches (participants 32 and up do
# not discover each other). The default is therefore SUBNET discovery, which
# this script only accepts inside a network namespace with nothing but the
# loopback interface (e.g. a devcontainer disconnected from its network).
# III_POWERLINE_DISCOVERY_RANGE=LOCALHOST keeps localhost-only discovery.
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/home/iii/ws}"
RUN_ID="${III_POWERLINE_RUN_ID:-powerline_slam_online_$(date +%Y%m%d_%H%M%S)}"
RUNTIME_CONFIG="${III_POWERLINE_RUNTIME_CONFIG:?backend runtime configuration (JSON path)}"
[[ -f "${RUNTIME_CONFIG}" ]] || { echo "missing runtime configuration: ${RUNTIME_CONFIG}" >&2; exit 2; }
ISOLATION_ROOT="${III_POWERLINE_ISOLATION_ROOT:-${WORKSPACE_ROOT}/runtime/isolated/${RUN_ID}}"

source /opt/ros/jazzy/setup.bash
source "${WORKSPACE_ROOT}/install/setup.bash"
source "${WORKSPACE_ROOT}/setup/setup_dev.bash"
set -u

export HOME="${III_POWERLINE_HOME:-/home/iii}"
export ROS_DOMAIN_ID="${III_POWERLINE_ROS_DOMAIN_ID:-76}"
discovery_range="${III_POWERLINE_DISCOVERY_RANGE:-SUBNET}"
case "${discovery_range}" in
  SUBNET)
    if ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -vqx 'lo'; then
      echo "SUBNET discovery needs an isolated network namespace with only the loopback interface;" >&2
      echo "disconnect the container from its network or set III_POWERLINE_DISCOVERY_RANGE=LOCALHOST." >&2
      exit 2
    fi
    export ROS_LOCALHOST_ONLY=0
    ;;
  LOCALHOST)
    export ROS_LOCALHOST_ONLY=1
    ;;
  *)
    echo "III_POWERLINE_DISCOVERY_RANGE must be SUBNET or LOCALHOST" >&2
    exit 2
    ;;
esac
export ROS_AUTOMATIC_DISCOVERY_RANGE="${discovery_range}"
export GZ_PARTITION="${III_POWERLINE_GZ_PARTITION:-iii_powerline_slam_${RUN_ID}}"
export GZ_IP=127.0.0.1
export III_SIMULATION_SEED="${III_SIMULATION_SEED:-20261002}"

export CONFIG_BASE_DIR="${ISOLATION_ROOT}/config"
# The configuration server re-reads its whole tuning journal on every transaction; the workspace-wide journal grows
# with every isolated run until a set call exceeds the client timeout.  Each isolated run keeps its own.
export III_TUNING_STATE_ROOT="${ISOLATION_ROOT}/tuning"
export III_SYSTEM_RUNTIME_DIR="${ISOLATION_ROOT}/system"
export III_SYSTEM_DAEMON_SOCKET="${III_SYSTEM_RUNTIME_DIR}/system_manager.sock"
export III_SYSTEM_DAEMON_LOG="${III_SYSTEM_RUNTIME_DIR}/system_manager.log"
export III_SYSTEMD_DAEMON_SERVICE="${III_POWERLINE_SYSTEMD_SERVICE:-iii-system-daemon-powerline-slam.service}"
export III_SYSTEM_TMUX_SESSION="${III_POWERLINE_SYSTEM_SESSION:-iii_sim_powerline_slam}"
export III_SYSTEM_PROFILE=sim
export III_MICRO_ROS_AGENT_UDP_PORT="${III_POWERLINE_XRCE_PORT:-8888}"
export ROS_LOG_DIR="${ISOLATION_ROOT}/logs"

export III_SIM_TOOLS_SESSION="${III_POWERLINE_SIM_SESSION:-iii_sim_tools_powerline_slam}"
export III_SIM_TOOLS_USER="${III_POWERLINE_USER:-iii}"
export III_SIM_TOOLS_PX4_INSTANCE="${III_POWERLINE_PX4_INSTANCE:-0}"
# III_POWERLINE_SIM_MODEL: the evaluation model or one of its sensor timing profiles
# (d4s_dc_drone_powerline_eval_<profile>; same sensor layout, different sampling phases).
POWERLINE_SIM_MODEL="${III_POWERLINE_SIM_MODEL:-d4s_dc_drone_powerline_eval}"
[[ "${POWERLINE_SIM_MODEL}" == d4s_dc_drone_powerline_eval* ]] || { echo "III_POWERLINE_SIM_MODEL must be a d4s_dc_drone_powerline_eval model" >&2; exit 2; }
export III_POWERLINE_SIM_MODEL="${POWERLINE_SIM_MODEL}"
export III_SIM_TOOLS_PX4_SIM_MODEL="gz_${POWERLINE_SIM_MODEL}"
export III_SIM_TOOLS_GZ_IP=127.0.0.1
export III_SIM_TOOLS_ENSURE_ASSETS_WITH_CUSTOM_COMMAND=1
export III_GAZEBO_DRONE_MODEL="${POWERLINE_SIM_MODEL}_${III_SIM_TOOLS_PX4_INSTANCE}"
PX4_BUILD="${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default"
# The tmux server may predate this environment, so the PX4 pane gets the
# isolation variables explicitly. The Gazebo server and the sensor plugins
# inherit them (III_SIMULATION_SEED seeds the radar and noise streams).
# III_POWERLINE_SIM_CPUS: CPU list for PX4 and the Gazebo server it starts (default: unpinned).
SIM_PIN=""
[[ -n "${III_POWERLINE_SIM_CPUS:-}" ]] && SIM_PIN="taskset -c ${III_POWERLINE_SIM_CPUS} "
export III_SIM_TOOLS_PX4_COMMAND="source ${WORKSPACE_ROOT}/setup/setup_dev.bash && cd ${PX4_BUILD}/rootfs && exec ${SIM_PIN}env HEADLESS=1 ROS_DOMAIN_ID=${ROS_DOMAIN_ID} ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY} ROS_AUTOMATIC_DISCOVERY_RANGE=${ROS_AUTOMATIC_DISCOVERY_RANGE} GZ_PARTITION=${GZ_PARTITION} GZ_IP=127.0.0.1 III_SIMULATION_SEED=${III_SIMULATION_SEED} PX4_SIM_MODEL=gz_${POWERLINE_SIM_MODEL} PX4_UXRCE_DDS_PORT=${III_MICRO_ROS_AGENT_UDP_PORT} PX4_PARAM_UXRCE_DDS_SYNCT=0 ${PX4_BUILD}/bin/px4 -i ${III_SIM_TOOLS_PX4_INSTANCE}"
export PYTHONPATH="${WORKSPACE_ROOT}/tools/III-Drone-MCP:${PYTHONPATH:-}"

mkdir -p "${ISOLATION_ROOT}" "${ROS_LOG_DIR}"
# Seed the isolated configuration and select the powerline evaluation layout
# before any launch file reads it; reconciliation keeps this valid value.
active_set="$(python3 - <<'PY'
from iii_drone_configuration.schema_utils import resolve_active_parameter_file, seed_runtime_configuration
seed_runtime_configuration("sim")
print(resolve_active_parameter_file("sim"))
PY
)"
sed -i 's|^\(    /tf/sim/sensor_layout:\).*$|\1 d4s_dc_drone_powerline_eval|' "${active_set}"
grep -q '^    /tf/sim/sensor_layout: d4s_dc_drone_powerline_eval$' "${active_set}"
# The powerline_slam processing stack and its runtime configuration (boot-only constants).
sed -i 's|^\(    /perception/processing_stack:\).*$|\1 powerline_slam|' "${active_set}"
grep -q '^    /perception/processing_stack: powerline_slam$' "${active_set}"
if grep -q '^    /perception/powerline_slam/runtime_config:' "${active_set}"; then
  sed -i "s|^\\(    /perception/powerline_slam/runtime_config:\\).*\$|\\1 ${RUNTIME_CONFIG}|" "${active_set}"
else
  echo "the active parameter set has no /perception/powerline_slam/runtime_config" >&2; exit 2
fi
grep -q "^    /perception/powerline_slam/runtime_config: ${RUNTIME_CONFIG}\$" "${active_set}"
cp "${active_set}" "${ISOLATION_ROOT}/active_parameter_set.yaml"

# The transient daemon, its system session and the simulation session are
# owned by this launcher; ones left over from an earlier run carry that run's
# isolation settings.
teardown() {
  if systemctl is-active --quiet "${III_SYSTEMD_DAEMON_SERVICE}"; then
    iii system shutdown --confirm --non-interactive >/dev/null 2>&1 || true
    sudo systemctl stop "${III_SYSTEMD_DAEMON_SERVICE}"
  fi
  tmux kill-session -t "${III_SYSTEM_TMUX_SESSION}" 2>/dev/null || true
  "${WORKSPACE_ROOT}/tools/simulation/launch_simulation_tools.sh" --stop >/dev/null
}
keep_running=0
for argument in "$@"; do
  [[ "${argument}" == "--keep-running" ]] && keep_running=1
done
teardown
if ! systemctl is-active --quiet "${III_SYSTEMD_DAEMON_SERVICE}"; then
  sudo systemd-run \
    --unit="${III_SYSTEMD_DAEMON_SERVICE}" \
    --property=User="${III_SIM_TOOLS_USER}" \
    --property=WorkingDirectory="${WORKSPACE_ROOT}" \
    --setenv="WORKSPACE_ROOT=${WORKSPACE_ROOT}" \
    --setenv="III_DATASET_RUN_ID=${RUN_ID}" \
    --setenv="III_DATASET_ISOLATION_ROOT=${ISOLATION_ROOT}" \
    --setenv="III_DATASET_ROS_DOMAIN_ID=${ROS_DOMAIN_ID}" \
    --setenv="III_DATASET_ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY}" \
    --setenv="III_DATASET_ROS_DISCOVERY_RANGE=${ROS_AUTOMATIC_DISCOVERY_RANGE}" \
    --setenv="III_DATASET_GZ_PARTITION=${GZ_PARTITION}" \
    --setenv="III_DATASET_XRCE_PORT=${III_MICRO_ROS_AGENT_UDP_PORT}" \
    --setenv="III_DATASET_SYSTEMD_SERVICE=${III_SYSTEMD_DAEMON_SERVICE}" \
    --setenv="III_DATASET_SYSTEM_SESSION=${III_SYSTEM_TMUX_SESSION}" \
    --setenv="III_GAZEBO_DRONE_MODEL=${III_GAZEBO_DRONE_MODEL}" \
    --setenv="III_TUNING_STATE_ROOT=${III_TUNING_STATE_ROOT}" \
    "${WORKSPACE_ROOT}/scripts/workspace/run_isolated_perception_daemon.sh" >/dev/null
fi
cd "${WORKSPACE_ROOT}"
status=0
python3 scripts/workspace/powerline_slam_online_flights.py \
  --run-id "${RUN_ID}" \
  --headless \
  "$@" || status=$?
if ((keep_running == 0)); then
  teardown
fi
exit "${status}"
