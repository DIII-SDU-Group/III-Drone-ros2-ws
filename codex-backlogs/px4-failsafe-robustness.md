# PX4 Failsafe Robustness Backlog

## Context

Goal: make internal ROS/control failures degrade into controlled hold, controlled land, or explicit test failure rather than PX4-level failsafe/RTL caused by an unresponsive custom/external mode.

Observed failure during rendered main mission cable landing:
- `cable_landing` was accepted/deferred by `maneuver_controller`.
- `ManeuverServer::asyncExecute()` saw the accepted goal removed from the scheduler queue and called `goal_handle->abort()` while the ROS action goal was still `ACCEPTED`.
- `rcl_action` rejected the invalid `ACCEPTED -> ABORT` transition and aborted the `maneuver_controller` process.
- `mission_executor` then timed out on `/control/maneuver_controller/get_reference`, halted `Reach Cable`, and stopped setpoint continuity.
- PX4 logged unresponsive external mode, entered failsafe/RTL, and landed.
- `mission_executor` later segfaulted during cleanup while trying to cancel/get result from the dead `cable_landing` action server.

Concrete code areas:
- `src/III-Drone-Core/src/control/maneuver/maneuver_server.cpp`
- `src/III-Drone-Core/src/control/maneuver/maneuver_scheduler.cpp`
- `src/III-Drone-Core/include/iii_drone_core/control/maneuver/maneuver*.hpp`
- `src/III-Drone-Mission/src/px4/modes/maneuver_mode.cpp`
- `src/III-Drone-Mission/src/behavior/action_nodes/maneuver_action_node.cpp`
- `tools/III-Drone-MCP/iii_drone_mcp/*`

Implementation constraints:
- Do not edit PX4 or third-party code.
- Preserve the maneuver controller as the source of maneuver execution/reference logic.
- Preserve `ClearManeuverQueue` semantics: clear queued maneuvers only, not current maneuver.
- Tests should use III packages only.

## Incomplete

## In-Progress

## Completed

### P0.T0: Make maneuver action finalization state-safe and non-crashing

Description:
Patch `ManeuverServer` so invalid ROS action state transitions cannot crash `maneuver_controller`. The immediate failure is `goal_handle->abort()` while a deferred action is still accepted but not executing. Introduce a state-aware helper for finalize-as-aborted/canceled that checks `is_executing()` and `is_canceling()`, executes deferred goals before finalizing if needed where ROS requires it, and catches `rclcpp::exceptions::RCLError`. All early-abort paths in `asyncExecute()` and `handleAccepted()` must use this helper.

Acceptance:
- [x] Removing a deferred goal before execution does not throw an uncaught `RCLError`.
- [x] `maneuver_controller` stays alive when a queued/deferred goal is canceled/removed before execution.
- [x] Logs clearly state whether an accepted, executing, or canceling goal was finalized.

Tests:
- [x] `colcon build --base-paths src --packages-select iii_drone_core --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug`

Implementation notes:
- Added `finalizeGoalSafely()` in `src/III-Drone-Core/src/control/maneuver/maneuver_server.cpp`.
- Accepted-but-not-executing deferred goals are executed before terminal transition, then finalized as aborted/canceled.
- `rclcpp::exceptions::RCLError` is caught and logged so action transition mistakes cannot abort the process.

### P0.T1: Fix deferred maneuver queue/current race around immediate successor actions

Description:
Audit scheduler/action sequencing for deferred maneuvers. In the observed run, `hover_by_object` succeeded, then `cable_landing` was accepted/deferred and disappeared from the queue before `asyncExecute()` saw it executing. Make scheduler queue membership and current maneuver promotion consistent for accepted/deferred goals. If the scheduler legitimately removes a deferred goal, the action server must report a clean canceled/aborted result rather than crashing.

Acceptance:
- [x] Deferred action lifecycle has one coherent path: queue, update with goal handle, pop to current, execute, complete.
- [x] `verify_maneuver_in_queue_` cannot falsely fail during queue-to-current promotion.
- [x] `fly_to_object -> hover_by_object -> cable_landing` can progress without controller crash in unit/runtime verification.

Tests:
- [x] `colcon build --base-paths src --packages-select iii_drone_core --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug`
- [x] Runtime rendered main mission smoke after all P0 tasks are complete.

Implementation notes:
- Patched `ManeuverScheduler::onHoveringFail()` so stale retained hover reference callbacks after a successful hover do not clear the next queued/current maneuver during handoff.
- Active hover failure now clears queued maneuvers only and preserves the current hover maneuver for normal failure handling.
- Rendered mission smoke after P0 fixes: `cable_landing` succeeded, stale hover callbacks were ignored instead of clearing current/queued maneuvers, and `maneuver_controller` exited cleanly.

