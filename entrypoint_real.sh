#!/bin/bash
# set -e

# # Source the ROS2 setup script
# source /opt/ros/humble/setup.bash

# # Source the workspace setup script
# if [ -f /home/iii/ws/install/setup.bash ]; then
#     source /home/iii/ws/install/setup.bash
# fi

# Source the workspace setup script
source /arm64-sysroot/home/iii/ws/setup_real.bash
cd /arm64-sysroot/home/iii/ws

# Execute the command passed to the entrypoint
exec "$@"
