# Runtime Launch And Node Graph

## 1. Canonical Bringup Model

Canonical operational entrypoint is the III CLI (`iii`), not direct `ros2 launch` alone.

Observed operational sequence:
1. Environment profile is loaded from `setup/*.bash` (for example dev/sim profile).
2. `iii system boot` starts a tmux session with predefined executables/layout.
3. Nodes/processes are running and reachable.
4. Supervisor then performs lifecycle-style configure/activate/deactivate/cleanup control over managed nodes.

Important distinction:
- Supervisor orchestrates lifecycle state transitions and dependency ordering.
- Supervisor does not replace process boot in current workflow; CLI/tmux boot provides initial process existence.

## 2. Launch Group Composition

### 2.1 Perception Launch (`perception.launch.py`)
Starts:
- `/perception/hough_transformer/hough_transformer`
- `/perception/pl_dir_computer/pl_dir_computer`
- `/perception/pl_mapper/pl_mapper`

### 2.2 Control Launch (`control.launch.py`)
Starts:
- `/control/trajectory_generator/trajectory_generator`
- `/control/maneuver_controller/maneuver_controller`

### 2.3 TF Launch (real) (`tf.launch.py`)
Starts:
- static transforms: drone->cable_gripper, drone->mmwave
- dynamic world->drone: `drone_frame_broadcaster`

### 2.4 TF Launch (simulation) (`iii_drone_simulation/launch/tf_sim.launch.py`)
Starts:
- static transforms: drone->cable_gripper, drone->mmwave, drone->depth_cam
- dynamic world->drone broadcaster

### 2.5 Simulation Sensors (`iii_drone_simulation/launch/sensors_sim.launch.py`)
Starts:
- `depth_cam_to_mmwave` converter
- `ros_gz_bridge` for camera image and depth cloud bridges

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
- `configuration_server` (lifecycle-style implementation in Python).
- `configuration_client` utility (operator/developer utility).

### 3.5 Supervision Nodes
- `supervisor` action/service orchestration node.
- `managed_node_wrapper` for process wrapping and monitor hooks.

### 3.6 Ground Control
- `iii_gc` node inside GUI process for telemetry, command, and parameter interactions.

## 4. Communication Patterns

System uses mixed ROS patterns:
- Actions for long-running maneuvers/supervision operations.
- Services for command/control and parameter management.
- Topics for state/telemetry/perception/control reference propagation.

Critical data links:
- PX4 odometry (`/fmu/out/vehicle_odometry`) into control/mission.
- Perception outputs into maneuver logic and mission condition nodes.
- Configuration services consumed by almost all higher-level components.

## 5. Lifecycle And Bringup Pattern

The codebase uses both:
- ROS lifecycle nodes directly.
- Non-lifecycle processes wrapped into lifecycle-manageable wrappers by supervision layer.

Result:
- Unified supervisory control semantics over heterogeneous node implementations.

## 6. Runtime Topology Summary

Typical complete system topology (sim or real profile dependent):
1. CLI boot creates runtime process set (tmux-managed).
2. Supervisor/configuration establish lifecycle control plane.
3. TF and sensor ingress are brought to active state via managed nodes.
4. Perception chain stabilizes powerline state.
5. Control primitives become available via action servers.
6. Mission executor registers PX4 modes and drives behavior trees.
7. Ground control observes status and injects operator commands/parameter changes.
