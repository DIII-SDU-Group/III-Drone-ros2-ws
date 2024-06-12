export CLI_CONFIGURATION="dev"

export CONFIG_BASE_DIR="/home/iii/ws/.config"
export SIMULATION="true"
export SUPERVISOR_CONFIG_FILE="/home/iii/ws/src/III-Drone-Supervision/supervision_config/sim.yaml"
export TMUXINATOR_PROJECT="iii_dev_launch"

export COLCON_HOME="/home/iii/ws"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
export WORKSPACE_DIR="$(dirname $SCRIPT_DIR)"

source $SCRIPT_DIR/remote.bash
source $SCRIPT_DIR/node_log_levels.bash
source $SCRIPT_DIR/ros_setup.bash