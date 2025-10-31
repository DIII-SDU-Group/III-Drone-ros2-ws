#!/bin/bash

# If source /home/iii/ws/setup_dev.bash is not in ~/.bashrc, add it
if ! grep -q "source /home/iii/ws/setup/setup_dev.bash" ~/.bashrc; then
    echo "source /home/iii/ws/setup/setup_dev.bash" >> ~/.bashrc
fi

# Install configuration
./src/III-Drone-Configuration/scripts/install.sh .config 

# Install simulation assets
if [ ! -d /home/iii/ws/PX4-Autopilot ]; then
    git clone --recursive https://github.com/DIII-SDU-Group/PX4-Autopilot.git /home/iii/ws/PX4-Autopilot
    /bin/bash /home/iii/ws/PX4-Autopilot/Tools/setup/ubuntu.sh
fi

./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/ws/PX4-Autopilot

sudo apt update
sudo apt install -y gcc-arm-none-eabi

# Rosdep install
rosdep update && rosdep install --from-paths src --ignore-src -y && sudo chown -R $(whoami) /home/iii/ws/