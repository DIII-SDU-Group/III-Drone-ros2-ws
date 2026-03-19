# III-Drone ROS2 Workspace

Integrated ROS2 workspace for the III-Drone autonomous powerline robotics stack.

This repository is the system-integration layer that ties together core autonomy packages, PX4 interfaces, supervision, simulation, ground control tooling, and deployment workflows.

## What This Repo Is

- A top-level ROS2 workspace (`src/`) with multiple sub-repositories.
- The canonical integration environment used by the team.
- The place where boot/setup/tooling/dependency governance are managed.
- Intended for day-to-day development in VS Code using the provided devcontainer.

Core software remains modular in separate repos (core, interfaces, mission, supervision, etc.), while this repo owns integrated workflows.

## High-Level Architecture

The stack is organized into the following domains:

- `configuration`: parameter server, schema validation, runtime profile switching
- `supervision`: dependency-aware lifecycle orchestration
- `perception`: camera/mmWave processing and powerline mapping
- `control`: trajectory generation and maneuver action servers
- `mission`: behavior-tree execution and PX4 mode integration
- `simulation`: Gazebo/PX4 simulation bridges and assets
- `ground control`: GUI + operator commands + live parameter interaction

## Repository Layout

- `src/`: ROS2 packages and submodules
- `setup/`: environment profiles (`dev`, `real`, `remote`) and runtime vars
- `scripts/`: categorized workspace tooling (`ci/`, `git/`, `remote/`, `workspace/`)
- `tools/`: CLI and tmuxinator layouts
- `deps/`: dependency lock files
- `docs/`: project architecture and engineering documentation

## Containerized Environments

This project is containerized for both development and deployment workflows.

- Dev container: `.devcontainer/devcontainer.json` using `Dockerfile.dev`
- Runtime/deployment container: `Dockerfile`
- Cross-compilation container: `Dockerfile.cc`
- Entrypoints: `entrypoint_dev.sh`, `entrypoint_real.sh`, `entrypoint_cc.sh`

This allows consistent tooling, dependencies, and ROS/PX4 integration across developer machines and robot-oriented environments.

## Canonical Bringup Model

Current team workflow is centered on the III CLI and the supervision daemon:

1. Load the correct environment profile from `setup/*.bash`.
2. Boot the runtime through III CLI (`iii system boot`).
3. The CLI ensures the background system-manager daemon is running.
4. The daemon launches the canonical ROS 2 system graph for the selected profile.
5. The CLI creates a tmux session from a separate tmux view specification.
6. Use `iii system start` / `stop` / `restart` / `status` for lifecycle-aware operations.

The canonical runtime graph now lives in `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py`.
Direct unmanaged launch is still supported through:

```bash
ros2 launch iii_drone_supervision system.launch.py profile:=sim
```

The supervision daemon adds process tracking, lifecycle orchestration, CLI integration, and derived tmux views on top of that graph.

## Quick Start (Development/Simulation)

```bash
# Run these inside the VS Code devcontainer terminal

# Boot system (canonical flow)
iii system boot

# Attach to running tmux session
iii system attach
```

## Branching And Stability

Recommended working model:

- `main`: stable, deployable
- `develop`: active integration
- `staging`: optional pre-release hardening
- feature branches: all development work
- tags: immutable robot-deployable snapshots

## Dependency Governance

Submodule references are governed by a lock file:

- Lock file: `deps/submodule-lock.txt`
- Verify: `./scripts/git/verify_submodule_lock.sh`
- Update intentionally: `./scripts/git/update_submodule_lock.sh`

CI enforces lock consistency on PRs/pushes.

## Documentation

Start here for detailed technical documentation:

- [Workspace Docs Index](docs/README.md)
- [Workspace Overview](docs/workspace-overview.md)
- [Runtime Launch And Node Graph](docs/runtime-launch-and-node-graph.md)
- [Supervision And Process Management](docs/supervision-and-process-management.md)
- [Dependency Governance](docs/dependency-governance.md)
- [Repository Boundary Map](docs/repo-boundary-map.md)

## Status

This codebase was built in active research and is being hardened for multi-developer team use while continuing feature development.

That means ongoing priorities are:
- robustness and reproducibility
- clearer ownership and release discipline
- preserving operator transparency during testing
