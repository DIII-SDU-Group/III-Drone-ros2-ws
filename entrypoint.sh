#!/bin/bash
set -e

# Source the ROS2 setup script
source /opt/ros/$ROS_DISTRO/setup.bash

# Source the workspace setup script
source install/setup.bash

# Execute the command passed to the entrypoint
exec "$@"
