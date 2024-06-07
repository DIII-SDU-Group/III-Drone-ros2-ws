source /opt/ros/humble/setup.bash
source /home/iii/ws/install/setup.bash

export CONFIG_BASE_DIR="/home/iii/ws/.config"
export SIMULATION="false"
export SUPERVISOR_CONFIG_FILE="/home/iii/ws/src/III-Drone-Supervision/supervision_config/real.yaml"
export TMUXINATOR_PROJECT="iii_real_launch"

export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

export SUPERVISOR_LOG_LEVEL="info"
export CONFIGURATION_SERVER_LOG_LEVEL="info"
export DRONE_FRAME_BROADCASTER_LOG_LEVEL="info"
export HOUGH_TRANSFORMER_LOG_LEVEL="info"
export PL_DIR_COMPUTER_LOG_LEVEL="info"
export PL_MAPPER_LOG_LEVEL="info"
export TRAJECTORY_GENERATOR_LOG_LEVEL="info"
export MANEUVER_CONTROLLER_LOG_LEVEL="debug"