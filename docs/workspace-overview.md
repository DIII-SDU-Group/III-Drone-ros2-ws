# Workspace Overview

## 1. What This Repository Is

This repository is a ROS2 workspace-level meta-repository for an autonomous drone system focused on powerline interaction (inspection/manipulation-like behavior including cable approach/landing/takeoff workflows).

It combines:
- A top-level ROS2 workspace (`src/` with many packages/submodules).
- A local PX4 firmware tree (`PX4-Autopilot/`) for simulation and firmware coupling.
- Containerized dev/deploy/cross-compilation workflows.
- Runtime supervision, mission behavior trees, perception, control, payload, and ground-control components.

## 2. Top-Level Structure

Key top-level directories:
- `src/`: ROS2 packages (mostly git submodules).
- `PX4-Autopilot/`: PX4 firmware tree (separate git repo).
- `setup/`: environment bootstrap scripts and runtime profile settings.
- `scripts/`: workspace helper scripts (build/install/remote/devcontainer hooks).
- `tools/`: CLI and operator tool integrations.
- `testing/`, `data_analysis/`, `rosbags/`, `runtime_logs/`: evaluation artifacts and analysis tooling.
- `Dockerfile*`, `.devcontainer/`: runtime and development container environments.

Build/runtime artifacts present in workspace:
- `build/`, `install/`, `log/` (colcon outputs).

## 3. Versioning/Repository Model

The workspace uses git submodules declared in `.gitmodules` and pinned to mostly internal DIII-SDU-Group branches/tags (many `v2.2` variants).

Important consequence:
- System consistency depends on synchronized branch/tag selection across core, mission, interfaces, simulation, supervision, px4_msgs, micro-ROS components, and PX4 firmware.

## 4. Major Functional Domains

The system decomposes into these runtime domains:
- Configuration domain: central parameter server, parameter declaration/distribution, config file management.
- Supervision domain: lifecycle/process orchestration with dependency graph for node bringup/bringdown.
- Perception domain: camera + mmWave-based powerline perception stack.
- Control domain: trajectory generation + maneuver execution/action servers.
- Mission domain: behavior tree execution + PX4 mode ownership/registration.
- Payload domain: charger/gripper interactions.
- Simulation domain: Gazebo bridge and synthetic sensor transformations.
- Operator domain: Ground Control GUI and CLI tooling.

## 5. Primary Runtime Pattern

Operationally, the architecture is centered on:
1. A canonical system specification in `III-Drone-Supervision` declares the runtime graph and profile-conditioned differences.
2. A background supervision daemon instantiates that graph through ROS 2 launch and tracks process state.
3. The daemon uses supervision logic to configure/activate managed nodes according to dependency constraints.
4. Configuration services provide parameter values and runtime updates to participating nodes.
5. Perception publishes environmental state (powerline estimates, orientations).
6. Control exposes maneuver action servers and reference services.
7. Mission layer runs behavior trees that orchestrate maneuver actions and system service calls.
8. PX4 interface layer registers and executes offboard mode behavior.

## 6. External Integrations

Core external integrations include:
- ROS2 Humble.
- PX4 (DDS/uORB via `px4_msgs`, micro-ROS agent, and px4_ros2 interface lib).
- Gazebo (Garden path in docs/scripts; also signs of older/classic references in readmes/scripts).
- CycloneDDS middleware configuration.
- USB camera + mmWave sensor pipeline.

## 7. Architectural Character

This codebase is not a simple single-package ROS app. It is a full system stack with:
- Mixed C++ + Python ROS2 nodes.
- Lifecycle nodes and standard nodes coexisting.
- Action/service-heavy orchestration.
- A launch-driven runtime graph with daemon-managed operator workflows.
- Subsystem-level modularity but strong cross-package coupling through shared interfaces and configuration keys.
