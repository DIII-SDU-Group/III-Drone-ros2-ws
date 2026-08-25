#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly III_ROS_PREFIX="${III_ROS_PREFIX:-/opt/ros/jazzy}"
readonly III_RELEASE_ROOT="${III_RELEASE_ROOT:-/opt/iii/current}"

if [[ ! -r "${III_ROS_PREFIX}/setup.bash" ]]; then
  echo "III real profile requires ROS Jazzy at ${III_ROS_PREFIX}" >&2
  return 30 2>/dev/null || exit 30
fi
source "${III_ROS_PREFIX}/setup.bash"

# A production activation atomically updates /opt/iii/current. Developers may
# source a workspace install explicitly, but setup never impersonates a sysroot.
if [[ -r "${III_RELEASE_ROOT}/install/setup.bash" ]]; then
  source "${III_RELEASE_ROOT}/install/setup.bash"
elif [[ -n "${III_WORKSPACE_INSTALL:-}" && -r "${III_WORKSPACE_INSTALL}/setup.bash" ]]; then
  source "${III_WORKSPACE_INSTALL}/setup.bash"
fi

source "${SCRIPT_DIR}/cli_path.bash"
source "${SCRIPT_DIR}/paths.bash"
export CONFIG_BASE_DIR="${CONFIG_BASE_DIR:-${HOME}/.config}"
export CLI_CONFIGURATION="native"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
source "${SCRIPT_DIR}/node_log_levels.bash"
source "${SCRIPT_DIR}/ros_setup.bash"
export CYCLONEDDS_URI="${CYCLONEDDS_URI_REAL}"
