# MCP Observation Semantic Improvements Backlog

## Goal

Improve the MCP observation stack from "tool plumbing works" to "agent can reliably understand what happened in simulation."

The current suite verifies that tools execute, artifacts exist, images are nonblank, and numeric verdicts are structurally valid. It does not yet fully prove that snapshots and data are semantically useful for diagnosing drone behavior.

## Current Findings

### Useful Now

- Numeric plots are useful:
  - top-down plot shows conductors, pylons, corridor center, and drone path
  - side plot shows drone altitude relative to conductor heights
  - conductor-clearance plot shows nearest-conductor distance over time
- Observation JSON is useful:
  - pose samples
  - altitude band
  - corridor membership
  - distance traveled
  - nearest-conductor clearance
- Follow-drone rendered snapshot is useful:
  - drone is visible
  - terrain/context is visible

### Not Strong Enough Yet

- `sim.observe_window` in the rendered E2E currently runs after `operation.fly_relative` and `operation.hover`, so it observes mostly hover/drift, not the maneuver itself.
- Rendered E2E observed only about `0.060 m` traveled although the maneuver command was `0.2 m`; the test passed because it did not assert target displacement.
- Topdown rendered snapshot is dark but usable.
- Corridor snapshot frames the pylon/building/corridor, but does not clearly show drone behavior.
- Explicit external snapshot shows the drone small and near the edge.
- Current image checks only verify file existence, nonblank bbox, dimensions, and hash difference.
- No semantic image checks verify drone visibility, conductor visibility, or pylon/corridor framing.

## Incomplete

### Backlog Item: Resolve CustomOperation Maneuver Reference Handoff Blocker

Status: unresolved blocker from rendered E2E on 2026-05-08.

Problem:

- `operation.start_fly_relative dx=0.6` accepts the underlying `fly_to_position` goal.
- Maneuver controller logs show `FlyToPositionManeuverServer::startExecution()` starts and computes the intended target.
- CustomOperation remains in `wait_for_maneuver_start`.
- `/fmu/in/trajectory_setpoint` remains the previous hover setpoint instead of the maneuver reference.
- Direct `/control/maneuver_controller/get_reference` probing during the active maneuver returned `is_valid=false` with a hover/current-state reference, so CustomOperation cannot yet drive the maneuver path.

Evidence artifacts were written under `/tmp/iii_drone/mcp_nonblocking_e2e_current/` during the failing run.

Already patched in this sweep:

- `sim.observe_active_goal` now captures start/end control snapshots: trajectory setpoint, reference mode, vehicle status, and maneuver queue.
- `operation.start_fly_relative` target summaries now include requested/target displacement and a warning for moves close to the configured maneuver success threshold.
- Observation verdicts now support max path length and target-regression checks.
- CustomOperation `/get_reference` fetching was changed from blocking wait in the setpoint loop to one outstanding async request with cached reference update.

Next required fix:

- Replace the CustomOperation hand-rolled maneuver reference client path with the same token-aware `ManeuverReferenceClient` semantics used by mission modes, or expose the needed token/reference handshake in a reusable non-lifecycle component. The current raw `GetReference` service client is not sufficient.

### Backlog Item: Add Generic ROS Service Call MCP Tool

Status: identified friction during diagnosis.

Problem:

- Diagnosing the reference handoff required a direct `ros2 service call /control/maneuver_controller/get_reference ...`.
- This should be an MCP tool rather than custom shell usage.

Scope:

- Add typed or schema-constrained `service.call` support for existing ROS services needed in operational diagnosis.
- At minimum support `GetReference`, `ClearManeuverQueue`, lifecycle service status calls, and parameter/configuration services.
- Write response artifacts to `/tmp/iii_drone/...`.

### Backlog Item: Align Logs/Inspect Command Naming

Status: identified friction during diagnosis.

Problem:

- I attempted `inspect capture_panes` because the tool description mentions pane capture, but the actual tool is `logs {command: capture}`.

Scope:

- Add an alias or clearer tool contract so pane/log capture is discoverable without guessing.

### Backlog Item: Strengthen Snapshot Preset Orientation

