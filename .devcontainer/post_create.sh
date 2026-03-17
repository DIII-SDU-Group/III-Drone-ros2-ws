#!/bin/bash
set -euo pipefail

# Devcontainer post-create hook.
#
# Responsibilities:
# - ensure the dev profile is sourced from ~/.bashrc
# - install workspace-owned configuration into .config
# - install PX4/Gazebo simulation prerequisites
# - run rosdep for the workspace
# - install additional apt tools needed in the devcontainer

# If source /home/iii/ws/setup_dev.bash is not in ~/.bashrc, add it
if ! grep -q "source /home/iii/ws/setup/setup_dev.bash" ~/.bashrc; then
    echo "source /home/iii/ws/setup/setup_dev.bash" >> ~/.bashrc
fi

# Install configuration
./src/III-Drone-Configuration/scripts/install.sh .config 

# Install simulation assets
if [ ! -d /home/iii/ws/PX4-Autopilot ]; then
    echo "ERROR: /home/iii/ws/PX4-Autopilot not found. Add PX4-Autopilot as a submodule before creating the devcontainer."
    exit 1
fi

/bin/bash /home/iii/ws/PX4-Autopilot/Tools/setup/ubuntu.sh

./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/ws/PX4-Autopilot

sudo apt update
sudo apt install -y gcc-arm-none-eabi

# Rosdep install
rosdep update && rosdep install --from-paths src --ignore-src -y && sudo chown -R $(whoami) /home/iii/ws/
