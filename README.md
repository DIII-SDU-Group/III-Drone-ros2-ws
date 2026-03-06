# III-Drone ROS2 Workspace

Integrated ROS2 workspace for the III-Drone autonomous powerline robotics stack.

This repository is the system-integration layer that ties together core autonomy packages, PX4 interfaces, supervision, simulation, ground control tooling, and deployment workflows.

## What This Repo Is

- A top-level ROS2 workspace (`src/`) with multiple sub-repositories.
- The canonical integration environment used by the team.
- The place where boot/setup/tooling/dependency governance are managed.

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
- `scripts/`: build/install/dev helper scripts
- `tools/`: CLI and tmuxinator layouts
- `deps/`: dependency lock files
- `docs/`: project architecture and engineering documentation

## Canonical Bringup Model

Current team workflow is intentionally research/test oriented:

1. Load the correct environment profile from `setup/*.bash`.
2. Boot processes via III CLI (`iii system boot`) into tmux layouts.
3. Use supervisor actions to configure/activate/manage lifecycle states.

Supervisor orchestrates node state transitions and dependencies, while CLI/tmux handles process boot transparency and control.

## Quick Start (Development/Simulation)

```bash
# From workspace root
source setup/setup_dev.bash

# Build
colcon build --symlink-install

# Boot system (canonical flow)
iii system boot
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
- Verify: `./scripts/verify_submodule_lock.sh`
- Update intentionally: `./scripts/update_submodule_lock.sh`

CI enforces lock consistency on PRs/pushes.

## Documentation

Start here for detailed technical documentation:

- [Workspace Docs Index](docs/README.md)
- [Workspace Overview](docs/workspace-overview.md)
- [Runtime Launch And Node Graph](docs/runtime-launch-and-node-graph.md)
- [Dependency Governance](docs/dependency-governance.md)
- [Repository Boundary Map](docs/repo-boundary-map.md)

## Status

This codebase was built in active research and is being hardened for multi-developer team use while continuing feature development.

That means ongoing priorities are:
- robustness and reproducibility
- clearer ownership and release discipline
- preserving operator transparency during testing
