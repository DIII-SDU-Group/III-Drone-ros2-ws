# GUI V2 Observation Sweep Backlog

## Context

User navigation of GUI v2 exposed noisy command feedback, overly strict runtime mutation gating, incomplete stateful controls, and missing runtime data adapters. The implementation must keep the operator console quiet and state-driven:

- Command results should appear briefly in the bottom-right toast region only, not persist in the page body.
- Transient PX4 source disagreements should not disable or noisily annotate every flight control unless sustained.
- Runtime daemon lifecycle commands must not be blocked by PX4 vehicle safety state; only runtime/system state should determine button availability.
- Operation, perception, configuration, rosbag, dashboard, and logs views should present useful state and avoid unavailable actions.
- Missing real adapters for mission activation, PL mapper service, rosbag recorder, and logs must be addressed where local interfaces exist.

Relevant code:
- Frontend pages: `src/III-Drone-GC/frontend/src/pages/*.tsx`
- Shared controls/styles: `src/III-Drone-GC/frontend/src/components/interaction.tsx`, `src/III-Drone-GC/frontend/src/styles.css`
- Runtime API: `src/III-Drone-Runtime/iii_drone_runtime/api/*.py`
- Runtime tests: `src/III-Drone-Runtime/test/`
- Frontend tests: `src/III-Drone-GC/frontend/src/pages/*.test.tsx`, `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`

## Incomplete

## In-Progress

## Completed

### Sweep Verification

Description:
Final broad verification for the observation sweep after task-level fixes.

Results:
- [x] Full frontend Vitest suite passed: `npm test -- --run` (97 tests).
- [x] Runtime API pytest suite passed: `python3 -m pytest src/III-Drone-Runtime/test -q` (157 tests).
- [x] GUI stack rebuilt and restarted: `scripts/workspace/gui_v2_up.sh --timeout-s 60`.
- [x] GUI smoke passed through login, runtime status, all main status endpoints, command handlers, events, and logout.
- [x] Playwright verification covered login, Dashboard, Runtime, Flight, Operations, Perception, Configuration, Rosbags, and Logs.
- [x] Playwright follow-up fixes verified: global mission label shows `mission_specification.yaml`, Configuration snapshot fields show `none`, Logs has a visible empty-state, runtime entities/services auto-populate, and command CORS preflight from `http://127.0.0.1:5174` succeeds.

### T3: Perception, Rosbag, Configuration, Dashboard, and Logs Data Polish

Description:
Make PL mapper controls stateful with Start/Pause/Freeze/Stop legality based on mapper state and a Reset checkbox passed to mapper commands. Wire real ROS service adapters for PL mapper and rosbag recorder when services exist, with clean unavailable state otherwise. Make dashboard status less noisy for expected unknown optional domains and show mission spec filename only. Improve configuration empty/unknown display. Make logs discover usable local log files instead of showing empty journal placeholders only.

Acceptance:
- [x] PL mapper buttons are enabled only for valid state transitions: stopped -> start; started -> pause/freeze/stop; paused -> start/freeze/stop; frozen -> pause/stop.
- [x] PL mapper Reset checkbox is sent with mapper actions.
- [x] PL mapper command errors only appear in bottom-right toasts.
- [x] Runtime default PL mapper service adapter calls `/perception/pl_mapper/pl_mapper_command`.
- [x] Rosbag start uses the real recorder services when available and otherwise exposes a clear unavailable state.
- [x] Dashboard Control/Payload are not marked degraded solely because optional topics have not been received.
- [x] Mission displays filename only, not `$MISSION_SPECIFICATION_DIR/...`.
- [x] Configuration view distinguishes unavailable manifest from real unknown values.
- [x] Logs page shows available runtime/workspace log files when present.

Tests:
- `python3 -m pytest src/III-Drone-Runtime/test/test_perception.py src/III-Drone-Runtime/test/test_rosbag.py src/III-Drone-Runtime/test/test_configuration_api.py src/III-Drone-Runtime/test/test_payload.py src/III-Drone-Runtime/test/test_log_sources.py -q` passed (21 tests).
- `npm test -- --run src/pages/PerceptionPage.test.tsx src/pages/Dashboard.test.tsx src/pages/ConfigurationPage.test.tsx src/pages/RosbagsPage.test.tsx src/pages/LogsPage.test.tsx` passed (28 tests).
- `npm run typecheck` passed.

