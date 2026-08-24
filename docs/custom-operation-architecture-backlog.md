# CustomOperation Architecture Backlog

## Goal

Make `CustomOperation` a thin PX4 mode/setpoint-owner layer that functionally replaces the mission executor for manual/operator/agent operations, while reusing the existing maneuver controller, maneuver servers, token system, and `ManeuverReferenceClient`.

Correct flow:

```text
MCP / GUI / operator tooling
  -> CustomOperation generic action endpoint
    -> dispatches to the selected maneuver_controller action server
      -> existing maneuver server
      -> existing maneuver scheduler
      -> existing ReferenceCallbackToken ownership
    -> CustomOperation uses one ManeuverReferenceClient
      -> PX4 trajectory setpoints
```

Ownership boundaries:

- `CustomOperation` owns PX4 custom mode activation and setpoint publication while active.
- `CustomOperation` owns exactly one active operation at a time.
- `ManeuverController` owns maneuver scheduling, action execution, token logic, and `/get_reference`.
- `ManeuverReferenceClient` owns hover/passthrough/wait-for-maneuver/maneuver reference behavior.
- Individual maneuver servers remain the only implementation of maneuver-specific logic.

## Incomplete

- None.

## In Progress

- None.

## Complete

### Backlog Item: Make ManeuverReferenceClient Work With Plain Nodes And Lifecycle Nodes

Implemented.

Details:

- `ManeuverReferenceClient` is now a templated constructor over the owning node type.
- It accepts both `rclcpp::Node` and `rclcpp_lifecycle::LifecycleNode`.
- It captures the owning node logger and wall-timer factory.
- It no longer stores or requires a helper lifecycle-node pointer.
- Existing mission-executor lifecycle usage still builds.
- Standalone `CustomOperation` now constructs the client directly from its plain node.

Validation:

- `colcon build --base-paths src --packages-select iii_drone_core iii_drone_mission`
- `colcon test --base-paths src --packages-select iii_drone_interfaces iii_drone_core iii_drone_mission`

### Backlog Item: Replace Hand-Rolled Reference Logic With ManeuverReferenceClient

Implemented.

Details:

- Removed the duplicate local reference-mode state machine from `custom_operation_node.cpp`.
- `CustomOperation` uses one `iii_drone::control::maneuver::ManeuverReferenceClient`.
- `updateSetpoint()` calls `ManeuverReferenceClient::GetReference(dt, on_fail)`.
- Activation clears the maneuver queue and initializes hover through the reference client.
- Operation start calls `StartManeuver()` after the underlying maneuver goal is accepted.
- Operation result stops through `StopManeuver(result.target_reference)` when available.
- Cancel/deactivate cancels the forwarded maneuver goal, clears the queue, and stops/holds through the reference client.

Validation:

- Rendered nonblocking E2E passed.
- Maneuver observation verdict: success.
- Observed movement: `0.592 m`.
- Observed target progress: `0.519 m`.
- Generic operation feedback/result reported `fly_to_position` succeeded.

### Backlog Item: Replace Typed Proxy Action Servers With One Generic Operation Action

Implemented.

Details:

- `CustomOperation` exposes one action server:
  - `/mission/custom_operation/run_operation`
- It accepts:
  - `operation`
  - `arguments_json`
  - `request_id`
- It enforces one active operation at a time.
- It dispatches to the existing typed maneuver-controller action servers underneath.
- Supported forwarded operations:
  - `fly_to_position`
  - `cable_aware_fly_to_position`
  - `fly_to_object`
  - `hover`
  - `hover_by_object`
  - `hover_on_cable`
  - `cable_landing`
  - `cable_takeoff`

Validation:

- `iii_drone_mission` builds with the new single generic server implementation.
- Rendered E2E accepted and completed a generic `fly_to_position` operation.
- Headless cancellation E2E rejected a second concurrent operation while `hover` was active.

### Backlog Item: Add Generic Operation Interface Definition

Implemented.

Details:

- Added `III-Drone-Interfaces/action/CustomOperation.action`.
- Added it to `III-Drone-Interfaces/CMakeLists.txt`.
- Result and feedback payloads are generic JSON strings while preserving typed maneuver-controller actions underneath.

