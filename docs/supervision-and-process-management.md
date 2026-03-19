# Supervision And Process Management

## 1. Purpose

`iii_drone_supervision` provides the workspace runtime-management plane. It combines:

- the canonical system launch graph
- lifecycle dependency handling
- managed external-process wrappers
- a background daemon used by the III CLI

The preferred operational path is now the daemon-owned system manager. The older standalone `supervisor_node.py` path remains in the package for compatibility and direct ROS-facing workflows.

## 2. Main Components

1. `system_spec.py`
- Canonical description of the runtime graph.
- Defines system entities, profile membership, lifecycle metadata, and dependency edges.

2. `launch/system.launch.py`
- Direct ROS 2 launch entrypoint for the canonical graph.

3. `system_manager.py`
- Owns `LaunchService`.
- Tracks process start/exit state.
- Instantiates the `Supervisor`.
- Serves boot/start/stop/restart/shutdown/status operations.

4. `system_daemon.py`
- Background Unix-socket daemon wrapping `SystemManager`.
- Primary integration surface for `iii system ...`.

5. `supervisor.py`
- Lifecycle dependency engine.
- Builds transition trees and performs ordered managed-node operations.

6. `supervisor_node.py`
- Legacy ROS-node wrapper around `Supervisor`.

7. `managed_node_wrapper.py` + `managed_process.py`
- Wrap arbitrary shell commands/processes or nested launch fragments into lifecycle-compatible control points.

## 3. Supervision Configuration Model

There are now two related configuration layers.

### 3.1 Canonical system specification

The preferred source of runtime truth is:

- `iii_drone_supervision/system_spec.py`

For each entity it defines:

- stable `entity_id`
- launch factory
- profile membership
- optional `ManagedNodeSpec`
- respawn policy

For each managed node, lifecycle metadata includes:

- `node_name`
- `node_namespace`
- optional `config_depend`
- optional `active_depend`

This system specification can generate the supervision model consumed by `Supervisor`.

### 3.2 Legacy supervision YAML

Files still present:

- `supervision_config/sim.yaml`
- `supervision_config/real.yaml`
- `supervision_config/opti_track.yaml`

These remain relevant for the older supervisor-centric path and as compatibility artifacts, but they are no longer the preferred top-level definition of the full system topology.

## 4. Dependency Graph Behavior

Supervisor-enforced lifecycle behavior still includes:

- a node must only be activated when specific dependencies are active/configured.
- stop/shutdown operations can optionally ignore dependencies.
- selected-node operations support partial system control.

The important architectural shift is that lifecycle dependencies are now declared alongside the canonical launch graph rather than in a separate runtime-control source of truth.

## 5. Managed Process Configurations

`node_management_config/*.yaml` still defines wrapped command processes, for example:

- `cable_camera.yaml` -> usb_cam command
- `micro_ros_agent.yaml` -> micro ROS agent
- `sensors_sim_launch.yaml` -> simulation sensor launch manager
- `tf_real_launch.yaml` / `tf_sim_launch.yaml` -> TF launch managers

Each config contains:
- command
- working directory
- monitor command definitions (typically topic checks)
- monitor/start timeout periods

These wrappers remain useful where:

- the wrapped executable is not lifecycle-native
- a nested launch fragment should appear as one managed entity
- compatibility with existing process-monitoring behavior is still needed

## 6. System-Level Effect

The current supervision layer turns the canonical launch graph into a managed, dependency-aware runtime.

Without it, startup sequencing and error recovery would become manual and fragile.

## 7. Practical Bringup Semantics

Current preferred workflow:

1. `iii system boot`
   - ensures the daemon is running
   - boots the canonical launch graph for the selected profile
   - returns a tmux session description to the CLI

2. CLI creates a tmux session
   - one pane typically runs `iii system status --watch`
   - other panes tail per-entity logs via `iii system logs <entity_id> --follow`

3. `iii system start`
   - configures and activates managed nodes in dependency order

This means:

- launch is the canonical source of process existence
- the daemon is the canonical source of process status and control
- supervision logic remains the source of lifecycle ordering
- tmux is an operator visibility layer, not the startup mechanism

Environment and profile selection are controlled by `setup/*.bash`, the system profile resolution in `system_spec.py`, and the CLI/daemon handshake.
