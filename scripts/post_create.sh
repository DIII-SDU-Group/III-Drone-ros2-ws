#!/bin/bash

# If source /home/iii/ws/dev_setup.bash is not in ~/.bashrc, add it
if ! grep -q "source /home/iii/ws/dev_setup.bash" ~/.bashrc; then
    echo "source /home/iii/ws/dev_setup.bash" >> ~/.bashrc
fi

# Install configuration
./src/III-Drone-Configuration/scripts/install.sh .config 

# Install simulation assets
./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/PX4-Autopilot

# Rosdep install
rosdep update && rosdep install --from-paths src --ignore-src -y && sudo chown -R $(whoami) /home/iii/ws/