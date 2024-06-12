source /arm64-sysroot/opt/ros/humble/setup.bash
source /arm64-sysroot/home/iii/ws/install/setup.bash 2> /dev/null

export CLI_CONFIGURATION="container"

export CONFIG_BASE_DIR="/home/iii/ws/.config"
export TMUXINATOR_PROJECT="iii_real_launch"

export SIMULATION="false"
export SUPERVISOR_CONFIG_FILE="/home/iii/ws/src/III-Drone-Supervision/supervision_config/real.yaml"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash