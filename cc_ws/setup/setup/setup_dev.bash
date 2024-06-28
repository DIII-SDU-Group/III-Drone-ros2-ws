export CLI_CONFIGURATION="dev"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export WORKSPACE_DIR="$(dirname $SCRIPT_DIR)"

source $SCRIPT_DIR/paths.bash

export SIMULATION="true"
export SUPERVISOR_CONFIG_FILE="$SUPERVISOR_CONFIG_DIR/sim.yaml"
export TMUXINATOR_PROJECT="iii_dev_launch"

export COLCON_HOME="$WORKSPACE_DIR"

source $SCRIPT_DIR/remote.bash
source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash

export DEBUGGABLE_NODES=$(cat $SCRIPT_DIR/debuggable_nodes.txt | tr '\n' ' ')

source $SCRIPT_DIR/python_debug_ports.bash