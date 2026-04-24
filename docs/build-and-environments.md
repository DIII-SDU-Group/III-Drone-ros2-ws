# Build And Environments

The workspace uses the devcontainer as the development OS image. Onboard runtime is native Linux with ROS 2 processes owned by the III daemon. The devcontainer boots systemd and runs the III daemon as a system service so development commands exercise the same runtime ownership model.

Container images remain useful for:
- development (`Dockerfile.dev` + devcontainer)
- dependency/bootstrap reference (`Dockerfile`)
- cross-compilation experiments (`Dockerfile.cc`)

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
- Sets `III_SYSTEM_PROFILE=sim`
- Loads paths, remote settings, log levels, ROS middleware variables
- Sets `COLCON_HOME` to workspace

2. `setup_real.bash`
- Intended for native deployment/runtime on the target platform
- Sets `SIMULATION=false`
- Sets `III_SYSTEM_PROFILE=real`
- Sources the installed ROS/workspace setup expected on the target OS

3. `setup_remote.bash`
- Remote tooling profile for deployment/SSH workflow.

Shared env and path conventions:
- `CONFIG_BASE_DIR`
- `NODE_MANAGEMENT_CONFIG_DIR`
- `MISSION_SPECIFICATION_DIR`
- `BEHAVIOR_TREES_DIR`
- `III_SYSTEM_RUNTIME_DIR`
- `III_SYSTEM_DAEMON_SOCKET`
- `III_SYSTEM_DAEMON_LOG`
- `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`

## 3. Development Container

`.devcontainer/devcontainer.json` points to `Dockerfile.dev` and mounts:
- workspace into `/home/iii/ws`
- X11 sockets/authority for GUI apps
- docker socket (docker-outside-of-docker)
- host network/IPC and privileged mode
- GPU runtime settings (NVIDIA)

`.dockerignore` keeps generated build outputs, PX4 checkouts/builds, logs, runtime state, and VCS metadata out of the image build context. Those files are still available in the running devcontainer through the workspace bind mount.

The devcontainer runs container-local systemd as PID 1. VS Code terminals still use the `iii` user through `remoteUser`, while the container process itself starts as root so systemd can run normally.

Post hooks:
- `.devcontainer/post_create.sh`
- `.devcontainer/post_start.sh`

`post_start.sh` builds the workspace, installs `tools/systemd/iii-system-daemon.service` into `/etc/systemd/system`, enables it, and restarts it.

## 4. Dockerfile Layers

1. `Dockerfile.dev`
- ROS Jazzy desktop-full base
- configures apt retries/timeouts for transient mirror failures
- rewrites Ubuntu archive/security apt sources to HTTPS while leaving ROS package sources on HTTP
- installs stable OS, ROS, and development tooling in an early apt layer using `--no-install-recommends`
- installs Python requirements
- installs QGroundControl AppImage + dev tools
- installs GUI/simulation operator packages and workspace ROS/runtime package dependencies in late apt layers so package additions do not invalidate the expensive stable layers

2. `Dockerfile`
- Runtime/base image with ROS Jazzy base
- workspace and CLI installation reference
- not the primary onboard process-supervision boundary

3. `Dockerfile.cc`
- Cross-compilation oriented image
- arm64 sysroot composition and toolchain setup

## 5. Entrypoints

- `entrypoint_dev.sh`: source ROS + workspace install if exists.
- `entrypoint_real.sh`: source target runtime setup + `setup_real.bash`.
- `entrypoint_cc.sh`: source arm64 ROS path.

## 6. Dependency Installation Strategy

Dependency sources:
- stable apt dependencies are listed directly in the Dockerfiles
- workspace ROS/runtime apt dependencies are listed directly in late Dockerfile layers
- Python dependencies via top-level `requirements.txt`
- package-level CMake dependencies and Python package setup
- workspace post-create script runs PX4's setup script for simulation tooling with the NuttX firmware toolchain disabled, installs Gazebo assets, then runs `rosdep install --from-paths src --ignore-src -y`

## 7. Operational Tooling

Workspace scripts provide utility for:
- package/executable discovery
- remote install/setup
- devcontainer startup behavior
- docker compose builds

Operational bringup typically uses III CLI commands after environment profile sourcing, rather than relying on a single direct launch file.

On the real drone, the deployment repository should install a native `systemd` unit for the III daemon. Inside the devcontainer, the workspace installs the dev unit automatically and `iii system boot` uses `systemctl start iii-system-daemon.service`.

Runtime ownership is:

- native `systemd` owns the III daemon onboard and in the devcontainer
- the III daemon owns ROS launch processes and daemon-managed services
- PX4 hardware, PX4 SITL/Gazebo, and QGroundControl are external to III supervision

## 8. Build/Runtime Observations

- Build graph includes external fetched dependencies (notably `yaml-cpp` via CMake `FetchContent` in config and mission packages).
- Runtime heavily depends on correct environment variable initialization before launch.
- Configuration files under `CONFIG_BASE_DIR/iii_drone` are first-class runtime dependencies, not optional.
