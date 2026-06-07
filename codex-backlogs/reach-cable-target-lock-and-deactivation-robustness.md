# Reach Cable Target Lock And Deactivation Robustness Backlog

## Context

Manual activation of Reach Cable from a running simulation showed the first under-cable waypoint was correct, but the subsequent object/cable landing phase chased a different conductor. Logs showed the tree selected overview line id `3`, accepted a live detection match to detected line id `2` at `1.658 m`, then later `GetGripperAlignmentYaw` aligned to detected line id `1` because the target id was not wired into that node. The root issue is target identity drift between stored overview, live detection verification, alignment, `FlyToObject`, `HoverByObject`, and `CableLanding`.

Agreed design:
- Do not require a fresh powerline overview.
- The live detected target must match the stored overview target using distance in the plane orthogonal to the powerline direction, so displacement along the conductor span is ignored.
- The matched live detected line id becomes the single target id used for alignment, fly-to-object, hover-by-object, and cable landing.
- Reject Reach Cable if no detected line is close enough to the overview target.
- Add target-selection and maneuver-target diagnostics as DEBUG logs, and enable debug log level for the associated nodes for now.
- Mission/BT execution must survive manual PX4 mode changes such as switching to Hold during execution.

Relevant files:
- `src/III-Drone-Mission/behavior_trees/reach_cable_tree.xml`
- `src/III-Drone-Mission/src/behavior/condition_nodes/verify_powerline_detected_condition_node.cpp`
- `src/III-Drone-Mission/include/iii_drone_mission/behavior/condition_nodes/verify_powerline_detected_condition_node.hpp`
- `src/III-Drone-Mission/src/behavior/condition_nodes/get_gripper_alignment_yaw_condition_node.cpp`
- `src/III-Drone-Mission/src/behavior/condition_nodes/target_provider_condition_node.cpp`
- `src/III-Drone-Mission/src/behavior/trees/tree_executor.cpp`
- `src/III-Drone-Core/src/control/maneuver/fly_to_object_maneuver_server.cpp`
- `src/III-Drone-Core/src/control/maneuver/cable_landing_maneuver_server.cpp`
- `src/III-Drone-Core/src/control/combined_drone_awareness_handler.cpp`
- `setup/node_log_levels.bash`

## Incomplete

## In-Progress

## Completed

### T4: Patch Mission Deploy Staging Altitude Against Live Ground Estimate

Description:
The first mission deploy run failed before Reach Cable because the staging target was raised only to `z=1.5`, while the current maneuver-controller ground estimate was `0.512 m`; the target was therefore `0.988 m` above ground and violated the `1.0 m` minimum target altitude. Patch the workflow to read combined drone awareness and adjust staging altitude to live ground estimate plus minimum clearance and margin before sending `fly_to_position`.

Acceptance:
- [x] Mission deploy workflow adjusts staging altitude from live ground estimate when needed.
- [x] MCP schema and command forwarding expose the new altitude guard arguments.
- [x] The retry deploy workflow gets past staging and activates Reach Cable.

