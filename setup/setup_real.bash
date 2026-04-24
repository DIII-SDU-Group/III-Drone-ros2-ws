source /arm64-sysroot/opt/ros/humble/setup.bash
source /arm64-sysroot/home/iii/ws/install/setup.bash 2> /dev/null

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source $SCRIPT_DIR/cli_path.bash
source $SCRIPT_DIR/paths.bash

export CONFIG_BASE_DIR="$HOME/.config"

export CLI_CONFIGURATION="container"

export SIMULATION="false"
export III_SYSTEM_PROFILE="real"

source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash

export CYCLONEDDS_URI=$CYCLONEDDS_URI_REAL
