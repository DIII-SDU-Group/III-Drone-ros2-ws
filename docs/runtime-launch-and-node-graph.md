# Runtime Launch And Node Graph

## 1. Canonical Bringup Model

Canonical operational entrypoint is the III CLI (`iii`), backed by the supervision daemon and a launch-driven runtime graph.

Inside the SIM devcontainer or onboard Pi, `iii` operates on the local
runtime after sourcing its `setup/` profile. The [native ground-computer
install](ground-computer-installation.md) routes the same runtime command over
Docker to the matching SIM devcontainer or over SSH to a selected Pi. Native
GC routing checks CLI source identity before execution; `iii-dev` owns only
SIM/HIL stack composition and container helpers.

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

For real inspection operation, use the staged startup, manual overview capture,
mission, takeover, and shutdown sequence in the authoritative
[`field-inspection-operations.md`](field-inspection-operations.md). Direct ROS
launch and simulation fixture staging are not field operator procedures.

## 2. Launch Group Composition

The authoritative launch topology comes from the canonical system specification in:

- `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py`
- `src/III-Drone-Supervision/launch/system.launch.py`

The model is organized as:

- `common entities`
  Present in the full profiles, for example configuration, payload, perception, control, and mission nodes. The reduced `opti_track` profile keeps only the subset listed in section 2.5.

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
- managed simulation asset launch wrapper (`sim_assets`)

### 2.3 Common profile services

- `micro_ros_agent`

In split-host HIL, `micro_ros_agent` owns the workstation PX4 SITL endpoint on
Pi UDP port 8890 and is the readiness gate for the mission graph. The physical
PX4 transport is deliberately not started in this profile: it is not part of
the virtual-flight proof and would consume Pi capacity. Real and OptiTrack
profiles own their physical PX4 transport separately: their agent listens on
Pi UDP port 8888 for the flight controller on the Ethernet link.

Every aircraft profile (`hil`, `real`, `opti_track`) runs the stack in the ROS
domain provisioned in `/etc/iii/runtime.env` (`iii_ros_domain_id`, default 42)
with Fast DDS over UDPv4. The agent creates PX4's DDS participant in the domain
that PX4's `UXRCE_DDS_DOM_ID` selects, so that parameter must equal the
provisioned domain. The onboard `setup/setup_*.bash` profiles import the same
settings.

### 2.4 Real profile entities

- managed TF real launch wrapper (`tf`)
- managed cable camera wrapper (`cable_camera`)
- `/sensor/mmwave/mmwave`

### 2.5 OptiTrack reduced profile

`opti_track` is a reduced flight-basics graph for the OptiTrack lab, where there
is no cable. It runs with and without the payload mounted:

- `configuration_server`, `tf`, `trajectory_generator`, `maneuver_controller`,
  `rosbag_recorder`, `mission_executor`, and `custom_operation`
- daemon services: `micro_ros_agent` (UDP 8888) and the motion-capture pose
  relay (`opti_track_pose_relay`)

There is no payload node, perception chain, overview provider, cable camera, or
mmWave node. The relay subscribes to the lab gateway's
`/body_splitter/body_<id>/pose` in the lab ROS domain (0) and publishes PX4
external vision on `/fmu/in/vehicle_visual_odometry` in the stack domain. Its
readiness is the heartbeat `/opti_track/pose_relay/fresh`, published only while
fresh poses flow, so `iii system start` waits for motion capture (up to 120 s).
The relay also sets PX4's EKF global origin once per flight-controller boot.
The relay (III-Drone-Core), this graph (III-Drone-Supervision), and its
parameters (III-Drone-Configuration) belong to those packages; see
[OptiTrack lab readiness](opti-track-lab-readiness.md) for the data flow and the
lab facts.

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
- GUI v2 frontend/proxy run on the ground-control computer without ROS/DDS.
- `iii-runtime-api` runs on the runtime host and bridges GUI/remote CLI
  requests to the III daemon, ROS graph, MAVLink/MAVSDK, logs, configuration,
  rosbag, and map/perception aggregators.
- `iii_gc` remains the legacy Tk GUI node for parity/reference and is not the
  GUI v2 runtime boundary.

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

When a lifecycle node's process dies and launch respawns it while the node is
meant to be active, the system manager configures and activates the new
process again. A process start or exit discards the supervisor's cached
lifecycle state for that node, so recovery always waits for the new process to
report its own state rather than trusting its predecessor's.

III C++ executables spin their nodes with `iii_drone::utils::MultiThreadedExecutor`
(`iii_drone_core/utils/multi_threaded_executor.hpp`), not rclcpp's
`MultiThreadedExecutor`. On Jazzy the upstream executor can permanently drop a
mutually exclusive callback group, including a node's default group with its
lifecycle services and timers, from its wait set
([ros2/rclcpp#3240](https://github.com/ros2/rclcpp/issues/3240)). The III
executor requests the rebuild that restores the group directly after every
mutually exclusive callback. Use it for new multi-threaded III executables.

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
10. GUI v2 observes status and injects operator commands/parameter changes
    through `iii-runtime-api`; the legacy Tk `iii_gc` node remains available as
    reference tooling but is not the primary operator GUI.
