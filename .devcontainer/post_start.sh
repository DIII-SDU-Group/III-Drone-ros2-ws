#!/bin/bash
set -euo pipefail

# Devcontainer post-start hook.
#
# Responsibilities:
# - refresh iii CLI argcomplete wiring in ~/.bashrc
# - reinstall the editable CLI package inside the container
# - build the workspace with the standard debug configuration

# Remove previously managed iii argcomplete block (if present) and legacy single-line entries.
sed -i '/# >>> iii-cli argcomplete >>>/,/# <<< iii-cli argcomplete <<</d' ~/.bashrc
sed -i '/# III CLI argcomplete (safe across argcomplete command variants)./,/^fi$/d' ~/.bashrc
sed -i '/eval "\$(register-python-argcomplete3 iii)"/d;/eval "\$(register-python-argcomplete iii)"/d' ~/.bashrc

# Add resilient completion setup once.
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

# Install and run the daemon through systemd so dev mirrors onboard runtime ownership.
./scripts/systemd/install_dev_systemd_service.sh