Validation:

- `iii_drone_interfaces` build passed.
- Downstream `iii_drone_core`, `iii_drone_mission`, and `iii_drone_gc` builds passed against the generated action.

### Backlog Item: Update MCP Operation Tools To Use Generic CustomOperation Endpoint

Implemented.

Details:

- `iii_drone_mission.operations_client.OperationsClient` now owns one `CustomOperation` action client.
- MCP operation tools keep typed ergonomic entry points.
- MCP serializes typed arguments into `arguments_json` and sends the single generic action.
- Nonblocking MCP goal registry, feedback, result, cancel, active-goal, wait, and safety-stop flows remain intact.

Validation:

- Static MCP observation suite passed.
- Rendered nonblocking E2E passed through MCP batch, without custom action-driving shell commands.
- Headless MCP batch confirmed nonblocking `start`, `active`, `cancel_goal`, and `wait_goal` behavior for cancelled goals.

### Backlog Item: Update GUI Operation Pane To Use Generic CustomOperation Endpoint

Implemented.

Details:

- The GUI operation pane already routes through `iii_drone_mission.operations_client.OperationsClient`.
- Updating `OperationsClient` switched the GUI operation path to the generic `CustomOperation` endpoint.
- Existing GUI controls remain typed/operator-friendly.

Validation:

- `python3 -m py_compile` passed for `iii_drone_gc/gc_node.py` and `iii_drone_gc/gui.py`.
- `colcon build --base-paths src --packages-select iii_drone_gc` passed.
- `colcon test --base-paths src --packages-select iii_drone_gc` passed.

### Backlog Item: Remove Old Typed CustomOperation Proxy Servers

Implemented.

Details:

- Removed the old externally exposed typed CustomOperation proxy server pattern from `custom_operation_node.cpp`.
- Kept typed underlying maneuver-controller action clients.
- Kept one generic external execution endpoint.

Validation:

- `iii_drone_mission` build passed.
- Rendered E2E used the generic `/mission/custom_operation/run_operation` path.

### Backlog Item: Rendered E2E Regression Test For CustomOperation Handoff

Implemented and passed.

Scenario:

1. Restart rendered simulation.
2. Restart, boot, and start the III system.
3. Verify PX4 health.
4. Arm.
5. Take off.
6. Activate `CustomOperation`.
7. Start nonblocking `fly_relative`, backed by generic `CustomOperation` -> typed `FlyToPosition`.
8. Observe active goal while maneuver runs.
9. Capture rendered snapshot set and observation timeline.
10. Safety-stop by landing and disarming.
11. Shutdown system and stop simulation.

Validation artifacts:

- `/tmp/iii_drone/mcp_nonblocking_e2e_current/nonblocking_maneuver_observation.json`
- `/tmp/iii_drone/mcp_nonblocking_e2e_current/nonblocking_observation_timeline.json`
- `/tmp/iii_drone/mcp_nonblocking_e2e_current/sim_topdown_topdown_1778244974628.png`
- `/tmp/iii_drone/mcp_nonblocking_e2e_current/sim_follow_drone_follow_drone_1778244977540.png`
- `/tmp/iii_drone/mcp_nonblocking_e2e_current/sim_corridor_corridor_1778244979885.png`

Result:

- MCP batch steps: 16/16 successful.
- `operation.activate`: success.
- `operation.start_fly_relative`: generic action accepted.
- `sim.observe_active_goal`: success.
- Final goal state: `succeeded`.
- Feedback count: 12.
- Distance traveled: `0.592 m`.
- Target progress: `0.519 m`.
- Rendered snapshot semantic audits: topdown, follow-drone, and corridor snapshots all passed not-blank, edge-density, drone-visible, and conductor-visible checks.

Additional cancellation/concurrency regression:

- Batch artifacts: `/tmp/iii_drone/mcp_custom_operation_cancel_e2e_2`.
- Headless MCP batch steps: 16/16 successful.
- Long `hover` accepted through the generic action.
- A second `hover` was rejected while the first operation was active.
- `operation.cancel_goal` succeeded.
- `operation.wait_goal` observed the expected cancelled terminal state.
- Safety-stop, system shutdown, and simulation stop all completed.
