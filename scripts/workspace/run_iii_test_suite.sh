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

colcon test \
  --base-paths src \
  --packages-select \
  iii_drone_core \
  iii_drone_interfaces \
  iii_drone_mission \
  iii_drone_simulation \
  iii_drone_supervision \
  iii_drone_gc

colcon test-result --verbose

python3 -m pytest \
  tests \
  tools/III-Drone-CLI/tests
