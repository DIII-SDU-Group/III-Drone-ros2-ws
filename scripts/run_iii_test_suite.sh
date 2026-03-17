#!/usr/bin/env bash

set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
