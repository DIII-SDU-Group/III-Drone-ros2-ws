# Submodules And Packages

## 1. Submodule Inventory (Functional)

From `.gitmodules` and checked submodule status, the workspace contains:

Core project packages:
- `III-Drone-Core`
- `III-Drone-Configuration`
- `III-Drone-Interfaces`
- `III-Drone-Mission`
- `III-Drone-Simulation`
- `III-Drone-Supervision`
- `III-Drone-GC`

Supporting dependencies (vendored/forked):
- `BehaviorTree.CPP`
- `BehaviorTree.ROS2`
- `dynamic_message_introspection`
- `px4_msgs`
- `px4-ros2-interface-lib`
- `micro-ROS-Agent`
- `micro_ros_msgs`
- `Micro-XRCE-DDS-Agent`
- `iwr6843aop-ROS2-pkg`

Tools:
- `tools/III-Drone-CLI`

## 2. ROS2 Package Inventory (from `src/*/package.xml`)

Package names and roles:

1. `iii_drone_core` (ament_cmake)
- Main C++ control/perception utilities and executable nodes.
- Provides core library + executables: `drone_frame_broadcaster`, `hough_transformer`, `pl_dir_computer`, `pl_mapper`, `trajectory_generator`, `maneuver_controller`.

2. `iii_drone_configuration` (ament_cmake + ament_python)
- Configuration server/client + C++ configurator abstractions.
- Handles parameter schema, declaration, persistence, loading, and update services.

3. `iii_drone_interfaces` (ament_cmake + rosidl)
- All custom messages/services/actions used across stack.

4. `iii_drone_mission` (ament_cmake)
- Mission execution, behavior trees, PX4 mode integration, powerline overview provider.
- Executables: `mission_executor`, `powerline_overview_provider`.

5. `iii_drone_simulation` (ament_cmake)
- Simulation-side node(s) and launch assets.
- Executable: `depth_cam_to_mmwave`.

6. `iii_drone_supervision` (ament_python)
- Supervisor node and managed node/process wrappers.
- Console scripts: `supervisor`, `managed_node_wrapper`.

7. `iii_drone_gc` (ament_python)
- Ground control GUI + ROS2 client node for operator telemetry/control.
- Console script: `gui`.

8. `iwr6843aop_pub` (ament_python)
- mmWave ROS2 publisher package.

9. `behaviortree_cpp`, `behaviortree_ros2`
- Behavior-tree runtime libraries.

10. `dynmsg`, `dynmsg_msgs`, `dynmsg_demo`, `test_dynmsg`
- Dynamic message introspection support.

11. `px4_msgs`, `micro_ros_msgs`
- ROS interface packages for PX4 and micro-ROS ecosystems.

12. `micro_ros_agent`
- Micro-ROS bridge package.

13. `px4_ros2_cpp`
- PX4 ROS2 interface library package.

## 3. Dependency Edges (High Value)

Major internal dependency chains:
- `iii_drone_core` depends on `iii_drone_interfaces`, `iii_drone_configuration`, `px4_msgs`.
- `iii_drone_mission` depends on `iii_drone_core`, `iii_drone_interfaces`, `iii_drone_configuration`, `px4_ros2_cpp`, `behaviortree_cpp`, `behaviortree_ros2`.
- `iii_drone_simulation` depends on `iii_drone_core`, `iii_drone_configuration`, `iii_drone_interfaces`.
- `iii_drone_gc` and `iii_drone_supervision` depend on core interfaces/services/runtime nodes.

System implication:
- `iii_drone_interfaces` is the central contract package.
- `iii_drone_configuration` is the central runtime parameter authority.

## 4. Packaging Model Notes

Mixed build model:
- C++ heavy packages: `ament_cmake`.
- Python operational/orchestration packages: `ament_python`.
- Hybrid package: `iii_drone_configuration` exposes both C++ and Python components.

This hybrid model is intentional and supports:
- Performance-critical control/perception in C++.
- Fast orchestration/config/supervision logic in Python.
