#!/bin/bash

set -x
set -e

SCRIPT_DIR=$(dirname $(readlink -f $BASH_SOURCE))

source $SCRIPT_DIR/../setup/setup_remote.bash

if ! grep -q "source $WORKSPACE_DIR/setup/setup_remote.bash" ~/.bashrc; then
    echo "source $WORKSPACE_DIR/setup/setup_remote.bash" >> ~/.bashrc
fi

# Install cli tools
pip3 install -e $WORKSPACE_DIR/tools/III-Drone-CLI

sudo activate-global-python-argcomplete3

if ! grep -q "eval \"\$(register-python-argcomplete3 iii)\"" ~/.bashrc; then
    echo "eval \"\$(register-python-argcomplete3 iii)\"" >> ~/.bashrc
fi

sudo apt install -y sshpass

# Setup ssh agent forwarding
# If ~/.ssh/config does not exist, create it
if [ ! -f ~/.ssh/config ]; then
    touch ~/.ssh/config
fi

# If ~/.ssh/config does not contain the following lines, append them
if ! grep -q "Host $III_SSH_HOST" ~/.ssh/config; then
    echo "Host $III_SSH_HOST" >> ~/.ssh/config
    echo "    ForwardAgent yes" >> ~/.ssh/config
fi

# Install cross-compilation tools
sudo apt update

sudo apt install -y \
    liblog4cxx-dev \
    python3-dev

sudo apt install -y \
    python3-numpy \
    python3-netifaces \
    python3-yaml

sudo apt install -y sshfs