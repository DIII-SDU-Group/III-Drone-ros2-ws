# Inspection Demo Mission Backlog

Source spec: `docs/inspection-demo-mission-spec.md`

Processing rules:

- Keep this file as source of truth during implementation.
- Move exactly one task from `Incomplete` to `In-Progress`.
- Implement, review, build/test, then move to `Completed`.
- Do not stop until `Incomplete` and `In-Progress` are empty unless blocked by an unrecoverable issue.

## In-Progress

None.
 
## Incomplete

None.

## Completed

### 19. Runtime Smoke Validation

Acceptance criteria:

- Restarted/started III system.
- Verified pylon overview provider services exist.
- Verified mission specification exposes `inspection_demo` through mission mode status topics.
- Verified intent services exist when mission executor is running.
- Performed minimal runtime smoke without exhaustive scenario flight validation.

Tests:

- MCP `system` status/start checks passed.
- After daemon restart and system start, `pylon_overview_provider` is active.
- ROS service graph includes pylon overview services and the three mission intent services.
- ROS topic graph includes `/mission/modes/inspection_demo/status`.
- Installed MCP CLI pylon smoke passed: clear, store pylon 1, store pylon 2, get valid two-pylon overview, clear.
- Installed MCP CLI intent smoke reached `/mission/inspection_demo/trigger_recharge_now`; it returned the expected inactive-mode rejection.

### 18. Build And Test Full Target Set

Acceptance criteria:

- Built affected packages:
  - `iii_drone_interfaces`
  - `iii_drone_mission`
  - `iii_drone_configuration`
  - `iii-drone-mcp`
- Ran targeted III package tests.
- Captured runtime-domain test interference root cause and reran configuration tests in isolated ROS domain.

Tests:

- `colcon build --base-paths src tools --packages-select iii_drone_interfaces iii_drone_configuration iii_drone_mission iii-drone-mcp` passed.
- `colcon test --base-paths src tools --packages-select iii_drone_interfaces iii_drone_mission` passed.
- Full `iii_drone_configuration` CTest suite passed with `ROS_DOMAIN_ID=88 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`.
- Installed MCP registry smoke passed for new pylon/intent tools.

### 17. Extend MCP/Deploy Tooling

Acceptance criteria:

- Added MCP/manual tools for store/get/clear pylon overview.
- Deploy workflow for `inspection_demo` validates stored powerline overview and stored pylon overview before activation.
- Runtime intent services are reachable through the generic `mission.set_bool_service` MCP tool.
- Mission specification registers trigger recharge, stay-on-cable, and interrupt-recharging intent services.

Tests:

- Python compile passed for MCP files.
- MCP registry smoke passed for the new pylon overview and bool-service tools.
- Deploy workflow parser smoke passed for pylon overview arguments.
- Mission specification YAML parse smoke passed.

### 16. Add Parameters And Defaults

Acceptance criteria:

- Added all new parameters to tracked manifest/defaults and active `.config` runtime files where present:
  - `inspection_demo.inspection_clearance_m`
  - `inspection_demo.pylon_low_height_above_ground_m`
  - `inspection_demo.max_pylon_powerline_direction_mismatch_rad`
  - `inspection_demo.pylon_span_margin_m`
  - `inspection_demo.battery_topic_timeout_s`
  - `inspection_demo.battery_check_retry_count`
  - `inspection_demo.battery_check_retry_interval_s`
  - `inspection_demo.battery_voltage_threshold_v`
  - `inspection_demo.battery_voltage_debounce_s`
  - `mission.bypass_battery_checks`
  - `cable_charging.minimum_stay_on_cable_s`
- Wired new parameters into mission BT managed configurations.
- Added pylon span margin use in Reach Cable routing.
- Added pylon span direction validation and span margin use in inspection phase routing.
- Added battery retry interval behavior.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission iii_drone_configuration` passed.
- YAML parse validation passed for tracked and active config files.
- `test_schema_validator`, `test_configuration`, `test_python_core`, `test_python_schema_utils`, `test_python_parameter_handler`, and `test_python_install_layout` passed.
- `test_configurator` and `test_python_configuration` pass when rerun with isolated `ROS_DOMAIN_ID=88`; they timed out in the live runtime ROS domain.

### 1. Finalize Spec Correction: Global Blackboard Clearing

Acceptance criteria:

- Spec states that the mission executor clears the global blackboard when any mode is interrupted, fails, or completes terminally.
- Spec does not require a reset-progress service for initial implementation.

Tests:

- Documentation grep verified no stale reset-progress requirement remains.

### 2. Add Pylon Overview Interfaces

Acceptance criteria:

- Added `Pylon.msg` with `id`, `x`, `y`.
- Added `PylonOverview.msg` with timestamp, frame ID, and pylon list.
- Added `StorePylonOverview.srv`, `GetPylonOverview.srv`, `ClearPylonOverview.srv`.
- Registered new interfaces in `iii_drone_interfaces`.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_interfaces` passed.
- `colcon test --base-paths src --packages-select iii_drone_interfaces` passed: 378 tests, 0 failures.

### 3. Add Pylon Overview Provider Node

Acceptance criteria:

