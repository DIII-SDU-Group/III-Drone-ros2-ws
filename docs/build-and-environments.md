# Build And Environments

This workspace is intentionally containerized across the full lifecycle:
- development (`Dockerfile.dev` + devcontainer)
- deployment/runtime (`Dockerfile`)
- cross-compilation (`Dockerfile.cc`)

## 1. Build System

Primary build system: `colcon` with workspace defaults (`defaults.yaml`).

Observed default behavior:
- Base path: `src`
- Skip regex: `example_*`

Typical build command pattern in scripts:
- `colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`

## 2. Environment Profiles (`setup/*.bash`)

The workspace defines explicit runtime modes via shell profiles:

1. `setup_dev.bash`
- Sets `SIMULATION=true`
- Uses sim supervisor config (`sim.yaml`)
- Loads paths, remote settings, log levels, ROS middleware variables
- Sets `COLCON_HOME` to workspace

2. `setup_real.bash`
- Intended for deployment/runtime on target platform (arm64 sysroot layout present)
- Sets `SIMULATION=false`
- Uses real supervisor config (`real.yaml`)
- Sources installed setup from `/arm64-sysroot/...`

3. `setup_remote.bash`
- Remote tooling profile for deployment/SSH workflow.

Shared env and path conventions:
- `CONFIG_BASE_DIR`
- `NODE_MANAGEMENT_CONFIG_DIR`
- `SUPERVISOR_CONFIG_DIR`
- `MISSION_SPECIFICATION_DIR`
- `BEHAVIOR_TREES_DIR`
- `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`

## 3. Containerization

### 3.1 Devcontainer

`.devcontainer/devcontainer.json` points to `Dockerfile.dev` and mounts:
- workspace into `/home/iii/ws`
- X11 sockets/authority for GUI apps
- docker socket (docker-outside-of-docker)
- host network/IPC/PID and privileged mode
- GPU runtime settings (NVIDIA)

Post hooks:
- `scripts/post_create.sh`
- `scripts/post_start.sh`

### 3.2 Dockerfile Layers

1. `Dockerfile.dev`
- ROS Humble desktop-full base
- installs dependencies via `III-Drone-Core/scripts/install_dependencies.sh`
- installs Gazebo ros_gz Garden package set
- installs Python requirements
- installs QGroundControl AppImage + dev tools

2. `Dockerfile`
- Runtime/base image with ROS Humble base
- workspace and CLI installation
- intended for real/containerized deployment flows

3. `Dockerfile.cc`
- Cross-compilation oriented image
- arm64 sysroot composition and toolchain setup

## 4. Entrypoints

- `entrypoint_dev.sh`: source ROS + workspace install if exists.
- `entrypoint_real.sh`: source arm64 sysroot bash + `setup_real.bash`.
- `entrypoint_cc.sh`: source arm64 ROS path.

## 5. Dependency Installation Strategy

Multiple layers:
- apt dependencies scripted under `III-Drone-Core/scripts/install_dependencies.sh`
- Python dependencies via top-level `requirements.txt`
- package-level CMake dependencies and Python package setup
- workspace post-create script runs `rosdep install --from-paths src --ignore-src -y`

## 6. Operational Tooling

Workspace scripts provide utility for:
- package/executable discovery
- remote install/setup
- devcontainer startup behavior
- docker compose builds (legacy path references in script)

Operational bringup typically uses III CLI commands after environment profile sourcing, rather than relying on a single direct launch file.

## 7. Build/Runtime Observations

- Build graph includes external fetched dependencies (notably `yaml-cpp` via CMake `FetchContent` in config and mission packages).
- Runtime heavily depends on correct environment variable initialization before launch.
- Configuration files under `CONFIG_BASE_DIR/iii_drone` are first-class runtime dependencies, not optional.
