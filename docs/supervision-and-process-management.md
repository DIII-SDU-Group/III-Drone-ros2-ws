# Supervision And Process Management

## 1. Purpose

`iii_drone_supervision` provides the workspace runtime-management plane. It combines:

- the canonical system launch graph
- daemon-managed system services
- lifecycle dependency handling
- managed external-process wrappers
- a background daemon used by the III CLI

## 2. Main Components

1. `system_spec.py`
- Canonical description of the runtime graph.
- Defines system entities, daemon-managed services, profile membership, lifecycle metadata, and dependency edges.

2. `launch/system.launch.py`
- Direct ROS 2 launch entrypoint for the canonical graph.

3. `system_manager.py`
- Owns `LaunchService`.
- Owns daemon-managed services.
- Tracks process start/exit state.
- Instantiates the `Supervisor`.
- Serves boot/start/stop/restart/shutdown/status operations.

4. `system_daemon.py`
- Background Unix-socket daemon wrapping `SystemManager`.
- Primary integration surface for `iii system ...`.

5. `supervisor.py`
- Lifecycle dependency engine.
- Builds transition trees and performs ordered managed-node operations.

6. `managed_node_wrapper.py` + `managed_process.py`
- Wrap arbitrary shell commands/processes or nested launch fragments into lifecycle-compatible control points.

7. `service_manager.py`
- Starts, stops, restarts, logs, and monitors daemon-managed services that are not ROS lifecycle nodes.

## 3. Supervision Configuration Model

There are two related configuration layers.

### 3.1 Canonical system specification

The source of runtime truth is:

- `iii_drone_supervision/system_spec.py`

For each entity it defines:

- stable `entity_id`
- launch factory
- profile membership
- optional `ManagedNodeSpec`
- respawn policy

For each daemon-managed service it defines:

- stable `service_id`
- command factory
- profile membership
- restart policy
- readiness topic checks

For each managed node, lifecycle metadata includes:

- `node_name`
- `node_namespace`
- optional `config_depend`
- optional `active_depend`
- optional `service_depend`

This system specification can generate the supervision model consumed by `Supervisor`.

### 3.2 Process-wrapper configurations

Wrapped external processes are defined by:

- `node_management_config/*.yaml`

These files describe commands, working directories, and monitor settings for managed wrapper entities.

Daemon-managed services are not process-wrapper configurations. They are declared in `system_spec.py` and owned directly by `SystemManager`.

## 4. Dependency Graph Behavior

Supervisor-enforced lifecycle behavior includes:

- a node must only be activated when specific dependencies are active/configured.
- stop/shutdown operations can optionally ignore dependencies.
- selected-node operations support partial system control.

Lifecycle dependencies are declared alongside the canonical launch graph and consumed by the runtime manager.

Service dependencies are evaluated by `SystemManager` before lifecycle transitions are requested. If a selected node requires a service that is not ready, the selected operation fails. During full-system start, nodes blocked by service readiness are left inactive while other eligible nodes are started.

## 5. Managed Process Configurations

`node_management_config/*.yaml` defines wrapped command processes, for example:

- `cable_camera.yaml` -> usb_cam command
- `sensors_sim_launch.yaml` -> simulation sensor launch manager
- `tf_real_launch.yaml` / `tf_sim_launch.yaml` -> TF launch managers

Each config contains:
- command
- working directory
- monitor command definitions (typically topic checks)
- monitor/start timeout periods

These wrappers are useful where:

- the wrapped executable is not lifecycle-native
- a nested launch fragment should appear as one managed entity
- process-level monitoring should stay inside the supervision package

`micro_ros_agent` is a daemon-managed service instead of a lifecycle wrapper. It is started through:

```bash
iii system service start micro_ros_agent
iii system service stop micro_ros_agent
iii system service restart micro_ros_agent
```

It is also started automatically by `iii system start` when the active profile includes it. Readiness is based on FMU topic heartbeats, so the process can be alive while PX4 is unavailable.

## 6. System-Level Effect

The supervision layer turns the canonical launch graph into a managed, dependency-aware runtime.

Without it, startup sequencing and error recovery would become manual and fragile.

## 7. Practical Bringup Semantics

Workflow:

1. `iii system boot`
   - starts `iii-system-daemon.service` through systemd if needed
   - boots the canonical launch graph for the selected profile
   - returns a tmux session description to the CLI

Daemon service control is available through:

```bash
iii system daemon start
iii system daemon stop
iii system daemon restart
iii system daemon status
iii system daemon logs --follow
```

2. CLI creates a tmux session
   - one pane typically runs `iii system status --watch`
   - other panes tail per-entity logs via `iii system logs <entity_id> --follow`

3. `iii system start`
   - starts required daemon-managed services
   - configures and activates managed nodes in dependency order
   - leaves service-blocked nodes inactive and reports them

This means:

- launch is the canonical source of process existence
- systemd is the process owner for the III daemon
- daemon-managed services are owned by the daemon
- the daemon is the canonical source of process status and control
- supervision logic remains the source of lifecycle ordering
- tmux is an operator visibility layer, not the startup mechanism

Environment and profile selection are controlled by `setup/*.bash`, the system profile resolution in `system_spec.py`, and the CLI/daemon handshake.