Fix snapshot presets that do not currently show useful information.

Scope:

- `topdown`:
  - improve brightness/exposure if possible
  - lower camera or adjust FOV enough to keep drone and conductor corridor visible
  - keep drone and conductor span centered
- `follow_drone`:
  - keep current useful behavior
  - ensure drone does not drift out of frame during motion
- `corridor`:
  - frame both drone and the nearest visible conductor span
  - avoid pylon/building-only framing
  - use current drone pose as one target anchor, corridor center/conductor midpoint as the other
- `target`:
  - frame current drone and target simultaneously when target is supplied
  - return camera target metadata for both points
- `perception_fov`:
  - orient from behind/below drone toward expected conductor region
  - include conductors that `sim.visibility_state` predicts visible
- `custom`:
  - keep explicit pose mode unchanged

Acceptance:

- Each preset returns an image where the diagnostic subject is visually identifiable.
- Corridor preset must show drone or conductor span, not only terrain/building/pylon.
- Target preset must show the target area and current drone context.

### Backlog Item: Add Semantic Image Quality Checks

Replace weak image checks with image usefulness checks.

Scope:

- Keep existing checks:
  - file exists
  - nonzero size
  - dimensions
  - nonblank bbox
- Add scene checks:
  - drone visible or expected absent
  - conductor lines visible when expected
  - pylon visible in corridor/topdown views when expected
  - image not too dark
  - image not dominated by sky/terrain only
  - subject not on extreme image edge
- Implement first pass using computer vision heuristics:
  - brightness histogram
  - edge/line density
  - object/subject bounding box from known expected camera projection if available
  - image difference before/after maneuver
- Store semantic image audit JSON beside PNGs.

Acceptance:

- Snapshot tests fail if image is nonblank but diagnostically useless.
- The audit reports why a view failed.

### Backlog Item: Project Known Geometry Into Snapshot Frames

Use known geometry and camera pose to estimate whether expected objects should be visible in a rendered snapshot.

Scope:

- Convert snapshot camera pose/quaternion into projection frame.
- Project conductor samples, pylon boxes, drone pose, and target pose into image coordinates.
- Return expected 2D bounding boxes/line segments for visible geometry.
- Overlay optional debug image with projected geometry.
- Use projection to drive semantic image checks.

Acceptance:

- Agent can inspect an image with projected conductor/drone/target overlays.
- Tests can verify that the chosen camera orientation should contain the intended subjects.

### Backlog Item: Stronger Numeric Maneuver Verdicts

Make observation verdicts verify behavior, not just data availability.

Scope:

- Add expected target displacement:
  - `expected_dx`
  - `expected_dy`
  - `expected_dz`
  - tolerances
- Add target pose and distance-to-target before/after.
- Add trajectory/path progress:
  - monotonic progress toward target where applicable
  - minimum movement threshold
  - maximum overshoot
- Add PX4 mode timeline check.
- Add action feedback/result check.
- Add setpoint publication check.
- Add hover drift threshold for hover segments.

Acceptance:

- A `fly_relative dx=0.2` observation fails if the drone moves only `0.06 m`.
- Hover observation fails if drift exceeds configured threshold.

### Backlog Item: Perception Expected-Vs-Detected Verdict

Connect `sim.visibility_state` to perception topic data.

Scope:

- Parse `/perception/pl_mapper/powerline` artifacts.
- Compare expected visible conductors/geometry with detected powerline output.
- Include:
  - expected conductor ids
  - expected bearing/range/elevation
  - detection count
  - detection geometry summary
  - missing/extra detection notes
- Add verdict checks for perception active and publishing.

Acceptance:

- Observation verdict says whether expected visible conductors had corresponding perception output.

### Backlog Item: Observation Timeline Artifact

Add a compact timeline artifact for actions, modes, setpoints, perception, and pose.

Scope:

- Sample:
  - PX4 nav state
  - CustomOperation active state
  - maneuver queue state
  - current action status/result
  - trajectory setpoint echo
  - perception publish timestamps
  - drone pose
- Emit:
  - JSON timeline
  - plot timeline