Implementation notes:
- Added runtime ROS adapters for PL mapper and rosbag recorder services.
- PL mapper actions now send `reset` and are stateful in the UI.
- Dashboard/config/log displays now avoid placeholder-heavy unknown states where better information is available.

### T2: Flight and Operation Gating Cleanup

Description:
Throttle transient PX4 fused-source disagreement reasons in the Flight page so short-lived inconsistency does not spam disabled reasons. Add mission mode activation transport equivalent to the custom operation PX4 nav-state adapter. Operations page must show whether CustomOperation mode is active, disable operation controls when it is not active, and enable Cancel only when an operation is active.

Acceptance:
- [x] Flight control disagreement text appears only after sustained disagreement duration.
- [x] Mission activation no longer rejects with `control mode request adapter unavailable for mission`.
- [x] Operations page clearly displays CustomOperation active/inactive state.
- [x] Operations controls are disabled when CustomOperation mode is inactive.
- [x] Cancel operation is disabled unless an operation is active.

Tests:
- `python3 -m pytest src/III-Drone-Runtime/test/test_flight_commands.py src/III-Drone-Runtime/test/test_operation_status.py src/III-Drone-Runtime/test/test_mission_status.py -q` passed (21 tests).
- `npm test -- --run src/pages/FlightPage.test.tsx src/pages/OperationsPage.test.tsx` passed (18 tests).

Implementation notes:
- FlightPage delays PX4 MAVSDK/ROS-uXRCE disagreement reasons for 2 seconds.
- `Px4NavStateModeAdapter` now supports mission and custom-operation targets when mode ids are available.
- OperationsPage displays CustomOperation mode active/inactive and gates start/validate/cancel actions.

### T1: Runtime Mutations Must Not Use Vehicle Safety Gate

Description:
Runtime shutdown, restart, and stop commands were rejected with `vehicle state unknown`; the PX4 safety gate leaked into daemon lifecycle mutations. Runtime lifecycle gating is now separate from vehicle safety.

Acceptance:
- [x] `runtime.stop`, `runtime.restart`, and `runtime.shutdown` are not rejected solely because PX4 vehicle state is unknown.
- [x] Runtime page still disables invalid lifecycle transitions by system state.
- [x] Service start/stop/restart remains stateful.

Tests:
- `python3 -m pytest src/III-Drone-Runtime/test/test_runtime_api_skeleton.py src/III-Drone-Runtime/test/test_handler_permissions.py src/III-Drone-Runtime/test/test_runtime_commands.py src/III-Drone-Runtime/test/test_runtime_gating.py -q` passed (20 tests).
- `npm test -- --run src/pages/RuntimePage.test.tsx` passed (10 tests).

Implementation notes:
- `RuntimeCommandHandlers` only applies `RuntimeMutationGate` when one is explicitly injected.
- RuntimePage lifecycle buttons now ignore vehicle armed/in-air state and use system boot/running state.

### T0: Quiet Command Feedback and Press-Hold Reset

Description:
Remove page-body command result notices from command pages where the same result is already displayed as a toast. Ensure accepted and rejected command toasts auto-dismiss briefly. Ensure press-and-hold progress bars reset after completion, cancellation, disabled state changes, and command rerenders.

Acceptance:
- [x] Rejected command notifications do not linger forever.
- [x] Command results no longer appear both mid-page and bottom-right.
- [x] Press-and-hold progress bars reset after click-and-hold completes.

Tests:
- `npm test -- --run src/components/interaction.test.tsx src/pages/FlightPage.test.tsx src/pages/PerceptionPage.test.tsx src/pages/OperationsPage.test.tsx src/pages/RosbagsPage.test.tsx src/pages/ConfigurationPage.test.tsx` passed (44 tests).
- `npm run typecheck` passed.

Implementation notes:
- `ToastRegion` now auto-dismisses toasts without explicit timeouts.
- Flight, Operations, Perception, Payload, Rosbags, and Configuration pages no longer render duplicate inline command result notices.
- `PressAndHoldButton` resets progress on completion release and disabled-state changes.
