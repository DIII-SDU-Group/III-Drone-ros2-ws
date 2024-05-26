#!/bin/bash

# Install configuration
./src/III-Drone-Configuration/scripts/install.sh .config 

# Install simulation assets
./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/PX4-Autopilot

# Rosdep install
rosdep update && rosdep install --from-paths src --ignore-src -y && sudo chown -R $(whoami) /home/iii/ws/