# AGENT.md - III-Drone ROS2 Workspace Guide

This file defines how coding agents should work in this repository.

## 1) Repository Purpose

`III-Drone-ros2-ws` is the workspace-level integration repository for the III-Drone stack.
It composes multiple sub-repositories (mostly under `src/`) plus `PX4-Autopilot/`, tooling, and environment/bootstrap glue.

Treat this repo as the source of truth for:
- integration workflows
- environment setup
- dependency pinning/governance
- runtime bringup conventions

## 2) Canonical Runtime Model

Preferred bringup flow:
1. Source an environment profile from `setup/` (usually `setup/setup_dev.bash`).
2. Start runtime via III CLI and tmux layout:
   - `iii system boot`
   - `iii system attach`
3. Use supervision/configuration services for lifecycle/state management.

Do not assume direct `ros2 launch ...` alone matches operational behavior.

## 3) Environment And Build Baseline

Default dev path (inside devcontainer):
- workspace path: `/home/iii/ws`
- ROS distro target: Jazzy in devcontainer config and Dockerfile args

### 3.1 Devcontainer Build/Test Execution

Agents may run build and test commands inside the active devcontainer instead of the host shell.

Do not hardcode a container id. Discover the container from the workspace path/labels first.

Preferred discovery command from the host workspace root:
```bash
docker ps \
  --filter "label=devcontainer.local_folder=$(pwd)" \
  --format '{{.ID}}\t{{.Names}}'
```

The devcontainer config is in `.devcontainer/devcontainer.json` and the in-container workspace root is:
- `/home/iii/ws`

Preferred execution pattern:
```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon build --base-paths src --packages-select <pkg> --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
'
```

Preferred test pattern:
```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon test --base-paths src --packages-select <pkg> --ctest-args --output-on-failure
  colcon test-result --verbose
'
```

Notes:
- Only run tests for III packages. Do not run test commands for non-III third-party packages.
- Use `--base-paths src` when running `colcon` in the devcontainer to avoid package discovery in unrelated workspace directories.
- Source `/opt/ros/jazzy/setup.bash` before `colcon test`, otherwise Python-based `ament` test helpers may be missing from the environment.

Common build command:
```bash
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

Workspace defaults are in `defaults.yaml` (`src` base path, skip `example_*`).

## 4) Dependency Governance (Strict)

Submodule refs are lock-governed:
- lock file: `deps/submodule-lock.txt`
- verify: `./scripts/git/verify_submodule_lock.sh`
- update lock intentionally: `./scripts/git/update_submodule_lock.sh`

Rules:
- Do not change submodule commits casually.
- If submodule refs are intentionally changed, update and verify lock file in the same change.
- Document why each submodule bump is needed.

## 5) Submodule Edit Policy (Authoritative)

Primary workspace-owned integration areas are still safe default targets:
- `setup/`
- `scripts/`
- `tools/tmuxinator/`
- top-level docs and workflow files

For submodules, use this strict policy.

### 5.1 Main III codebase (editable when appropriate)

These are core project code and can be edited when the task needs it:
- `src/III-Drone-Core`: main control/perception runtime code.
- `src/III-Drone-Configuration`: configuration server/client and parameter model.
- `src/III-Drone-Interfaces`: ROS message/service/action contracts.
- `src/III-Drone-Mission`: mission and behavior execution layer.
- `src/III-Drone-Simulation`: simulation integration and assets glue.
- `src/III-Drone-Supervision`: supervision and lifecycle orchestration.
- `src/III-Drone-GC`: ground control/operator tooling package.
- `tools/III-Drone-CLI`: main CLI used for canonical bringup.

### 5.2 Forked open-source libraries (ask for verification first)

These are open-source libraries maintained as forks. Editing may be needed, but requires user verification first:
- `src/BehaviorTree.CPP`
- `src/BehaviorTree.ROS2`
- `src/px4-ros2-interface-lib`
- `src/iwr6843aop-ROS2-pkg`

Rule: before changing any of these three, pause and ask for explicit verification.

### 5.3 Third-party dependencies (do not edit by default)

Everything else is considered third-party and should not be edited unless there is a strong technical reason, then ask first:
- `PX4-Autopilot`
- `src/Micro-XRCE-DDS-Agent`
- `src/dynamic_message_introspection`
- `src/micro-ROS-Agent`
- `src/micro_ros_msgs`
- `src/px4_msgs`

Recursive/nested third-party submodules (for example under `PX4-Autopilot` and `src/III-Drone-Simulation`) are also no-touch by default.

`build/`, `install/`, and `log/` are generated artifacts: do not hand-edit.

## 6) Configuration And Runtime Assumptions

Runtime expects environment variables and config layout from `setup/paths.bash`, especially:
- `CONFIG_BASE_DIR`
- `NODE_MANAGEMENT_CONFIG_DIR`
- `SUPERVISOR_CONFIG_DIR`
- `MISSION_SPECIFICATION_DIR`
- `BEHAVIOR_TREES_DIR`

Bringup often depends on installed config content under `.config/iii_drone`.
If config-dependent behavior fails, verify setup/install scripts were run.

## 7) Agent Work Protocol

When implementing changes:
1. Read relevant local docs first:
   - `README.md`
   - `docs/README.md`
   - `docs/runtime-launch-and-node-graph.md`
   - `docs/build-and-environments.md`
   - `docs/dependency-governance.md`
2. Prefer minimal diffs and keep behavior consistent with CLI-first bringup.
3. Validate with the smallest meaningful command set for the touched area.
4. Report any observed inconsistencies instead of silently “fixing” architecture.

## 8) Validation Checklist

Use as applicable:
```bash
# Dependency integrity
./scripts/git/verify_submodule_lock.sh

# Build (full)
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

# Build (targeted)
colcon build --packages-select <pkg_name> --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug
```

If runtime-related:
```bash
source setup/setup_dev.bash
iii system boot
```

## 9) Known Project Risks To Keep In Mind

Current docs flag these active risks:
- launch-path inconsistencies between some launch files and supervision-managed flows
- tight cross-package coupling via shared interfaces/config
- potential confusion from legacy simulation naming/history
- fragility when env/config setup is incomplete

Agents should preserve stability and avoid broad refactors unless explicitly requested.
