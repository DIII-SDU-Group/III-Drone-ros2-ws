#!/usr/bin/env bash
# Onboard OptiTrack runtime profile. It shares the local ROS/CLI environment of
# real mode (including the provisioned runtime paths and stack DDS domain from
# /etc/iii/runtime.env on the Pi) while selecting OptiTrack's independent
# runtime and configuration profile.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! source "${SCRIPT_DIR}/setup_real.bash"; then
  return 30 2>/dev/null || exit 30
fi

export CLI_CONFIGURATION="dev"
export SIMULATION="false"
export III_SYSTEM_PROFILE="opti_track"
export III_ENVIRONMENT_PROFILE="opti_track"
export III_DEFAULT_TARGET="opti_track"
export III_RUNTIME_TARGET="opti_track"