- Added lifecycle node in `iii_drone_mission`.
- Stores pylon ID + XY in world frame.
- Store by ID replaces existing entry.
- Get returns overview and validity flag.
- Clear removes all stored pylons.
- Valid overview requires exactly two distinct pylons.
- Node exposes a status topic.
- Node is included in build/install targets.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 4. Supervise Pylon Overview Provider

Acceptance criteria:

- Added pylon overview provider to canonical system profile.
- Added provider to mission tmux pane visibility.
- Mission executor depends on provider being active.

Tests:

- `colcon test --base-paths src --packages-select iii_drone_supervision --pytest-args -q test/test_system_spec.py` passed.

### 5. Add Runtime Intent Service Specification Parsing

Acceptance criteria:

- Mission spec supports `intent_services`.
- Each entry includes `service_name`, `flag_name`, `type=bool`, and optional valid mode keys.
- Parsed data is exposed from `MissionSpecification`.
- Unknown type fails specification parsing.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.
- `colcon test --base-paths src --packages-select iii_drone_mission -R iii_drone_mission_specification_test` passed.

### 6. Implement Generic Queued Runtime Intent Services

Acceptance criteria:

- Mission executor registers intent service endpoints from mission spec.
- Uses generic bool service request/response.
- Callback validates active mode against configured valid modes.
- Callback enqueues `{flag_name, value, sequence_id, timestamp}` instead of directly mutating blackboard.
- Service returns accepted/rejected clearly.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 7. Add BT Nodes For Intent Application And Flag Access

Acceptance criteria:

- `ApplyPendingIntentUpdates` drains mission executor pending queue on the BT tick thread.
- Add BT nodes to read/set/clear blackboard bool/string flags as needed.
- Flag lifecycle remains tree-enforced.
- Nodes are registered in `TreeExecutor`.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 8. Add Global Blackboard Clearing On Mode Termination

Acceptance criteria:

- Mission executor clears global blackboard when any mode is interrupted, fails, or completes as terminal mission completion.
- Blackboard is not cleared during normal nonterminal transition from `Inspection Demo -> Reach Cable -> Cable Charging -> Leave Cable -> Inspection Demo`.
- Clearing is logged with reason.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 9. Add Shared Powerline Geometry Helpers

Acceptance criteria:

- Extract reusable helper(s) for:
  - powerline direction/cross-corridor axes
  - reference top/middle conductor selection using existing `PowerlineWaypointProvider` logic
  - one-side/two-side split
  - pylon span direction validation
  - corridor classification
- `PowerlineWaypointProvider` continues to behave compatibly.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 10. Add PhaseWaypointProvider BT Node

Acceptance criteria:

- Generates all waypoints for normal `FlyToPosition`.
- Consumes stored powerline overview, stored pylon overview, current state.
- Requires exactly two pylons.
- Derives conductor order:
  1. one-side conductor
  2. top/middle reference conductor
  3. high conductor on two-conductor side
  4. low conductor on two-conductor side
- Selects closest generated inspection path on first activation.
- Uses top-clearance staging, with inside-corridor/between-pylons exception using Reach Cable-style bottom entry-clearance staging.
- Keeps active queue stable until phase boundary.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 11. Add Inspection Waypoint Consumption BT Nodes

Acceptance criteria:

- Add nodes to get next inspection waypoint and advance progress.
- Progress advances only after waypoint success.
- Recharge interruption leaves current waypoint index unchanged.
- All movement uses existing `FlyToPosition`.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 12. Add Charger Battery Recharge Condition

Acceptance criteria:

- Subscriber-backed nonblocking BT condition reads charger/gripper node topic only.
- No PX4 fallback.
- If `mission.bypass_battery_checks=true`, battery checks and stale-topic failures are bypassed.
- If bypass false, stale/missing topic fails only after configured retry/sampling.
- Low voltage triggers recharge after debounce.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 13. Update Cable Charging Tree

Acceptance criteria:

- Adds `ApplyPendingIntentUpdates`.
- Clears stale `charging.interrupt_requested` on activation/tree start.
- Enforces minimum stay-on-cable duration for normal exits.
- `charging.interrupt_requested` exits immediately and overrides minimum dwell.
- `charging.bypass_battery_full_check` disables battery-full exit.
- Global `mission.bypass_battery_checks` makes charging stay indefinitely until interrupt/manual mode change.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 14. Add Inspection Demo Tree And Mission Spec Mode

Acceptance criteria:

- Adds `inspection_demo_tree.xml`.
- Adds mode entry to mission specification.
- Sequence loops: `Inspection Demo -> Reach Cable -> Cable Charging -> Leave Cable -> Inspection Demo`.
- `Inspection Demo` returns success only for recharge.
- Mode is mission-owned generic maneuver mode.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.

### 15. Make Reach Cable Pylon-Aware

Acceptance criteria:

- `PowerlineWaypointProvider` has optional pylon overview input port.
- If absent/invalid, behavior unchanged.
- If inside corridor and beyond pylon span, inserts vertical FTP escape to top-clearance altitude before above-corridor path.
- Pylon-aware logic is entirely inside `PowerlineWaypointProvider`.

Tests:

- `colcon build --base-paths src --packages-select iii_drone_mission` passed.
