#!/usr/bin/env bash
set -eo pipefail

WORKSPACE_ROOT=${WORKSPACE_ROOT:-/home/iii/ws}
DATASET_ROOT=${III_QUALIFICATION_DATASET_ROOT:-${WORKSPACE_ROOT}/datasets/powerline_qualification}
RUNTIME_ROOT=${III_QUALIFICATION_RUNTIME_ROOT:-${WORKSPACE_ROOT}/runtime/isolated/powerline_qualification_v1}
ROS_DOMAIN_ID=${III_QUALIFICATION_ROS_DOMAIN_ID:-184}
XRCE_PORT=${III_QUALIFICATION_XRCE_PORT:-19984}
PX4_INSTANCE=${III_QUALIFICATION_PX4_INSTANCE:-24}
PX4_TARGET_SYSTEM=$((PX4_INSTANCE + 1))
QUALIFICATION_PREFIX=${III_QUALIFICATION_PREFIX:-iii_powerline_qualification_v1}
RCS=${RUNTIME_ROOT}/smoke/rcS

source /opt/ros/jazzy/setup.bash
source "${WORKSPACE_ROOT}/install/setup.bash"
export ROS_DOMAIN_ID ROS_LOCALHOST_ONLY=1 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export GZ_IP=127.0.0.1 SIMULATION=true PX4_TARGET_SYSTEM
export GZ_SIM_SYSTEM_PLUGIN_PATH="${WORKSPACE_ROOT}/install/iii_drone_simulation/lib"
export PX4_GZ_WORLDS="${WORKSPACE_ROOT}/PX4-Autopilot/Tools/simulation/gz/worlds"
export PX4_GZ_MODELS="${WORKSPACE_ROOT}/PX4-Autopilot/Tools/simulation/gz/models"
export GZ_SIM_RESOURCE_PATH="${PX4_GZ_MODELS}:${PX4_GZ_WORLDS}:${GZ_SIM_RESOURCE_PATH:-}"
export PX4_UXRCE_DDS_PORT=${XRCE_PORT} PX4_SIM_HOST_ADDR=127.0.0.1 PX4_NO_FOLLOW_MODE=1

flight_split() {
  case "$1" in
    CAL*) echo calibration ;;
    VAL*) echo validation ;;
    TEST*) echo test ;;
    POSTTEST*) echo '' ;;
    CANARY) echo '' ;;
    FINALTEST*) echo '' ;;
  esac
}