Acceptance:

- Agent can answer "what was active when?" without reading raw logs.

## In Progress

- None.

## Complete

### Backlog Item: Temporary Artifact Containment

Implemented `/tmp/iii_drone/...` defaults for temporary MCP artifacts and Python bytecode.

- `mcp_call.py`, `mcp_batch.py`, `mcp_server.py`, and `AgentTools` default artifact directories now point under `/tmp/iii_drone`.
- `observation_test_suite.py` and `run_mcp_observation_tests.sh` default observation artifacts to `/tmp/iii_drone/mcp_observation_suite/...`.
- `run_mcp_observation_tests.sh` sets `PYTHONPYCACHEPREFIX=/tmp/iii_drone/pycache` and `PYTHONDONTWRITEBYTECODE=1`.
- `tools/III-Drone-MCP/README.md` documents the temp-artifact defaults and override variables.

Verification:

- Static MCP suite passed with artifacts under `/tmp/iii_drone/...`.
- No `__pycache__` directory remains under `tools/III-Drone-MCP`.

### Backlog Item: Active-Goal Control Snapshot Instrumentation

Implemented active-goal control snapshots for maneuver diagnosis.

- `sim.observe_active_goal` now records start/end:
  - `/fmu/in/trajectory_setpoint`
  - `/fmu/out/vehicle_status_v1`
  - `/mission/custom_operation/maneuver_reference_client/reference_mode`
  - `/control/maneuver_controller/maneuver_queue`
- This exposed the current CustomOperation blocker: reference mode stays at `wait_for_maneuver_start` and setpoint remains hover.

Verification:

- Rendered E2E produced diagnostic JSON with control snapshots.

### Backlog Item: Topic Message Count Alias

Patched ROS topic capture ergonomics.

- `topic {command: record_messages}` now accepts both `message_count` and `count`.
- The MCP schema documents the `count` alias.

Verification:

- Static MCP suite passed.

### Backlog Item: Goal Persistence Boundary

Implemented the persistence boundary for MCP-side goal handles.

- Nonblocking responses include `mcp_session_id` and `goal_registry_started_at`.
- `operation.goal_registry_status` reports `persistence=process-local` and `recoverable_after_process_restart=false`.
- Unknown/stale goal ids return structured `unknown_goal_id` data with `goal_not_recoverable=true`.
- `operation.discover_active_goals` reports the relevant ROS action/status surfaces and explicitly states that typed MCP goal records cannot be reconstructed after process restart.
- MCP process shutdown cancels active tracked goals to avoid abandoning CustomOperation state.
- Documented the process-local handle model in `tools/III-Drone-MCP/README.md`.

Verification:

- Static MCP suite passed.
- Manual stale-handle check returned `unknown_goal_id` with actionable diagnostics.

### Backlog Item: Single Active Operation Policy

Implemented the CustomOperation single-active-operation guard.

- `operation.start*` rejects when the current MCP process already tracks an active goal.
- Rejection includes active goal details, process-local active state, maneuver queue state, and suggested next tools.
- `cancel_existing=true` cancels the tracked active goal before starting a replacement.
- `clear_queue=true` clears queued maneuvers before sending the new goal.
- Added `operation.active`.
- Added `mcp_batch` `expect_success` support so expected rejections/cancellations can be asserted without custom parsing.
- Added `config/single_active_operation_policy_e2e.json`.

Verification:

- Static MCP suite passed.
- Headless E2E policy batch passed end to end:
  - first hover accepted
  - `operation.active` reported active goal and maneuver queue
  - second hover was rejected as expected
  - first hover cancelled as expected
  - PX4 landed/disarmed
  - system and simulation stopped

### Backlog Item: Stale Goal Cleanup

Implemented lifecycle cleanup for process-local operation goal records.

- Terminal goal records carry `completed_at`, `cancelled_at`, `failed_at`, and `last_feedback_at`.
- Added `operation.clear_completed_goals`.
- Added `operation.prune_goals` with `retention_sec` and `max_retained_goals`.
- `operation.goal_registry_status` includes retention settings and last prune counters.
- Registry reads prune terminal records automatically.

