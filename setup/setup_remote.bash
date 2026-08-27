export CLI_CONFIGURATION="remote"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
export WORKSPACE_DIR

source "$SCRIPT_DIR/cli_path.bash"
