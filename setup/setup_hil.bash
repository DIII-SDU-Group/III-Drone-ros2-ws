#!/usr/bin/env bash
# Split-host HIL operator profile: runtime on iii.local, Gazebo/PX4 SITL on the
# workstation. The aircraft Runtime API remains the only III control boundary.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$SCRIPT_DIR/setup_field.bash"

export III_SYSTEM_PROFILE="hil"
export III_DEFAULT_TARGET="hil"
