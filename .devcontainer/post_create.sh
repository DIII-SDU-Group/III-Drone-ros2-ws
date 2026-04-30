#!/bin/bash
set -euo pipefail

# Devcontainer post-create hook.
#
# Responsibilities:
# - ensure the dev profile is sourced from ~/.bashrc
# - install PX4/Gazebo simulation prerequisites
# - run rosdep for the workspace

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

# Interactive non-login shells use ~/.bashrc; tmux panes launched as bash -lc
# depend on ~/.profile because ~/.bashrc returns early for non-interactive
# shells.
ensure_source_line "source /home/iii/ws/setup/setup_dev.bash" "$HOME/.bashrc"
ensure_source_line "source /home/iii/ws/setup/setup_dev.bash" "$HOME/.profile"

# Install simulation assets
if [ ! -d /home/iii/ws/PX4-Autopilot ]; then
    echo "ERROR: /home/iii/ws/PX4-Autopilot not found. Add PX4-Autopilot as a submodule before creating the devcontainer."
    exit 1
fi

# The devcontainer uses PX4 SITL/Gazebo only. Skip the NuttX ARM firmware
# toolchain; it is large and not needed for companion-computer development.
/bin/bash /home/iii/ws/PX4-Autopilot/Tools/setup/ubuntu.sh --no-nuttx

./src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh /home/iii/ws/PX4-Autopilot

# Rosdep install
rosdep update && rosdep install --from-paths src --ignore-src -y

# Some devcontainer lifecycle steps and VS Code helper commands may run as root.
# Normalize the writable runtime/config paths back to the container user so live
# parameter snapshots and other runtime artifacts can always be persisted.
ensure_workspace_runtime_ownership
