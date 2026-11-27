#!/usr/bin/env bash
# Operator-computer field profile. This sets convenient defaults only; every
# deployment/runtime command may still select its target/profile explicitly.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
export WORKSPACE_DIR

# A field shell controls the normal developer account and services through the
# III CLI. It does not need a ROS overlay on this computer.
source "$SCRIPT_DIR/cli_path.bash"
source "$SCRIPT_DIR/paths.bash"
source "$SCRIPT_DIR/remote_runtime.bash"

export CLI_CONFIGURATION="remote"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
export III_ENVIRONMENT_PROFILE="field"
export III_RUNTIME_HOST_PROFILE="field"
export III_DEFAULT_TARGET="real"
export III_RUNTIME_TARGET="real"
export III_SSH_HOST="${III_HIL_PI_ENDPOINT:-${III_HIL_PI_ADDRESS:-${III_SSH_HOST:-${III_RUNTIME_HOST:-${III_RUNTIME_API_HOST:-}}}}}"
export III_SSH_USER="${III_SSH_USER:-iii}"

# Field middleware binds to a detected stable LAN interface at runtime. Do not
# leak the local-simulation Gazebo loopback binding into a field shell.
unset GZ_IP
