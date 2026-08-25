#!/usr/bin/env bash
set -euo pipefail

readonly sysroot="${III_SYSROOT:-/opt/iii/sysroot}"
readonly toolchain="${CMAKE_TOOLCHAIN_FILE:-/opt/iii/arm64-toolchain.cmake}"
[[ -d "${sysroot}/opt/ros/jazzy" ]] || { echo "missing Jazzy sysroot" >&2; exit 30; }
[[ -r "${toolchain}" ]] || { echo "missing ARM64 toolchain" >&2; exit 30; }

colcon build \
  --base-paths src \
  --packages-skip micro_ros_agent microxrcedds_agent micro_ros_msgs px4_msgs \
  --packages-skip-regex 'example_.*' \
  "$@" \
  --cmake-args \
  -DCMAKE_TOOLCHAIN_FILE="${toolchain}" \
  -DCMAKE_PREFIX_PATH="${sysroot}/opt/ros/jazzy;${sysroot}/usr"
