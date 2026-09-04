#!/usr/bin/env bash
# Operator-computer field profile. This sets convenient defaults only; every
# deployment/runtime command may still select its target/profile explicitly.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
export WORKSPACE_DIR

# A field operator shell controls installed remote services through the III CLI.
# Do not source the development ROS overlay here: it may be absent or stale on a
# field laptop and is not required for runtime API or receiver operations.
source "$SCRIPT_DIR/cli_path.bash"
source "$SCRIPT_DIR/paths.bash"

export CLI_CONFIGURATION="remote"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
export III_ENVIRONMENT_PROFILE="field"
export III_DEFAULT_TARGET="real"

# Field middleware binds to a detected stable LAN interface at runtime. Do not
# leak the local-simulation Gazebo loopback binding into a field shell.
unset GZ_IP