Verification:

- Static MCP suite passed.
- Manual `operation.prune_goals` and `operation.clear_completed_goals` calls passed.

### Backlog Item: Async Tool Result Contract

Implemented a stable schema for nonblocking operation goal responses.

- Start/status/wait/cancel/result/list snapshots share the same goal fields.
- Rejected and unknown-goal responses now include schema-compatible null/default fields.
- Documented fields and state values in `tools/III-Drone-MCP/README.md`.
- Unknown goal responses use `state=unknown`.
- Rejections use `state=rejected`.

Verification:

- Static MCP suite passed.
- Manual unknown-goal schema check returned the expected normalized fields.

### Backlog Item: Progress Sampling Without Flooding

Implemented bounded feedback sampling for nonblocking operation goals.

- Each nonblocking start uses a bounded `feedback_history` ring buffer.
- Default feedback retention is 50 messages.
- `max_feedback_messages` can override the default per started goal.
- Goal snapshots include compact `feedback_count`, `last_feedback`, and feedback age.
- `operation.goal_feedback` returns the bounded history instead of an unbounded stream.
- High-rate raw topic data remains handled by topic capture tools, which write artifacts instead of embedding unlimited data in MCP responses.

Verification:

- Static MCP suite passed.
- Rendered nonblocking E2E returned compact goal status with bounded feedback metadata.

### Backlog Item: Timeout And Liveness Semantics

Implemented and documented separated timeout semantics.

- `operation.start*` uses only server/send/accept timeout.
- `operation.wait_goal` has optional `max_wait_sec`, optional `no_feedback_timeout_sec`, and `allow_no_feedback`.
- Without `max_wait_sec`, `operation.wait_goal` continues until terminal state unless stale-feedback detection is explicitly requested.
- `sim.observe_window` remains observation-duration-only and does not cancel actions.
- Goal snapshots expose last feedback timestamp/age and result state.
- `operation.active` exposes maneuver queue context.
- README documents when to use each timeout.

Verification:

- Static MCP suite passed.
- Rendered nonblocking E2E used no action execution timeout and waited for the maneuver result.

### Backlog Item: Safety Stop Tooling

Implemented `operation.safety_stop`.

- Cancels tracked process-local operation goals.
- Waits briefly for cancellation to settle.
- Clears queued maneuvers by default.
- Commands PX4 `hold` or `land`.
- Supports `disarm_after_land`.
- Returns event-by-event status plus final PX4 and operation state.
- Added `config/safety_stop_e2e.json`.

Verification:

- Static MCP suite passed.
- Headless safety-stop E2E passed:
  - started active hover in CustomOperation
  - `operation.safety_stop mode=land disarm_after_land=true` cancelled the operation
  - queue cleared
  - PX4 landed and disarmed
  - final operation status reported no active goal
  - system and simulation stopped

### Backlog Item: Observe Running Operation Workflow

Implemented the preferred observe-while-running workflow.

- Added `sim.observe_active_goal`.
- The helper uses the same process-local nonblocking operation registry and does not start actions itself.
- It records pose samples, a goal-state timeline, plots, verdict JSON, and final goal status until the goal reaches a terminal state or max duration expires.
- Updated `config/nonblocking_operation_e2e.json` to use:
  - `operation.start_fly_relative`
  - `sim.observe_active_goal`
  - `operation.goal_status`
  - `sim.render_snapshot_set`
  - `operation.wait_goal`
  - `operation.goal_result`

Verification:

- Static MCP suite passed.
- Rendered nonblocking E2E passed with `sim.observe_active_goal`, collecting 18 valid pose samples while the goal transitioned to `succeeded`.

### Backlog Item: Update E2E Tests To Use Nonblocking Actions

Updated the E2E workflow to use nonblocking actions.

- `config/nonblocking_operation_e2e.json` starts `operation.start_fly_relative`.
- The same process observes the active goal with `sim.observe_active_goal`.
- The workflow polls status, captures rendered snapshots, waits result, verifies result, lands, disarms, shuts down, and stops simulation.
- Static validation includes the nonblocking E2E config.

