export CLI_CONFIGURATION="remote"
unset III_RUNTIME_TARGET
unset III_RUNTIME_HOST_PROFILE

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
export WORKSPACE_DIR

source "$SCRIPT_DIR/cli_path.bash"
source "$SCRIPT_DIR/remote_runtime.bash"
