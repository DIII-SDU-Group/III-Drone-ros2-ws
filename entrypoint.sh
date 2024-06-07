#!/bin/bash
set -e

# Source the ROS2 setup script
source /opt/ros/$ROS_DISTRO/setup.bash

# Source the workspace setup script
if [ -f /home/iii/ws/install/setup.bash ]; then
    source /home/iii/ws/install/setup.bash
fi

# source /home/iii/ws/setup_real.bash

# Execute the command passed to the entrypoint
bash -c "$@"