Tests:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mission_deploy_workflow.py tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py` - passed.
- Runtime retry adjusted target to `z=1.662380003929138`, completed staging, stored overview, and activated Reach Cable.

### T3: Restart Runtime And Run Rendered Mission Deploy

Description:
After implementation and build, restart rendered simulation and III system, then run the existing non-blocking mission deploy flow. Inspect mission and maneuver logs for the target lock behavior, rejection of bad overview/live associations, and absence of mission executor crashes.

Acceptance:
- [x] Rendered simulation restarts.
- [x] III system starts and managed nodes become healthy.
- [x] Mission deploy starts through the existing tooling.
- [x] Logs show the matched live detected id is used throughout Reach Cable.
- [x] If the mission fails, the failure is logged as a controlled behavior failure rather than a process crash.

Tests:
- MCP `simulation` restart - passed, rendered simulation ready.
- MCP/CLI `system boot` and `system start` - passed after clearing stale `iii_sim` session.
- MCP `workflow.start_mission_deploy` - first run exposed T4 altitude guard; retry passed deployment and activated Reach Cable.
- Reach Cable runtime log showed overview line id `3` matched detected line id `3` at `0.239 m`, and id `3` was used for gripper yaw, target provider, fly-to-object, and cable landing.
- Reach Cable later failed after three controlled cable-landing aborts; `mission_executor` and `maneuver_controller` remained alive and PX4 reported no failsafe.

### T2: Make Behavior Tree Execution Robust To Manual Deactivation

Description:
Protect `TreeExecutor::execute()` against exceptions thrown while ticking or halting a tree, including action-client cancellation races such as `rclcpp_action::exceptions::UnknownGoalHandleError`. Manual PX4 mode changes should terminate the tree as failure/deactivated without terminating the `mission_executor` process. Preserve existing success semantics: success only when the tree returns `SUCCESS` while still running.

Acceptance:
- [x] Exceptions in `tickOnce()` or `haltTree()` are caught and logged.
- [x] `mission_executor` process does not crash when a running Reach Cable mission is manually deactivated.
- [x] The tree ends with `success=false` on exception/deactivation.

Tests:
- `colcon build --base-paths src --packages-select iii_drone_mission iii_drone_core --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug` - passed.
- Runtime check pending in T3 mission deploy.

Implementation notes:
- Wrapped `TreeExecutor::execute()` tick and halt paths in exception handlers.
- Exceptions now log controlled errors, mark the tree failed, and leave the process alive.

### T1: Add DEBUG Target Diagnostics And Enable Associated Debug Logging

Description:
Add DEBUG logs to target matching/selection/reference path: overview target id/point, each detected candidate id/distance, matched detected id, target-provider id/frame/transform, gripper-alignment target id/yaw, fly-to-object target adapter id/frame, cable-landing target id/frame, and combined awareness target transform world pose. Change the temporary log level defaults in `setup/node_log_levels.bash` for behavior tree, mission executor, maneuver controller, and trajectory generator to debug.

Acceptance:
- [x] Diagnostic target identity and geometry data is available at DEBUG level.
- [x] Normal INFO output is not made substantially noisier by these new diagnostics.
- [x] Associated node log level env vars default to `debug` for this sweep.

Tests:
- `colcon build --base-paths src --packages-select iii_drone_mission iii_drone_core --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug` - passed.
- Runtime log confirmation pending in T3 mission deploy.

Implementation notes:
- Added DEBUG logs in overview/live matching, gripper yaw alignment, target provider, fly-to-object reference updates, cable-landing reference updates, and combined awareness target transform computation.
- Set behavior tree, mission executor, maneuver controller, and trajectory generator default log levels to `debug`.

### T0: Lock Reach Cable Target To Verified Matched Detection

Description:
Extend `VerifyPowerlineDetectedConditionNode` so overview/live matching outputs the matched detected line id and orthogonal-plane distance. The existing `distanceInPlaneOrthogonalToDirection` already ignores displacement along the conductor direction and should remain the matching metric. Update `reach_cable_tree.xml` so `VerifyPowerlineDetected` writes the matched id to `{target_cable_id}` and remove the independent `SelectTargetLine` step from Reach Cable. Wire `{target_cable_id}` into `GetGripperAlignmentYaw`.

Acceptance:
- [x] `VerifyPowerlineDetected` has output ports for matched detected line id and match distance.
- [x] Reach Cable rejects when no detected line matches the overview target within threshold.
- [x] Reach Cable uses the matched detected id for `GetGripperAlignmentYaw`, `FlyToObject`, `HoverByObject`, and `CableLanding`.
- [x] The match threshold is stricter than the previous `2.0 m`; use `0.5 m` initially to reject the observed `1.658 m` false association while accepting normal observed matches around `0.04-0.30 m`.

Tests:
- `colcon build --base-paths src --packages-select iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug` - passed.
- Inspect Reach Cable logs after mission deploy for consistent matched id and target id.

Implementation notes:
- Changed `VerifyPowerlineDetected` to output `matched_detected_line_id` and `matched_line_distance_m`.
- Changed Reach Cable trees to set `{@target_cable_id}` from verification and removed independent `SelectTargetLine` in that path.
- Changed Reach Cable match threshold from `2.0` to `0.5`.
