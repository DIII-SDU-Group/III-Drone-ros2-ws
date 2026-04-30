#!/usr/bin/env bash
set -euo pipefail
set -x

usage() {
  cat <<'USAGE'
NAME
  install_remote.bash - bootstrap the local machine for III remote workflows

SYNOPSIS
  scripts/remote/install_remote.bash

DESCRIPTION
  Installs and configures the local developer environment used for remote
  deployment workflows.

  Actions performed:
  - source and persist `setup/setup_remote.bash`
  - install the editable III CLI
  - configure CLI argcomplete in `~/.bashrc`
  - install `sshpass` and remote-development helper packages
  - configure SSH agent forwarding for the target host
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

source "$WORKSPACE_DIR/setup/setup_remote.bash"

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
_iii_python_argcomplete() {
    local IFS=$'\013'
    local suppress_space=0
    if compopt +o nospace 2> /dev/null; then
        suppress_space=1
    fi
    COMPREPLY=( $(IFS="$IFS" \
                  COMP_LINE="$COMP_LINE" \
                  COMP_POINT="$COMP_POINT" \
                  COMP_TYPE="$COMP_TYPE" \
                  _ARGCOMPLETE_COMP_WORDBREAKS="$COMP_WORDBREAKS" \
                  _ARGCOMPLETE=1 \
                  _ARGCOMPLETE_SUPPRESS_SPACE=$suppress_space \
                  "$1" 8>&1 9>&2 1>/dev/null 2>/dev/null) )
    if [[ $? != 0 ]]; then
        unset COMPREPLY
    elif [[ $suppress_space == 1 ]] && [[ "$COMPREPLY" =~ [=/:]$ ]]; then
        compopt -o nospace
    fi
}
complete -o nospace -o default -F _iii_python_argcomplete iii
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
