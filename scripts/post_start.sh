#!/bin/bash

# If eval "$(register-python-argcomplete iii)" is not in ~/.bashrc, add it
if ! grep -q "eval \"\$(register-python-argcomplete3 iii)\"" ~/.bashrc; then
    echo "eval \"\$(register-python-argcomplete3 iii)\"" >> ~/.bashrc
fi

# Reinstall the III-Drone-CLI
pip3 uninstall -y iii 2> /dev/null
pip3 install -e ./tools/III-Drone-CLI

# Build workspace
colcon build --cmake-args -DCMAKE_BUILD_TYPE=Debug