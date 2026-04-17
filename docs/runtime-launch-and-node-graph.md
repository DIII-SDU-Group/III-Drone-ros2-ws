# Runtime Launch And Node Graph

## 1. Canonical Bringup Model

Canonical operational entrypoint is the III CLI (`iii`), backed by the supervision daemon and a launch-driven runtime graph.

Operational sequence:
1. Environment profile is loaded from `setup/*.bash` (for example dev/sim profile).
2. `iii system boot` ensures the system daemon is running.
3. The daemon resolves the selected profile from `iii_drone_supervision/system_spec.py`.
4. The daemon instantiates the canonical `LaunchDescription` through ROS 2 launch.
5. The daemon prepares daemon-managed services from the same system specification.
6. The CLI requests the tmux view model from `iii_drone_supervision/tmux_spec.py` and creates the operator session.
7. `iii system start` starts required services and performs lifecycle-style configure/activate control over managed nodes.

Important distinction:
- ROS 2 launch is the canonical source of process existence.
- The system daemon owns non-lifecycle system services such as `micro_ros_agent`.
- Supervision logic orchestrates lifecycle transitions and dependency ordering.
- tmux is an operator view derived from the system specification family, not the process-spawn source of truth.

Direct launch without the daemon remains available:

```bash
ros2 launch iii_drone_supervision system.launch.py profile:=sim
```

That path launches the same canonical launch graph but does not provide daemon-managed services, service readiness gates, socket control, or tmux automation.

## 2. Launch Group Composition

The authoritative launch topology comes from the canonical system specification in:

- `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py`
- `src/III-Drone-Supervision/launch/system.launch.py`

The model is organized as:

- `common entities`
  Present in all profiles, for example configuration, payload, perception, control, and mission nodes.

- `common services`
  Daemon-owned non-lifecycle processes present in the selected profile, for example `micro_ros_agent`.

- `profile entities`
  Present only in selected profiles, for example simulation sensor launch wrappers or hardware sensor nodes.

- `profile dependency overrides`
  Used where the same subsystem exists in both sim and real, but depends on different upstream entities.

Examples from the specification:

### 2.1 Common entities

- `/configuration/configuration_server/configuration_server`
- `/payload/charger_gripper/charger_gripper`
- `/perception/hough_transformer/hough_transformer`
- `/perception/pl_dir_computer/pl_dir_computer`
- `/perception/pl_mapper/pl_mapper`
- `/control/trajectory_generator/trajectory_generator`
- `/control/maneuver_controller/maneuver_controller`
- `/mission/powerline_overview_provider/powerline_overview_provider`
- `/mission/mission_executor/mission_executor`

### 2.2 Simulation profile entities

- managed TF simulation launch wrapper (`tf`)
- managed simulation sensor launch wrapper (`sensors`)

### 2.3 Common profile services

- `micro_ros_agent`

### 2.4 Real / OptiTrack profile entities

- managed TF real launch wrapper (`tf`)
- managed cable camera wrapper (`cable_camera`)
- `/sensor/mmwave/mmwave`

## 3. Node Categories

### 3.1 Core Perception Nodes
- `hough_transformer`: image-based cable orientation extraction.
- `pl_dir_computer`: direction/orientation estimation fusion.
- `pl_mapper`: line mapping/stateful powerline estimate manager.

### 3.2 Core Control Nodes
- `trajectory_generator`: reference trajectory generation (MPC interactions present).
- `maneuver_controller`: action servers for maneuver primitives and scheduling.
- `drone_frame_broadcaster`: publishes frame transform/heartbeat status.

### 3.3 Mission Nodes
- `mission_executor` (lifecycle): BT and mode orchestration.
- `powerline_overview_provider` (lifecycle): stores and serves powerline overviews.

### 3.4 Configuration Nodes
- `configuration_server`: Python configuration server.
- `configuration_client`: operator/developer utility.

### 3.5 Supervision Nodes
- `system_daemon`: background system-manager process exposed over a Unix socket.
- `system_manager`: daemon-owned runtime controller using ROS 2 launch and supervision logic.
- `service_manager`: daemon-owned process control and readiness monitoring for services such as `micro_ros_agent`.
- `supervisor`: lifecycle dependency engine used internally by the system manager.
- `managed_node_wrapper`: lifecycle wrapper for external processes and nested launch fragments.

### 3.6 Ground Control
- `iii_gc` node inside GUI process for telemetry, command, and parameter interactions.

## 4. Communication Patterns

System uses mixed ROS patterns:
- Actions for long-running maneuvers/supervision operations.
- Services for command/control and parameter management.
- Topics for state/telemetry/perception/control reference propagation.

Critical data links:
- PX4 odometry (`/fmu/out/vehicle_odometry`) into control/mission.
- PX4 FMU status (`/fmu/out/vehicle_status_v1`) as the `micro_ros_agent` readiness heartbeat.
- Perception outputs into maneuver logic and mission condition nodes.
- Configuration services consumed by almost all higher-level components.

## 5. Lifecycle And Bringup Pattern

The codebase uses both:
- ROS lifecycle nodes directly.
- Non-lifecycle processes wrapped into lifecycle-manageable wrappers by supervision layer.

Result:
- Unified lifecycle-oriented control semantics over heterogeneous node implementations.

At the process level, the canonical path is launch-driven:

- launch defines what exists
- the daemon tracks which launched processes are alive
- the daemon owns service processes that are not lifecycle nodes
- supervision logic decides which managed nodes may be configured/activated

`mission_executor` is gated by `micro_ros_agent: ready`. The micro-ROS agent service may be alive while PX4 is absent; readiness follows configured FMU topic heartbeats. This supports starting the III system before PX4 SITL or the physical flight controller is available, then bringing PX4 online later and rerunning `iii system start`.

## 6. Runtime Topology Summary

Typical complete system topology (sim or real profile dependent):
1. CLI boot ensures the supervision daemon is alive.
2. Daemon launches the canonical system graph for the selected profile.
3. Daemon prepares daemon-managed services such as `micro_ros_agent`.
4. CLI creates a tmux session whose panes observe logs and status.
5. `iii system start` starts required services and activates lifecycle nodes whose dependencies are available.
6. TF and sensor ingress are brought to active state via managed nodes.
7. Perception chain stabilizes powerline state.
8. Control primitives become available via action servers.
9. Mission executor registers PX4 modes and drives behavior trees when PX4 readiness is present.
10. Ground control observes status and injects operator commands/parameter changes.
