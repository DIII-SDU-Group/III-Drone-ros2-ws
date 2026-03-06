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

if command -v activate-global-python-argcomplete3 >/dev/null 2>&1; then
    sudo activate-global-python-argcomplete3 || true
fi

# Remove previously managed iii argcomplete block (if present) and legacy single-line entries.
sed -i '/# >>> iii-cli argcomplete >>>/,/# <<< iii-cli argcomplete <<</d' ~/.bashrc
sed -i '/# III CLI argcomplete (safe across argcomplete command variants)./,/^fi$/d' ~/.bashrc
sed -i '/eval "\$(register-python-argcomplete3 iii)"/d;/eval "\$(register-python-argcomplete iii)"/d' ~/.bashrc

if ! grep -q "# >>> iii-cli argcomplete >>>" ~/.bashrc; then
cat >> ~/.bashrc <<'EOF'
# >>> iii-cli argcomplete >>>
__iii_enable_argcomplete() {
    local cmd shellcode
    for cmd in register-python-argcomplete3 register-python-argcomplete; do
        if command -v "$cmd" >/dev/null 2>&1; then
            shellcode="$("$cmd" iii 2>/dev/null || true)"
            if [ -n "$shellcode" ]; then
                eval "$shellcode"
                return 0
            fi
        fi
    done
    return 1
}
__iii_enable_argcomplete || true
unset -f __iii_enable_argcomplete
# <<< iii-cli argcomplete <<<
EOF
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
