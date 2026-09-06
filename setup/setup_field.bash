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
source "$SCRIPT_DIR/remote_runtime.bash"

export CLI_CONFIGURATION="remote"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
export III_ENVIRONMENT_PROFILE="field"
export III_DEFAULT_TARGET="real"

# Use the operator-owned workstation trust bootstrap consistently for release,
# field-readiness, and GC application verification. Provisioning this public
# trust store remains an explicit one-time step; sourcing this profile never
# creates or modifies credentials.
III_WORKSTATION_TRUST_STORE="${XDG_CONFIG_HOME:-$HOME/.config}/iii/keys/signing/trusted-signers.json"
if [[ -f "$III_WORKSTATION_TRUST_STORE" ]]; then
    export III_RELEASE_TRUSTED_SIGNERS="${III_RELEASE_TRUSTED_SIGNERS:-$III_WORKSTATION_TRUST_STORE}"
    export III_GC_TRUSTED_SIGNERS="${III_GC_TRUSTED_SIGNERS:-$III_WORKSTATION_TRUST_STORE}"
fi
unset III_WORKSTATION_TRUST_STORE

# Field middleware binds to a detected stable LAN interface at runtime. Do not
# leak the local-simulation Gazebo loopback binding into a field shell.
unset GZ_IP