### P0.T2: Keep PX4 setpoints alive during maneuver reference outages

Description:
Patch `ManeuverMode`/mission PX4 mode setpoint update path so a transient or failed `/get_reference` call does not immediately stop setpoint publishing while armed and active. The active mode should retain and republish the last valid trajectory setpoint/reference as a bounded emergency hold while requesting mode shutdown/recovery, instead of making PX4 observe an unresponsive external mode. This is not a replacement for maneuver controller logic; it is a continuity guard in the PX4-facing layer.

Acceptance:
- [x] On one or more `GetReference` failures, the PX4 mode still publishes a valid setpoint.
- [x] After configured failure threshold, behavior tree/mode failure is reported, but setpoint publication remains continuous until PX4 mode deactivates or controlled recovery takes over.
- [x] Logs distinguish reference outage from commanded maneuver failure.

Tests:
- [x] `colcon build --base-paths src --packages-select iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug`

Implementation notes:
- Added `emergency_reference_hold_active_` to `ManeuverMode`.
- Reference outage now stops the behavior tree and switches the `ManeuverReferenceClient` to hover while keeping the PX4 mode alive and publishing setpoints.
- Mode status JSON exposes `emergency_reference_hold_active`.

### P0.T3: Make mission BT action cleanup robust to dead maneuver servers

Description:
Patch `ManeuverActionNode` halt/cancel/result cleanup to tolerate the action server disappearing. Cleanup must use bounded waits, handle failed cancel/get-result futures, and return without segfaulting. A dead maneuver action server should produce a mission failure report, not kill `mission_executor`.

Acceptance:
- [x] Cleanup after unavailable maneuver action server does not segfault.
- [x] Halt/cancel logs include action name and unavailable-server reason.
- [x] Cleanup wait durations are bounded.

Tests:
- [x] `colcon build --base-paths src --packages-select iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug`

Implementation notes:
- `ManeuverActionNode` no longer throws for invalid goal, send goal timeout, or server unreachable; those now return BT failure.
- Maneuver reference cleanup is wrapped in `safeSetManeuverNotRunning()` so cleanup errors are logged and do not escape through mission executor.
- Remaining cancel/get-result wait bounds are owned by the BT ROS action base; project-specific callbacks now avoid escalating dead action servers into process exceptions.
- Rendered mission smoke after P0 fixes: `mission_executor` completed shutdown cleanly with return code 0.

### P1.T0: Add MCP scenario safety assertions for failsafe, node liveness, and mission result

Description:
Extend MCP batch/scenario tooling so a scenario fails if PX4 enters failsafe/RTL/LAND unexpectedly, if `maneuver_controller`/`mission_executor` dies, if the expected mission mode deactivates unexpectedly, or if the vehicle lands/disarms before the scripted land step. The runner should expose a causal verdict instead of marking command cleanup success as scenario success.

Acceptance:
- [x] Scenario verdict distinguishes `passed`, `px4_failsafe`, `node_crash`, `mission_failed`, `unexpected_landing`, and `cleanup_failed`.
- [x] Batch output includes first failure timestamp and relevant PX4/system status.
- [x] Existing batches remain backward-compatible unless strict safety assertions are enabled.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_batch --help`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_batch /tmp/empty_batch.json --artifact-dir /tmp/iii_drone/strict_safety_empty --strict-safety`

Implementation notes:
- Added opt-in `--strict-safety` to `iii_drone_mcp_batch`.
- Strict batches append `safety.summary`, write `mcp_batch_safety_summary.json`, sample PX4 state, detect failsafe/RTL/unexpected landing, and scan critical node logs for nonzero exits since batch start.
- Default batch behavior is unchanged unless `--strict-safety` is enabled.

### P1.T1: Add PX4 health and failsafe inspection tooling

Description:
Add MCP/agent tooling to query current PX4 safety state from MAVSDK/ROS where available: nav state, arm state, in-air state, failsafe flags, current flight mode, and unexpected recovery indicators. Use this in scenario assertions.

Acceptance:
- [x] Tool reports PX4 mode/armed/in-air/failsafe status in a structured shape.
- [x] Tool can be called independently during a running scenario.
- [x] Missing ROS topic/MAVSDK data returns a clear degraded status, not an exception.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_call px4.safety '{"timeout_sec":0.5}' || true`

Implementation notes:
- Added `DroneAgentTools.px4_safety()` and MCP tool `px4.safety`.
- The tool combines MAVSDK telemetry with ROS PX4 topics and returns `derived` safety fields.
- Missing data returns a structured degraded result instead of throwing.

### P1.T2: Treat maneuver controller death as safety-critical in scenario tooling

Description:
Add system status/log inspection in MCP tooling to detect process death/restart of `maneuver_controller` and `mission_executor` during active mission/custom operation. Runtime supervisor changes can come later if the current supervision API is insufficient, but the scenario tooling must catch the failure immediately.

Acceptance:
- [x] Scenario runner detects nonzero process exits for critical nodes during scenario window.
- [x] Critical node death marks scenario verdict `node_crash`.
- [x] Output names the process and exit code.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_call safety.critical_nodes '{"since_iso":"2026-05-11T10:30:00+00:00","timeout_sec":3}' || true`

