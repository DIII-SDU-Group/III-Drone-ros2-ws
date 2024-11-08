export CLI_CONFIGURATION="dev"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export WORKSPACE_DIR="$(dirname $SCRIPT_DIR)"

source $SCRIPT_DIR/paths.bash

export SIMULATION="true"
source $SCRIPT_DIR/supervisor_config.bash
export SUPERVISOR_CONFIG_FILE=$SUPERVISOR_CONFIG_FILE_SIM

source $SCRIPT_DIR/tmuxinator_config.bash
export TMUXINATOR_PROJECT=$TMUXINATOR_PROJECT_DEV
export TMUXINATOR_PROJECT_HITL=$TMUXINATOR_PROJECT_DEV_HITL

export COLCON_HOME="$WORKSPACE_DIR"

source $SCRIPT_DIR/remote.bash
source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash

export CYCLONEDDS_URI=$CYCLONEDDS_URI_REMOTE

export ROS_LOG_DIR_BASE=$WORKSPACE_DIR/runtime_logs

export DEBUGGABLE_NODES=$(cat $SCRIPT_DIR/debuggable_nodes.txt | tr '\n' ' ')

source $SCRIPT_DIR/python_debug_ports.bash