Verification:

- Rendered nonblocking E2E passed end to end.

### Backlog Item: Document Blocking Semantics

Documented MCP blocking semantics in `tools/III-Drone-MCP/README.md`.

- Identifies nonblocking `operation.start*` as the preferred operational path.
- Marks legacy/simple operation tools, mission executor actions, services, PX4 commands, simulation/system commands, and `mcp_batch` as blocking/sequential where applicable.
- Explains how nonblocking operation handles enable observation/status/snapshot calls while a maneuver runs.

Verification:

- Static MCP suite passed after README/config updates.

### Backlog Item: MCP Batch Variable Passing

Implemented in `iii_drone_mcp.mcp_batch`.

- Supports `save` field-path extraction, for example `{"goal_id": "data.goal_id"}`.
- Supports `${goal_id}` interpolation in later batch call arguments.
- Writes `mcp_batch_variables.json` in the artifact directory.
- Keeps static JSON batch compatibility.
- Verified by `config/nonblocking_operation_e2e.json`, which starts a goal, saves its returned `goal_id`, observes/snapshots while it runs, polls status, waits, and reads result.

### Backlog Item: Nonblocking MCP Action Execution

Change MCP-side maneuver/action execution to be nonblocking by default for operational workflows.

Scope:

- Add nonblocking operation start tools:
  - `operation.start`
  - typed aliases such as `operation.start_fly_to_position`, `operation.start_fly_relative`, `operation.start_hover`
- `operation.start*` should:
  - validate inputs
  - send the ROS action goal
  - return immediately after goal acceptance or rejection
  - return a stable MCP-side `goal_id`
  - return the underlying ROS goal id if available
  - return action name, start timestamp, target/reference summary, and initial feedback count
- Add goal query tools:
  - `operation.goal_status`
  - `operation.goal_result`
  - `operation.goal_feedback`
  - `operation.list_goals`
  - `operation.wait_goal`
  - `operation.cancel_goal`
- Store active goal records in the MCP process while it is running.
- Keep feedback history compact:
  - count
  - last feedback
  - optional decimated feedback timeline
- Preserve typed III-Drone action inputs; do not switch to stringly typed payloads.
- Keep existing blocking operation tools as compatibility aliases or mark them as legacy/simple-use tools.
- Update `mcp_batch` support so a batch can:
  - start an operation
  - collect snapshots/data while it runs
  - poll completion
  - then wait/cancel/verify result
- Make blocking/nonblocking behavior explicit in each tool description.

Acceptance:

- Agent can start `fly_relative dx=0.2`, then call `sim.observe_window`, `sim.render_snapshot`, `sim.trajectory_state`, and topic capture while the maneuver is still running.
- Agent can query progress and completion without blocking other MCP calls.
- Agent can cancel an active operation.
- E2E no longer observes only post-action hover/drift.
- Existing blocking tools still work or are intentionally documented as compatibility tools.

Implemented:

- Added `operation.start`, `operation.start_fly_relative`, `operation.start_fly_to_position`, `operation.start_hover`.
- Added `operation.goal_status`, `operation.goal_feedback`, `operation.goal_result`, `operation.wait_goal`, `operation.cancel_goal`, `operation.cancel_all`, `operation.active`, `operation.list_goals`, and `operation.goal_registry_status`.
- Added bounded feedback history and process-local goal records.
- Added shutdown cleanup so MCP batch/process exit cancels still-active MCP-tracked operation goals instead of abandoning CustomOperation.
- Added `config/nonblocking_operation_e2e.json`.
- Added static test coverage for the new tools and config.

Verification:

- `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile ...` passed.
- `tools/III-Drone-MCP/bin/run_mcp_observation_tests.sh --phase static --clean` passed.
- Rendered nonblocking E2E batch passed end to end on 2026-05-08:
  - simulation launch
  - system boot/start
  - PX4 health/arm/takeoff
  - CustomOperation activation
  - nonblocking `fly_relative dx=0.2`
  - observation while action was running
  - rendered snapshots
  - goal wait/result
  - land/disarm
  - system shutdown and simulation stop
