# Core Control And Perception Details

## 1. Package: `iii_drone_core`

`iii_drone_core` provides a shared C++ library (`iii_drone_lib`) plus multiple executable nodes.

## 2. Executable Nodes

1. `hough_transformer`
- Ingests cable camera stream.
- Computes cable angle-like features.
- Publishes status and angle outputs.
- Exposes start/stop `SystemCommand` service.

2. `pl_dir_computer`
- Consumes hough output and powerline estimates.
- Publishes orientation/quaternion and status.
- Exposes `SystemCommand` service.

3. `pl_mapper`
- Fuses mmWave point cloud + direction state into mapped powerline representation.
- Publishes `Powerline` and several debug point clouds.
- Exposes `PLMapperCommand` service (start/stop/pause/freeze behavior).
- Provides internal prediction loop with configurable period.

4. `trajectory_generator`
- Computes references/trajectories (MPC integration present through external matlab-generated library path).
- Supports different trajectory modes for maneuver contexts.

5. `maneuver_controller`
- Central maneuver action server host.
- Includes action servers for:
  - hover/hover by object/hover on cable
  - fly to position/fly to object
  - cable landing/cable takeoff
- Publishes maneuver queue/current status and references.
- Uses scheduler and combined drone awareness logic.

6. `drone_frame_broadcaster`
- Converts PX4 odometry into frame transform updates.
- Publishes liveness heartbeat topic used by supervision monitoring.

## 3. Shared Library Structure

`iii_drone_lib` contains:
- perception primitives (`Powerline`, `SingleLine`, Hough support)
- control primitives (`State`, `Reference`, trajectory/interpolator)
- maneuver framework (`maneuver_server`, scheduler, queue)
- adapters for ROS/PX4/message conversion
- utility types and timing/history helpers

## 4. Adapter Layer Role

Adapters abstract conversion between:
- internal math/state representations
- ROS2 custom interfaces (`iii_drone_interfaces`)
- PX4 message types (`px4_msgs`)

This adapter layer is heavily used by maneuver and mission logic, reducing direct message-handling noise in algorithm classes.

## 5. Control Stack Character

The control stack is action-centric and scheduler-based:
- maneuvers register, queue, execute, and report status
- reference retrieval is service-driven (`GetReference` path)
- mission layer requests high-level maneuvers; core translates into references/setpoints

## 6. Perception Stack Character

Perception stack is staged:
- image-derived orientation signal
- orientation processing/fusion
- pointcloud-based line mapping with temporal persistence and state transitions

`pl_mapper` state management appears central to mission landing workflows (explicit BT commands to start/pause/freeze/stop mapper).
