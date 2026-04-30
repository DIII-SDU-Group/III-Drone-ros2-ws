#!/bin/bash
set -euo pipefail

# Devcontainer post-start hook.
#
# Responsibilities:
# - refresh iii CLI argcomplete wiring in ~/.bashrc
# - reinstall the editable CLI package inside the container
# - build the workspace with the standard debug configuration
# - run the schema-aware configuration install/update that requires the built
#   iii_drone_configuration native extension

ensure_workspace_runtime_ownership() {
    local target_user="iii"
    local target_group="iii"

    sudo mkdir -p /home/iii/ws/.config /home/iii/ws/runtime /home/iii/ws/runtime_logs
    sudo chown -R "${target_user}:${target_group}" \
        /home/iii/ws/.config \
        /home/iii/ws/runtime \
        /home/iii/ws/runtime_logs
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

# Reinstall the III-Drone-CLI
pip3 uninstall -y iii 2> /dev/null
pip3 install -e ./tools/III-Drone-CLI

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

# micro_ros_agent does not declare the vendored Micro XRCE-DDS Agent as a ROS
# package dependency, so build the CMake package first and source it explicitly.
COLCON_HOME=/home/iii/ws colcon build \
    "${COLCON_COMMON_ARGS[@]}" \
    --packages-select microxrcedds_agent \
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
    --packages-skip microxrcedds_agent micro_ros_agent \
    "${COLCON_CMAKE_ARGS[@]}"

# Install configuration after the workspace build so the native validation
# extension used by the configuration tools matches the current sources.
./src/III-Drone-Configuration/scripts/install.sh .config

# Install and run the daemon through systemd so dev mirrors onboard runtime ownership.
./scripts/systemd/install_dev_systemd_service.sh
