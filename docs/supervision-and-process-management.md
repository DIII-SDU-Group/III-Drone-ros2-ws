# Supervision And Process Management

## 1. Purpose

`iii_drone_supervision` provides system bringup/teardown control as an orchestration plane over lifecycle nodes and wrapped external processes.

## 2. Main Components

1. `supervisor_node.py`
- ROS node exposing action servers:
  - `supervisor/start`
  - `supervisor/stop`
  - `supervisor/restart`
  - `supervisor/shutdown`
- Exposes `supervisor/get_managed_nodes` service.
- Delegates orchestration logic to `Supervisor` class.

2. `supervisor.py`
- Loads supervision YAML.
- Constructs dependency-aware transition tree.
- Manages `ManagedNodeClient` instances.
- Performs bringup/bringdown transitions with optional selected-node scopes.

3. `managed_node_wrapper.py` + `managed_process.py`
- Wrap arbitrary shell commands/processes into managed lifecycle-compatible control points.
- Provide monitor hooks via topic checks and process health logic.

## 3. Supervision Configuration Model

Files:
- `supervision_config/sim.yaml`
- `supervision_config/real.yaml`
- `supervision_config/opti_track.yaml`

Top-level fields:
- `monitor_period_ms`
- `request_state_timeout_ms`
- `max_threads`
- `managed_nodes` map

Each managed node defines:
- identity (`node_name`, `node_namespace`)
- optional dependencies:
  - `config_depend`
  - `active_depend`

## 4. Dependency Graph Behavior

Supervisor enforces dependency state requirements such as:
- a node must only be activated when specific dependencies are active/configured.
- stop/shutdown operations can optionally ignore dependencies.
- selected-node operations support partial system control.

This gives deterministic bringup order and safer subsystem restarts.

## 5. Managed Process Configurations

`node_management_config/*.yaml` defines wrapped command processes, e.g.:
- `cable_camera.yaml` -> usb_cam command
- `micro_ros_agent.yaml` -> micro ROS agent
- `sensors_sim_launch.yaml` -> simulation sensor launch manager
- `tf_real_launch.yaml` / `tf_sim_launch.yaml` -> TF launch managers

Each config contains:
- command
- working directory
- monitor command definitions (typically topic checks)
- monitor/start timeout periods

## 6. System-Level Effect

Supervision layer is the operational control backbone. It turns many independent ROS nodes/launch files into a managed, dependency-aware system lifecycle.

Without it, startup sequencing and error recovery would become manual and fragile.

## 7. Practical Bringup Semantics (Current Project Workflow)

Current project workflow is intentionally test/research oriented with high operator transparency:
- Runtime processes are generally booted via III CLI (`iii system boot`) using tmux session definitions.
- Supervisor then manages lifecycle transitions and dependency-aware state changes.

This means:
- Nodes/process wrappers must be running and addressable for supervisor lifecycle control to succeed.
- Supervisor is lifecycle/state orchestration, not the sole process-spawn mechanism in the canonical workflow.

Environment and profile selection are controlled by `setup/*.bash` and corresponding supervision config files.
