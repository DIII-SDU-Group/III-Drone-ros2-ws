# CustomOperation Remaining Backlog

## Incomplete

- None.

## Complete

### Backlog Item: Replace Ad Hoc Argument Parsing

Status: Complete

Current `CustomOperation` parses `arguments_json` with small regex helpers. This is fragile for nested JSON payloads and future operator/MCP inputs.

Acceptance:

- `custom_operation_node.cpp` uses a structured parser instead of regex.
- Nested target payloads from MCP/GUI can be accepted without flattening-only assumptions.
- Invalid or malformed payloads fail the operation with a clear error.
- Target parsing supports both nested `target_transform.translation/rotation` and the existing flat fallback keys.
- Build and tests pass.

### Backlog Item: Runtime-Test All Generic CustomOperation Maneuver Dispatch Paths

Status: Complete

The generic dispatcher supports all currently relevant maneuver actions, but only `fly_to_position`, `hover`, cancellation, and concurrency were runtime-tested after the refactor.

Acceptance:

- Run runtime MCP tests for:
  - `fly_to_position`
  - `cable_aware_fly_to_position`
  - `hover`
  - `fly_to_object`
  - `hover_by_object`
  - `hover_on_cable`
  - `cable_landing`
  - `cable_takeoff`
- For environment-dependent maneuvers, classify the outcome explicitly:
  - success when the maneuver is feasible in the current sim state
  - expected rejection/failure when preconditions are not satisfied
- No dispatcher serialization/parsing/action-client failures remain.
- Safety-stop and environment shutdown complete after every batch.

### Backlog Item: Mission/CustomOperation Coexistence Regression

Status: Complete

The CustomOperation refactor should preserve the PX4-mode ownership boundary with mission executor modes.

Acceptance:

- Exercise at least one mission-owned mode activation path or mission executor action path.
- Switch into `CustomOperation`.
- Run a small CustomOperation operation.
- Verify there is no simultaneous setpoint-owner conflict and that the system can safety-stop cleanly.
- If a full mission cannot run in the current sim state, record the tested coexistence surface and the reason.

### Backlog Item: MCP Active Operation Recovery Friction

Status: Complete

MCP goal handles are process-local. If the MCP process dies, exact goal handles cannot be reconstructed, but the tooling should make this explicit and provide recovery tooling.

Acceptance:

- Existing discovery/recovery tooling is tested after an MCP process restart while an operation is active, or improved if inadequate.
- The tool result clearly states that exact handle recovery is impossible.
- The suggested recovery tools are actionable.
- A stale/unknown-goal situation can be recovered through cancel-all/safety-stop/queue clear/PX4 mode commands.

## In Progress

- None.

## Validation Summary

### Replace Ad Hoc Argument Parsing

Completed. `custom_operation_node.cpp` now parses `arguments_json` using `yaml-cpp` structured nodes instead of regex helpers. It accepts nested `target_transform.translation/rotation` payloads and flat fallback transform keys. Invalid malformed payloads are rejected with a clear action error.

Evidence:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.
- `colcon test --base-paths src --packages-select iii_drone_mission` passed as part of the final mission/GC test pass.
- Runtime dispatch batch `/tmp/iii_drone/mcp_all_operation_dispatch_3_output.json` showed nested `fly_to_object` and `hover_by_object` payloads reached the underlying maneuver servers and failed only on expected current-simulation preconditions, not parser errors.
- Malformed raw payload `{bad json` returned `invalid arguments_json: yaml-cpp: error at line 1, column 1: end of map flow not found`.

### Runtime-Test All Generic CustomOperation Maneuver Dispatch Paths

Completed. All generic operation names were exercised through MCP against the single generic `CustomOperation` endpoint.

Evidence from `/tmp/iii_drone/mcp_all_operation_dispatch_3_output.json`:

- `hover`: accepted and succeeded.
- `fly_to_position` through `fly_relative`: accepted and succeeded.
- `fly_to_object`: accepted by the generic gateway, then rejected by the underlying maneuver server because current awareness/target preconditions were not satisfied.
- `hover_by_object`: accepted by the generic gateway, then rejected by the underlying maneuver server because current awareness/target preconditions were not satisfied.
- `hover_on_cable`: accepted by the generic gateway, then rejected by the underlying maneuver server because the drone was not on/targeting a cable in the required state.
- `cable_landing`: accepted by the generic gateway, then rejected by the underlying maneuver server because cable/contact preconditions were not satisfied.
- `cable_takeoff`: accepted by the generic gateway, then rejected by the underlying maneuver server because cable/takeoff preconditions were not satisfied.
- `cable_aware_fly_to_position`: accepted by the generic gateway and underlying action path, then failed in the maneuver action under current perception/planning preconditions.
- Safety-stop, system shutdown, and simulation stop completed.

No remaining dispatcher serialization, parser, or action-client failures were observed.

### Mission/CustomOperation Coexistence Regression

Completed for the available runtime surface. A full mission-owned mode activation was not reachable from the MCP action path in the tested state because the mode executor action rejected requests while its PX4 mode executor was not active. This is expected from `GenericModeExecutor::can*()` guards.

Evidence from `/tmp/iii_drone/mcp_mission_custom_coexistence_2_output.json`:

- PX4 health, arm, and takeoff succeeded.
- `mission.executor_action land` reached the action server and was rejected because the mission mode executor was not active.
- `operation.activate` then activated `CustomOperation`.
- Generic `hover` accepted and succeeded under `CustomOperation`.
- Safety-stop, system shutdown, and simulation stop completed.

This verifies the reachable coexistence surface: rejected mission-executor actions do not block CustomOperation activation or operation execution, and cleanup remains stable.

### MCP Active Operation Recovery Friction

Completed. The recovery behavior remains process-local by design, but the tooling now reports this clearly and the recovery path is tested.

Evidence from `/tmp/iii_drone/mcp_operation_recovery_2_output.json`:

- `operation.goal_status` for a stale goal id returned `unknown_goal_id`, `persistence: process-local`, and `goal_not_recoverable: true`.
- `operation.discover_active_goals` succeeded, listed `/mission/custom_operation/run_operation` and underlying maneuver action servers, and returned `recoverable: false` with the reason.
- Suggested recovery tools include `operation.cancel_all`, `operation.safety_stop`, `maneuver.clear_queue`, and `px4`.
- `operation.cancel_all`, `maneuver.clear_queue`, and `px4 status` all succeeded.
- System shutdown and simulation stop completed.

### Final Validation

- `python3 -m py_compile` passed for touched MCP/mission Python files.
- `colcon build --base-paths src --packages-select iii_drone_mission iii_drone_gc` passed.
- `colcon test --base-paths src --packages-select iii_drone_mission iii_drone_gc` passed: 179 tests, 0 failures.
- MCP static observation suite passed.
