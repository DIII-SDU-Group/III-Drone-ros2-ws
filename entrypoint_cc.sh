#!/usr/bin/env bash
set -euo pipefail

readonly sysroot="${III_SYSROOT:-/opt/iii/sysroot}"
readonly ros_setup="${sysroot}/opt/ros/jazzy/setup.bash"
if [[ ! -r "${ros_setup}" ]]; then
  echo "canonical Jazzy target sysroot is unavailable: ${ros_setup}" >&2
  exit 30
fi

# These variables describe target search paths; executables still run on the
# amd64 builder. No host executable is injected into the target sysroot.
export AMENT_PREFIX_PATH="${sysroot}/opt/ros/jazzy${AMENT_PREFIX_PATH:+:${AMENT_PREFIX_PATH}}"
export CMAKE_PREFIX_PATH="${sysroot}/opt/ros/jazzy${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
exec "$@"
