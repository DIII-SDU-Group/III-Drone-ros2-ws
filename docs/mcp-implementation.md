# MCP Implementation Tracker

This file tracks the operational MCP tooling needed for agent-orchestrated testing, mission running, diagnosis, analysis, simulation inspection, and data capture.

## Incomplete

- Add `px4.param_get` and `px4.param_set` for diagnostic/admin workflows. These should not silently alter normal operational control paths.
- Add guarded diagnostic-only `px4.shell` for PX4 console commands used during investigation.
- Add `logs.capture` for supervised node logs, simulation logs, PX4 console output, and QGroundControl/Gazebo process logs without direct tmux access.
- Add `artifact.list`, `artifact.copy_to_workspace`, and artifact metadata summaries for generated plots, topic captures, and simulation snapshots.
- Add `simulation.health` as a composed readiness check for Gazebo/PX4, micro-ROS topic flow, III supervised nodes, and CustomOperation readiness.
- Add generic `ros.action_status` and `ros.action_cancel` helpers that expose native ROS action state while preserving the project rule that running actions should not be timed out as long as feedback continues and the server is alive.
- Add mission/custom-operation handover inspection tooling that verifies only the active PX4 mode path publishes setpoints.

## Next

- Fix PX4 MCP client lifecycle so operational `px4.*` calls reuse one MAVSDK server/client and close it cleanly. Current behavior can leave stale `mavsdk_server` processes bound to the same UDP endpoint.
- Add `px4.health` using PX4 ROS topics, including preflight pass/fail, arming state, navigation state, failsafe flags, land detector state, and recent command acknowledgements.
- Add `operation.status` for CustomOperation lifecycle/process state, PX4 custom mode active state, action server presence, active goal, last feedback, and maneuver executor busy/idle state.
- Add `gazebo.set_model_pose`, `gazebo.hold_model_pose`, and `gazebo.release_model_pose` for deterministic simulation positioning and perception tests.
- Add `tf.lookup` for recording transforms such as `drone` relative to `world`.
- Add `simulation.geometry` helpers for `tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json`: list positions, get position, record current pose, apply pose to Gazebo, and summarize expected visibility.
- Add `perception.capture_assert` to record mmwave and powerline perception topics, decode semantic outputs, and assert expected conductor visibility.

## Complete

- `operation.*` actions expose the CustomOperation maneuver gateways for the typed III-Drone action interfaces.
- `operation.cancel` cancels the current CustomOperation action goal.
- `px4.arm`, `px4.takeoff`, `px4.disarm`, `px4.land`, `px4.hold`, `px4.return_to_launch`, `px4.set_mode`, `px4.set_nav_state`, and `px4.status` exist.
- `system.boot`, `system.start`, `system.stop`, `system.status`, `system.service_list`, and `system.service_restart` exist.
- `simulation.start`, `simulation.restart`, `simulation.stop`, and `simulation.status` exist, including rendered/headless selection.
- `topic.list`, `topic.list_info`, `topic.record_seconds`, and `topic.record_messages` exist.
- `inspect.ros_nodes`, `inspect.ros_topics`, `inspect.ros_services`, `inspect.ros_actions`, `inspect.topic_once`, and `inspect.plot_path_topic` exist.
- `gazebo.topics`, `gazebo.services`, `gazebo.topic_once`, `gazebo.set_camera_pose`, `gazebo.image_snapshot`, and `gazebo.ros_image_snapshot` exist.
- `pl_mapper` start/stop/status control exists.
