#!/usr/bin/env bash
# Onboard OptiTrack runtime profile. It shares the supported local ROS/CLI
# environment with real mode while selecting OptiTrack's independent runtime
# and configuration profile.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/setup_real.bash"

export CLI_CONFIGURATION="dev"
export SIMULATION="false"
export III_SYSTEM_PROFILE="opti_track"
export III_ENVIRONMENT_PROFILE="opti_track"
export III_DEFAULT_TARGET="opti_track"
export III_RUNTIME_TARGET="opti_track"