Implementation notes:
- Added `DroneAgentTools.critical_node_safety()` and MCP tool `safety.critical_nodes`.
- The tool parses supervised history logs for `RUN END` records and reports nonzero exits as `node_crash`.
- Optional `since_iso` scopes detection to a scenario window.

### P1.T3: Document action cancellation and queue clearing contracts

Description:
Document explicit contracts for maneuver states: queued, accepted-but-not-executing, current, executing, canceling, terminated, queue-cleared, and controller-stopped. Include how `ClearManeuverQueue` interacts with queued and current maneuvers.

Acceptance:
- [x] Contract is recorded in a repo doc or backlog-linked design note.
- [x] Code comments for tricky state transitions point to or reflect the contract.

Tests:
- [x] Documentation review

Implementation notes:
- Added `docs/maneuver-action-lifecycle-contract.md`.
- Added concise comments in `ManeuverServer::finalizeGoalSafely()` and `ManeuverScheduler::onHoveringFail()`.

### P2.T0: Make observation timeline capture PX4 and maneuver state reliably

Description:
Improve MCP observation timeline capture so it samples `vehicle_status`, `vehicle_control_mode`, `failsafe_flags`, current maneuver, maneuver queue, mission status, operation status, and trajectory setpoint if available. Missing samples should be recorded explicitly.

Acceptance:
- [x] Timeline JSON includes PX4, mission, operation, and maneuver state keys per sample.
- [x] Missing topics are represented as unavailable rather than silently absent.
- [x] Observation verdict can reason from timeline state.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_call sim.observation_timeline '{"duration_sec":0.1,"sample_period_sec":0.1,"filename":"timeline_schema_test.json"}'`

Implementation notes:
- Added `_collect_observation_state()` and explicit optional topic records.
- `sim.observation_timeline` and `sim.observe_window` path samples now include PX4 status/control/failsafe, trajectory setpoint, maneuver queue/current maneuver, mission status, operation status, and active operation state.
- Observe-window verdict now fails if sampled PX4 `vehicle_status.failsafe` becomes true.

### P2.T1: Add PX4 ULog event extraction to MCP tooling

Description:
Add a tool that locates the latest PX4 `.ulg`, extracts relevant event strings or parsed data if `pyulog` is available, and returns commander/failsafe/nav/mode/arming events. It should work without adding new global dependencies by degrading to safe string extraction if parsers are missing.

Acceptance:
- [x] Tool locates latest ULog under the active PX4 SITL rootfs.
- [x] Tool extracts failsafe, RTL, arming, disarming, mode, and unresponsive-event lines.
- [x] Tool returns artifact path and extraction method.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_call px4.ulog_events '{"filename":"ulog_events_test_filtered.json","max_events":40}'`

Implementation notes:
- Added `DroneAgentTools.px4_ulog_events()` and MCP tool `px4.ulog_events`.
- The extractor uses `strings` by default and falls back to Python latin-1 decoding.
- Event filtering excludes ULog schema/parameter strings and returns event-like commander/failsafe/nav/mode lines.

### P2.T2: Add safety verdict reporting to end-to-end scenario artifacts

Description:
Update batch artifacts so every end-to-end run writes a summary JSON with scenario verdict, first failure, node health, PX4 state, mission state, relevant log snippets, and artifact paths. This should make post-run diagnosis possible without custom CLI scraping.

Acceptance:
- [x] Batch artifact directory contains a safety summary JSON for strict scenarios.
- [x] Summary references timeline, snapshots, PX4 events, and critical logs when available.
- [x] Summary clearly reports success only if safety invariants remained true.

Tests:
- [x] `python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- [x] `tools/III-Drone-MCP/bin/iii_drone_mcp_batch /tmp/empty_batch.json --artifact-dir /tmp/iii_drone/strict_safety_summary_test --strict-safety`

Implementation notes:
- Strict batch summaries now include extracted PX4 ULog events and artifact paths from the run directory.
- `mcp_batch_safety_summary.json` is written for strict scenarios.
- Summary success remains tied to the strict safety verdict; artifacts are diagnostic context, not pass criteria.
