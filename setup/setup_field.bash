#!/usr/bin/env bash
# Operator-computer field profile. This sets convenient defaults only; every
# deployment/runtime command may still select its target/profile explicitly.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Reuse the supported workstation toolchain and installed workspace paths.
# setup_dev does not mutate a target and all values below are process-local.
source "$SCRIPT_DIR/setup_dev.bash"

export CLI_CONFIGURATION="remote"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
export III_ENVIRONMENT_PROFILE="field"
export III_DEFAULT_TARGET="real"

# Field middleware binds to a detected stable LAN interface at runtime. Do not
# leak the local-simulation Gazebo loopback binding into a field shell.
unset GZ_IP
