#!/bin/bash

# Install III-Drone-CLI in editable mode
pip3 install -e ./tools/III-Drone-CLI

# If eval "$(register-python-argcomplete iii)" is not in ~/.bashrc, add it
if ! grep -q "eval \"\$(register-python-argcomplete3 iii)\"" ~/.bashrc; then
    echo "eval \"\$(register-python-argcomplete3 iii)\"" >> ~/.bashrc
fi

# Reinstall the III-Drone-CLI
pip3 uninstall -y iii 2> /dev/null
pip3 install -e ./tools/III-Drone-CLI

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