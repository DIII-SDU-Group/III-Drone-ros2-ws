source /arm64-sysroot/opt/ros/humble/setup.bash
source /arm64-sysroot/home/iii/ws/install/setup.bash 2> /dev/null

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source $SCRIPT_DIR/paths.bash

export CLI_CONFIGURATION="container"

source $SCRIPT_DIR/tmuxinator_config.bash
#export TMUXINATOR_PROJECT=$TMUXINATOR_PROJECT_OPTI_TRACK
export TMUXINATOR_PROJECT=$TMUXINATOR_PROJECT_REAL
export TMUXINATOR_PROJECT_HITL=$TMUXINATOR_PROJECT_REAL_HITL

export SIMULATION="false"

source $SCRIPT_DIR/supervisor_config.bash
#export SUPERVISOR_CONFIG_FILE=$SUPERVISOR_CONFIG_FILE_OPTI_TRACK
export SUPERVISOR_CONFIG_FILE=$SUPERVISOR_CONFIG_FILE_REAL

source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash
