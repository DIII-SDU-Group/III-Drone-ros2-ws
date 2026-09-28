#!/bin/bash
set -euo pipefail

# Devcontainer post-start hook.
#
# Responsibilities:
# - refresh iii CLI argcomplete wiring in ~/.bashrc
# - reinstall the editable CLI package inside the container
# - build the workspace with the standard debug configuration
# - install and restart the supervised daemon and Runtime API

POST_START_READY=/run/lock/iii-dev-post-start.ready
exec 7>/run/lock/iii-dev-post-start.lock
flock -x 7
if [[ "${1:-}" == "--if-needed" && -f "${POST_START_READY}" ]]; then
    exit 0
fi
rm -f "${POST_START_READY}"

ensure_workspace_runtime_ownership() {
    local target_user="iii"
    local target_group="iii"

    sudo mkdir -p \
        /home/iii/ws/.config \
        /home/iii/ws/runtime \
        /home/iii/ws/runtime_logs \
        /home/iii/ws/build \
        /home/iii/ws/install \
        /home/iii/ws/log
    sudo chown -R "${target_user}:${target_group}" \
        /home/iii/ws/.config \
        /home/iii/ws/runtime \
        /home/iii/ws/runtime_logs \
        /home/iii/ws/build \
        /home/iii/ws/install \
        /home/iii/ws/log
}

ensure_source_line() {
    local line="$1"
    local file="$2"

    if ! grep -qxF "$line" "$file" 2>/dev/null; then
        echo "$line" >> "$file"
    fi
}

ensure_workspace_runtime_ownership
ensure_source_line "source /home/iii/ws/setup/setup_dev.bash" "$HOME/.bashrc"
ensure_source_line "source /home/iii/ws/setup/setup_dev.bash" "$HOME/.profile"

# Remove previously managed iii argcomplete block (if present) and legacy single-line entries.
sed -i '/# >>> iii-cli argcomplete >>>/,/# <<< iii-cli argcomplete <<</d' ~/.bashrc
sed -i '/# III CLI argcomplete (safe across argcomplete command variants)./,/^fi$/d' ~/.bashrc
sed -i '/eval "\$(register-python-argcomplete3 iii)"/d;/eval "\$(register-python-argcomplete iii)"/d' ~/.bashrc

# Add static iii completion wiring once. This avoids runtime dependence on
# whichever register-python-argcomplete helper happens to be installed.
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

# The old deployment Python distribution was removed from this editable
# workspace. Clear it before refreshing dependencies so its obsolete pins do
# not conflict with the current requirements on reused devcontainers.
if pip3 show iii-deployment >/dev/null 2>&1; then
    pip3 uninstall -y iii-deployment
fi

# Refresh workspace Python dependencies, then install the local editable III
# distributions. The deployment/ directory now contains Ansible and systemd
# assets rather than a Python package.
pip3 install -r ./requirements.txt
pip3 install -e ./src/III-Drone-Contracts
pip3 install -e ./src/III-Drone-Configuration
pip3 uninstall -y iii 2> /dev/null
pip3 install -e ./tools/III-Drone-CLI
# Simulation acceptance and fixture helpers invoke the MCP command-line
# entrypoints directly from the devcontainer.  Install the workspace package so
# those helpers do not depend on an ad-hoc PYTHONPATH or a manually prepared
# shell.
pip3 install -e ./tools/III-Drone-MCP

# Refresh PX4 Gazebo simulation assets if the checkout is present.
if [ -d /home/iii/ws/PX4-Autopilot ]; then
    ./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/ws/PX4-Autopilot
fi

# Source only the ROS underlay before building. Sourcing the workspace install
# here can make CMake resolve stale artifacts from previous builds.
set +u
source /opt/ros/jazzy/setup.bash
set -u

# Build workspace. Limit discovery to src/ so colcon does not pick up duplicate
# packages from auxiliary workspaces or Python virtual environments.
COLCON_COMMON_ARGS=(
    --base-paths src
    --symlink-install
)
COLCON_CMAKE_ARGS=(
    --cmake-args
    -DCMAKE_BUILD_TYPE=Debug
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
)

# micro_ros_agent needs both the vendored Micro XRCE-DDS Agent and the generated
# micro_ros_msgs package in the install prefix before its own build starts.
COLCON_HOME=/home/iii/ws colcon build \
    "${COLCON_COMMON_ARGS[@]}" \
    --packages-select microxrcedds_agent micro_ros_msgs \
    "${COLCON_CMAKE_ARGS[@]}"

set +u
source /home/iii/ws/install/setup.bash
set -u

COLCON_HOME=/home/iii/ws colcon build \
    "${COLCON_COMMON_ARGS[@]}" \
    --packages-select micro_ros_agent \
    "${COLCON_CMAKE_ARGS[@]}" \
    -DMICROROSAGENT_SUPERBUILD=OFF

set +u
source /home/iii/ws/install/setup.bash
set -u

COLCON_HOME=/home/iii/ws colcon build \
    "${COLCON_COMMON_ARGS[@]}" \
    --packages-skip microxrcedds_agent micro_ros_msgs micro_ros_agent \
    "${COLCON_CMAKE_ARGS[@]}"

# Install and run the daemon through systemd so dev mirrors onboard runtime ownership.
./scripts/systemd/install_dev_systemd_service.sh
./scripts/systemd/install_runtime_api_service.sh
touch "${POST_START_READY}"