flight_seed() {
  case "$1" in
    CAL*) echo $((1000 + 10#${1#CAL})) ;;
    VAL*) echo $((2000 + 10#${1#VAL})) ;;
    TEST*) echo $((3000 + 10#${1#TEST})) ;;
    POSTTEST*) echo $((4000 + 10#${1#POSTTEST})) ;;
    CANARY) echo 5099 ;;
    FINALTEST*) echo $((5000 + 10#${1#FINALTEST})) ;;
  esac
}

kill_gazebo_partition() {
  local partition=$1 pid
  for envfile in /proc/[0-9]*/environ; do
    [[ -r ${envfile} ]] || continue
    if tr '\0' '\n' < "${envfile}" 2>/dev/null | grep -Fq "GZ_PARTITION=${QUALIFICATION_PREFIX}_"; then
      pid=${envfile#/proc/}; pid=${pid%/environ}
      [[ ${pid} == $$ ]] || kill -TERM "${pid}" 2>/dev/null || true
    fi
  done
}

run_one() {
  local flight_id=$1
  local mode=${2:-full}
  local split seed flight_runtime flight_output
  split=$(flight_split "${flight_id}")
  seed=$(flight_seed "${flight_id}")
  flight_runtime=${RUNTIME_ROOT}/flights/${flight_id}
  flight_output=${DATASET_ROOT}/${split}/${flight_id}
  mkdir -p "${flight_runtime}/logs" "${flight_runtime}/config" "${flight_runtime}/rootfs"
  : > "${flight_runtime}/logs/lifecycle.log"
  export GZ_PARTITION="${QUALIFICATION_PREFIX}_${flight_id}"
  kill_gazebo_partition "${GZ_PARTITION}"
  sleep 1
  export III_SIMULATION_SEED=${seed}
  export CONFIG_BASE_DIR="${flight_runtime}/config"
  export ROS_LOG_DIR="${flight_runtime}/logs"

  local process_groups=()
  cleanup() {
    trap - EXIT INT TERM
    if ((${#process_groups[@]})); then
      for pgid in "${process_groups[@]}"; do kill -TERM -- "-${pgid}" 2>/dev/null || true; done
      sleep 2
      for pgid in "${process_groups[@]}"; do kill -KILL -- "-${pgid}" 2>/dev/null || true; done
      wait 2>/dev/null || true
    fi
    kill_gazebo_partition "${GZ_PARTITION}"
    sleep 1
    kill_gazebo_partition "${GZ_PARTITION}"
  }
  trap cleanup EXIT INT TERM

  setsid "${WORKSPACE_ROOT}/install/microxrcedds_agent/bin/MicroXRCEAgent" udp4 -p "${XRCE_PORT}" > "${flight_runtime}/logs/xrce.log" 2>&1 & process_groups+=("$!")
  HEADLESS=1 PX4_GZ_WORLD=hca_full_pylon_setup PX4_GZ_MODEL=d4s_dc_drone \
    PX4_SYS_AUTOSTART=4010 PX4_SIM_MODEL=gz_d4s_dc_drone PX4_PARAM_COM_DL_LOSS_T=300 \
    PX4_PARAM_COM_RC_IN_MODE=4 PX4_PARAM_NAV_DLL_ACT=0 \
    setsid "${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default/bin/px4" \
    -s "${RCS}" -i "${PX4_INSTANCE}" -w "${flight_runtime}/rootfs" \
    "${WORKSPACE_ROOT}/PX4-Autopilot/build/px4_sitl_default/etc" \
    </dev/null > /dev/null 2>&1 & process_groups+=("$!")

  sleep 12
  setsid ros2 launch iii_drone_simulation sim_assets.launch.py > "${flight_runtime}/logs/sim_assets.log" 2>&1 & process_groups+=("$!")
  setsid ros2 launch iii_drone_simulation tf_sim.launch.py > "${flight_runtime}/logs/tf_sim.log" 2>&1 & process_groups+=("$!")
  setsid ros2 launch iii_drone_core perception.launch.py > "${flight_runtime}/logs/perception.log" 2>&1 & process_groups+=("$!")
  sleep 8

  # Lifecycle nodes must be active for the established perception topic set.
  for node in /perception/hough_transformer/hough_transformer /perception/pl_dir_computer/pl_dir_computer /perception/pl_mapper/pl_mapper; do
    local output=""
    for _ in {1..10}; do
      output=$(timeout 8 ros2 lifecycle set "${node}" configure 2>&1 || true)
      echo "${output}" >> "${flight_runtime}/logs/lifecycle.log"
      if grep -q 'Transitioning successful' <<< "${output}"; then break; fi
      sleep 1
    done
    grep -q 'Transitioning successful' <<< "${output}"
    output=""
    for _ in {1..10}; do
      output=$(timeout 8 ros2 lifecycle set "${node}" activate 2>&1 || true)
      echo "${output}" >> "${flight_runtime}/logs/lifecycle.log"
      if grep -q 'Transitioning successful' <<< "${output}"; then break; fi
      sleep 1
    done
    grep -q 'Transitioning successful' <<< "${output}"
  done
  ros2 topic list | sort > "${flight_runtime}/topics.txt"

  local smoke_arg=()
  [[ ${mode} == smoke ]] && smoke_arg=(--smoke)
  set +e
  python3 "${WORKSPACE_ROOT}/scripts/workspace/powerline_qualification_executor.py" \
    "${flight_id}" --output-root "${DATASET_ROOT}" "${smoke_arg[@]}" \
    > "${flight_runtime}/logs/executor.log" 2>&1
  local rc=$?
  set -e
  if [[ ${rc} -eq 0 ]]; then
    set +e
    python3 "${WORKSPACE_ROOT}/scripts/workspace/validate_powerline_qualification_flight.py" \
      "${flight_id}" --output-root "${DATASET_ROOT}" \
      > "${flight_runtime}/logs/validation.log" 2>&1
    rc=$?
    set -e
  fi
  echo "${rc}" > "${flight_runtime}/executor.exit_code"
  cleanup
  trap - EXIT INT TERM
  return "${rc}"
}

if (($# == 0)); then
  flights=(CAL01 CAL02 CAL03 CAL04 CAL05 CAL06 CAL07 CAL08 CAL09 CAL10 CAL11 CAL12 VAL01 VAL02 VAL03 VAL04 TEST01 TEST02 TEST03 TEST04)
  for flight in "${flights[@]}"; do run_one "${flight}" full; done
else
  run_one "$1" "${2:-full}"
fi
