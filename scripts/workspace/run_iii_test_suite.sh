#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  run_iii_test_suite.sh - run the workspace-owned III package test suite

SYNOPSIS
  scripts/workspace/run_iii_test_suite.sh

DESCRIPTION
  Runs the curated III-only test suite for this workspace:
  - ROS package tests for the selected III packages via `colcon test`
  - top-level integration pytest suite under `tests/`
  - CLI pytest suite under `tools/III-Drone-CLI/tests`

  This script intentionally excludes third-party package tests.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "${workspace_root}/.." && pwd)"
cd "${workspace_root}"

if [[ -n "${ROS_DISTRO:-}" ]] && [[ -f "/opt/ros/${ROS_DISTRO}/setup.sh" ]]; then
  set +u
  . "/opt/ros/${ROS_DISTRO}/setup.sh"
  set -u
elif [[ -f "/opt/ros/jazzy/setup.sh" ]]; then
  set +u
  . "/opt/ros/jazzy/setup.sh"
  set -u
fi

packages=(
  iii_drone_core
  iii_drone_interfaces
  iii_drone_mission
  iii_drone_simulation
  iii_drone_supervision
  iii_drone_gc
)

has_stale_colcon_prefix=0
if [[ -f install/setup.sh ]]; then
  install_prefix="$(sed -n 's/^_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX=//p' install/setup.sh | head -n 1 | tr -d '"')"
  if [[ -n "$install_prefix" && "$install_prefix" != "${workspace_root}/install" ]]; then
    has_stale_colcon_prefix=1
  fi
fi

while IFS= read -r cache_file; do
  cache_source_dir="$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' "$cache_file")"
  if [[ -n "$cache_source_dir" && "$cache_source_dir" != "${workspace_root}"/* ]]; then
    has_stale_colcon_prefix=1
    break
  fi
done < <(find build -name CMakeCache.txt -print 2>/dev/null)

if (( has_stale_colcon_prefix == 1 )); then
  echo "Detected stale colcon build/install metadata from a different workspace path."
  echo "Cleaning build/, install/, and log/ before rebuilding test targets."
  rm -rf build install log
fi

colcon build \
  --base-paths src \
  --packages-up-to \
  "${packages[@]}"

colcon test \
  --base-paths src \
  --packages-select \
  "${packages[@]}"

colcon test-result --verbose

set +u
. install/setup.sh
set -u

python3 -m pytest tests
python3 -m pytest tools/III-Drone-CLI/tests
