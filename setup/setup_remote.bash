export CLI_CONFIGURATION="remote"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export WORKSPACE_DIR="$(dirname $SCRIPT_DIR)"

source $SCRIPT_DIR/remote.bash