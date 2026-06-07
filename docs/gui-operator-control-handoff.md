# GUI Operator Control Handoff

This document seeds a new agent/session for continuing the III-Drone GUI work.
It summarizes the current architecture, the reusable control surfaces built
during the agent-orchestrated testing sweep, and the operator workflows the GUI
should support.

## Goal

Bring the GUI into parity with the operator/control surface now exposed to
agents through MCP. The GUI should become the human-facing equivalent of the
agent tooling: status-rich, action-oriented, nonblocking, and useful during
simulation and real drone workflows.

The intended design is not for the GUI to call MCP stdio directly. Instead,
extract or reuse the same underlying Python/ROS control clients that MCP uses,
so both MCP and GUI are thin presentation layers over shared operator-control
logic.

## Current Runtime Architecture

The system has two PX4 mode paths that must coexist:

- Mission executor path:
  - `mission_executor` owns mission modes such as `Reach Cable`, `Cable Charging`, and `Leave Cable`.
  - Behavior trees call maneuver actions, gripper services, PL mapper services, rosbag recorder services, etc.
  - Mission executor streams references to PX4 through its maneuver reference client.

- Custom operation path:
  - `custom_operation` is a separate supervised node and PX4 `ModeBase`, not contained inside the mission executor.
  - It exposes one generic `CustomOperation` ROS action:
    - action server: `/mission/custom_operation/run_operation`
    - status topic: `/mission/custom_operation/status`
  - It forwards weakly typed operation requests to the existing maneuver servers.
  - It uses its own `ManeuverReferenceClient`.
  - It rejects goals while the CustomOperation PX4 mode is inactive.
  - It allows only one operation at a time; no queue.
  - It should not duplicate maneuver token/reference logic. That logic belongs in the maneuver controller and nested maneuver servers.

Setpoint safety rule:

- Mission executor and CustomOperation must never publish PX4 setpoints at the same time.
- The px4_ros2 mode path gates setpoint publication by active PX4 mode.
- GUI should surface which owner currently has control:
  - mission mode active
  - custom operation active
  - PX4 manual/position/hold/etc.

## Relevant Code Paths

Workspace root:

- `/home/ffn/Workspace/III-Drone-ros2-ws`

GUI package:

- `src/III-Drone-GC/iii_drone_gc/gui.py`
  - Current Tk GUI presentation layer.
- `src/III-Drone-GC/iii_drone_gc/gc_node.py`
  - ROS node that aggregates GUI-facing state and wraps some operator services.
  - Already imports `OperationsClient` if available.
- `src/III-Drone-GC/test/test_gc_node_logic.py`
  - Existing GUI/node tests.

Shared operation client:

- `src/III-Drone-Mission/iii_drone_mission/operations_client.py`
  - Python client for the generic `CustomOperation` ROS action.
  - Currently supports typed helper methods:
    - `fly_to_position`
    - `cable_aware_fly_to_position`
    - `fly_to_object`
    - `cable_landing`
    - `cable_takeoff`
    - `hover`
    - `hover_by_object`
    - `hover_on_cable`
    - `cancel_active`
  - Current caveat: helper methods are mostly blocking. GUI should use nonblocking semantics.

Custom operation node:

- `src/III-Drone-Mission/src/operations/custom_operation_node.cpp`
  - PX4 `ModeBase` implementation.
  - Generic action server.
  - Operation forwarding to maneuver action servers.
  - Rejects goals unless active.
  - Clears maneuver queue on activate/deactivate.

MCP tooling:

- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`
  - Rich operator tooling facade for agents.
  - Includes nonblocking operation goal registry, PX4 commands, system/simulation wrappers, logs, topics, rosbags, Gazebo observation, mission deploy workflow.
- `tools/III-Drone-MCP/iii_drone_mcp/px4_command_client.py`
  - MAVSDK/PX4 command client used for QGroundControl-equivalent commands.
- `tools/III-Drone-MCP/iii_drone_mcp/mission_deploy_workflow.py`
  - Nonblocking mission deployment workflow implementation.
- `tools/III-Drone-MCP/iii_drone_mcp/simulation_observation.py`
  - Gazebo/simulation observation helpers.
- `tools.md`
  - MCP tool usage map and docker-exec audit.

Supervision/system:

- `src/III-Drone-Supervision/iii_drone_supervision/system_manager.py`
  - Fixed so one `iii system start` robustly waits for `micro_ros_agent` readiness and starts dependent nodes in the same command.
- `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py`
  - `micro_ros_agent` is a service dependency for `mission_executor` and `custom_operation`.

Simulation geometry:

- `tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json`
  - Ground truth powerline geometry, pylons, named drone poses, default mission staging pose, and camera presets.

## Current GUI State

The GUI is Tk-based and already has many diagnostics:

- drone location
- armed/offboard indicators
- target status
- on-cable id
- ground altitude estimate
- powerline/perception status
- charger/gripper status
- image/plot paths depending on current code
- parameter editing through configuration server services

The GUI node already subscribes to key runtime topics:

- `/control/maneuver_controller/combined_drone_awareness`
- `/control/maneuver_controller/current_maneuver`
- `/control/maneuver_controller/target`
- `/control/trajectory_controller/target_pose`
- `/control/trajectory_controller/trajectory_path`
- `/mission/mission_executor/maneuver_reference_client/reference_mode`
- `/payload/charger_gripper/*`
- `/perception/pl_mapper/powerline`
- `/perception/pl_mapper/state`
- `/perception/pl_dir_computer/status`
- `/perception/hough_transformer/status`

The GUI node already wraps services:

- gripper command
- PL mapper command
- update powerline overview
- configuration get/save/load/set services

The GUI node already creates:

- `self.operations_client = OperationsClient(self)` when `iii_drone_mission.operations_client` is importable.

Main gap:

- The GUI presentation does not yet expose the full operator control pane.
- The shared control client is not yet GUI-friendly/nonblocking.
- MCP has richer workflow/status behavior than the GUI.

## Control Surface To Reuse

The reusable control surface should be split like this:

### Shared Python Operator Facade

Create or extend a shared package/module that can be imported by both MCP and GUI.

Suggested location:

- `src/III-Drone-Mission/iii_drone_mission/operator_control.py`

or, if it is broader than mission:

- `src/III-Drone-GC/iii_drone_gc/operator_control.py`

Preferred: put ROS action/service clients near the package that owns the ROS APIs, then keep GUI-specific state in `III-Drone-GC`.

The facade should provide:

- operation action start/status/cancel
- current active operation state
- mission mode activation helpers
- gripper open/close command helpers
- PL mapper start/stop/store overview helpers
- powerline overview status helpers
- PX4 command/status wrapper if MAVSDK is available in GUI runtime

Do not make GUI depend on MCP stdio protocol.

### Nonblocking Operation Semantics

The GUI should not call blocking operation helpers directly from button callbacks.

Needed shared semantics:

- `start_operation(operation, arguments) -> goal_handle/status object`
- `get_operation_status()`
- `cancel_operation()`
- action feedback callback updates GUI state
- action result callback updates GUI state
- reject new action if one is already active
- show rejected reason if CustomOperation mode is inactive

MCP already implements a process-local nonblocking registry in `AgentTools`.
Use it as a reference design, but avoid copying MCP-specific response schemas into GUI code.

### PX4/QGroundControl Equivalent Commands

Agents currently use MCP for:

- arm
- disarm
- takeoff
- land
- hold
- return to launch
- set mode/nav state
- health/status/failsafe checks

The GUI should expose a curated subset:

- Arm
- Takeoff
- Land
- Hold
- Disarm
- Activate CustomOperation
- Activate Reach Cable
- Activate Cable Charging
- Activate Leave Cable
- Show current PX4 mode/nav state/armed/in-air/failsafe state

Implementation options:

- Use MAVSDK client code from `tools/III-Drone-MCP/iii_drone_mcp/px4_command_client.py`.
- Or wrap the relevant ROS/PX4 command topics/services if preferred.

For consistency with agent tooling and QGroundControl-equivalent behavior, MAVSDK is currently the strongest starting point.

## Operator Workflows To Support

### Workflow 1: Bringup/Status

Operator wants to see if the system is ready.

GUI should show:

- simulation/system profile if available
- supervised node state summary
- `micro_ros_agent` alive/ready
- `mission_executor` active
- `custom_operation` active
- PX4 connected/armed/in-air/nav state
- current control owner
- active mission mode
- active custom operation goal
- perception state:
  - Hough transformer status
  - PL mapper state
  - PL dir computer status
  - number of detected lines
  - stored overview valid/invalid

Actions:

- refresh status
- open logs path or show latest relevant log tail if implemented

### Workflow 2: Manual CustomOperation Flight

Operator wants to fly by typed target, not joystick.

Expected sequence:

1. PX4 is armed and in flight, or operator uses GUI takeoff.
2. Operator activates `CustomOperation`.
3. GUI confirms CustomOperation mode active.
4. Operator enters target:
   - frame: `world`
   - x/y/z/yaw
5. Operator presses Fly.
6. GUI starts nonblocking `fly_to_position`.
7. GUI displays:
   - accepted/rejected
   - operation name
   - target summary
   - feedback count/latest feedback
   - running/succeeded/failed/canceled
8. Operator can cancel/safety stop.

Important:

- GUI must not queue actions.
- If action rejected because CustomOperation inactive, say that explicitly.
- If action rejected because another action is active, show current action.

### Workflow 3: Mission Deploy Prep

Operator wants to stage the drone and store powerline overview before mission.

Current agent workflow:

1. If not in flight, takeoff.
2. Activate CustomOperation.
3. Fly to configured mid-corridor position.
4. Start PL mapper.
5. Wait for enough detected powerline lines.
6. Store powerline overview.
7. Optionally activate mission mode.

GUI should support both:

- step-by-step buttons
- one-shot “Prepare Mission” button

One-shot logic:

- If stored powerline overview is valid:
  - skip PL mapper/store overview.
  - if not in flight, takeoff.
  - leave operator ready to launch mission.
- If overview is missing/invalid:
  - takeoff if needed.
  - activate CustomOperation.
  - fly to staging pose from `hca_full_pylon_setup_geometry.json`.
  - start PL mapper.
  - store overview.

The current successful staging pose was updated in the geometry JSON during testing. Use the current file as source of truth.

### Workflow 4: Launch Reach Cable Mission

Operator wants to start mission from current state.

GUI actions:

- Activate mission mode `Reach Cable`.
- Show mission state progression:
  - Reach Cable
  - Cable Charging
  - Leave Cable
  - Hold/completed/failure
- Show behavior-level status if available from logs/topics.
- Show current maneuver:
  - fly_to_position
  - fly_to_object
  - cable_landing
  - hover_on_cable
  - cable_takeoff
  - etc.
- Show rosbag recording state.

Important:

- Reach Cable tree starts/stops rosbag recording automatically through `rosbag_recorder`.
- On failure, GUI should show a direct failure reason when available.

### Workflow 5: Perception/Overview Control

Operator wants to inspect and command perception.

GUI should expose:

- PL mapper start
- PL mapper stop
- Store powerline overview
- Overview status:
  - valid/invalid
  - line count
  - target/entry conductor id if available
- Detected lines table:
  - id
  - point in world frame
  - visibility/alive status if available
- Stored overview table:
  - id
  - point in world frame
  - direction

This is important because many recent Reach Cable failures were caused by overview-vs-live line matching, not by PX4 or cable landing.

### Workflow 6: Gripper/Payload

Operator wants to inspect and command the simulated/real gripper.

GUI should show:

- gripper commanded state if available
- gripper reported state:
  - open
  - closing
  - closed
- battery voltage
- charging power
- charger status
- charger operating mode

GUI actions:

- Open gripper
- Close gripper

Simulation gripper semantics:

- If instructed open: reports open and detaches if attached.
- If instructed closed but not on conductor: reports open/closing, not closed.
- If instructed closed and conductor enters latch volume: attaches and reports closed.
- Closed gripper is ground truth for “on cable” in addition to geometry.

### Workflow 7: Safety Stop

Operator needs a single obvious escape action.

Safety stop should:

1. Cancel active CustomOperation goal, if any.
2. Ask maneuver system to stop/idle if exposed.
3. Switch PX4 to Hold or Position, depending on chosen policy.
4. Optionally land/disarm only if operator explicitly chooses that variant.
5. Show final PX4 status and active operation state.

Do not hide failures. If cancel or mode switch fails, display it.

## Recent Mission/Simulation Context

The full mission has recently succeeded in rendered sim after many fixes.

Important fixes already made:

- `CustomOperation` is supervised and depends on `micro_ros_agent` readiness.
- Single `iii system start` race was fixed.
- Reach Cable can fly down from higher corridor starts to the correct target cable.
- Powerline direction alignment should accept positive or negative cable direction and use shortest yaw.
- Gripper simulation latch geometry was tuned.
- ROS mmwave/gripper frame translations were aligned with Gazebo-rendered debug frames.
- Cable landing safety checks were changed toward gripper-frame/V-gate logic.
- Cable charging/leave cable naming and sequence were updated.
- Leave cable uses upward thrust/hover-on-cable before opening gripper.
- Rosbag recording is wired into Reach Cable via BT services.

Known current issue from the latest run:

- Reach Cable failed before cable landing because `VerifyPowerlineDetected` rejected live detected line id 3 against stored overview line id 3.
- The orthogonal-plane error was about `0.679m` to `0.795m`, threshold `0.500m`.
- This indicates perception/overview matching diagnostics should be visible in the GUI.

## GUI Design Recommendations

Keep the GUI as an operator dashboard, not a landing page.

Layout suggestion:

- Left: system/PX4 status and safety controls.
- Center: operation control pane.
- Right: perception/mission/payload panels.
- Bottom: current action/mission timeline/log tail.

Control pane sections:

- PX4:
  - Arm, Takeoff, Hold, Land, Disarm
  - current mode/nav state
- Mode activation:
  - CustomOperation
  - Reach Cable
  - Cable Charging
  - Leave Cable
- CustomOperation:
  - Fly to position fields
  - Cable-aware fly to position fields
  - Hover duration
  - Cable landing target id
  - Cable takeoff target id/distance
  - Start, Cancel
- Mission prep:
  - Prepare overview
  - Launch Reach Cable
  - Full deploy
- Perception:
  - PL mapper start/stop
  - Store overview
  - line count/status
- Payload:
  - Gripper open/close
  - status/charging

GUI should make actions disabled when unavailable:

- CustomOperation action buttons disabled unless CustomOperation mode active.
- Mission buttons disabled if mission executor inactive or PX4 mode unavailable.
- Store overview disabled if PL mapper not running or no lines detected.
- Gripper buttons disabled if gripper service unavailable.

## Implementation Plan

### Step 1: Shared Nonblocking Operation Client

Extend `OperationsClient` or add a sibling class with:

- `start_goal(operation, arguments, feedback_cb=None, done_cb=None)`
- `active_goal_snapshot()`
- `cancel_active()`
- `poll()` if needed

Acceptance:

- GUI can start `fly_to_position` and keep refreshing UI.
- Starting a second operation while one is active is rejected locally or by server and reported.
- Cancel works and updates state.

### Step 2: GC Node Operator Methods

Add methods to `IIIGCNode`:

- `start_custom_operation_goal(...)`
- `get_custom_operation_status()`
- `cancel_custom_operation()`
- `activate_custom_operation_mode()`
- `activate_mission_mode(mode_key)`
- `px4_arm/takeoff/hold/land/disarm/status`
- `start_pl_mapper/stop_pl_mapper/store_overview`
- `open_gripper/close_gripper`

Acceptance:

- Each method returns a structured result:
  - `success`
  - `message`
  - optional `data`
- No Tk code directly constructs ROS action/service requests.

### Step 3: GUI Control Pane

Add a dedicated control pane to `gui.py`.

Acceptance:

- Buttons are grouped by workflow.
- Long actions do not freeze the GUI.
- Current action and status are visible.
- Operator can cancel active custom operation.

### Step 4: Mission Prep/Deploy Workflow

Port the nonblocking mission deploy workflow concept from MCP into shared code or GUI node code.

Acceptance:

- One GUI button can run:
  - takeoff if needed
  - CustomOperation activation
  - staging fly-to
  - PL mapper start
  - overview store
  - optional mission activation
- Workflow exposes step-by-step progress.
- Operator can stop/cancel.

### Step 5: Perception Diagnostics

Add overview/live powerline display.

Acceptance:

- GUI shows stored overview line points.
- GUI shows live detected line points.
- GUI shows selected/target line id where available.
- GUI surfaces verify/matching failures if published/logged.

### Step 6: Testing

Use targeted tests first:

- `iii_drone_gc` unit tests.
- Tests for nonblocking operation client using mocked action client where feasible.
- Manual rendered-sim validation:
  - launch sim
  - `iii system start`
  - use GUI to takeoff
  - activate CustomOperation
  - fly to staging pose
  - start PL mapper
  - store overview
  - launch Reach Cable
  - observe mission result and GUI state

## Operational Commands For New Session

Prefer MCP for runtime operations:

- `mcp__iii_drone__.simulation`
- `mcp__iii_drone__.system`
- `mcp__iii_drone__.px4`
- `mcp__iii_drone__.mission_activate_mode`
- `mcp__iii_drone__.workflow_start_mission_deploy`
- `mcp__iii_drone__.logs`

Use shell for code/build/test:

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon build --base-paths src --packages-select iii_drone_gc iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
'
```

Tests:

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon test --base-paths src --packages-select iii_drone_gc iii_drone_mission --ctest-args --output-on-failure
  colcon test-result --verbose
'
```

Only run tests for III packages.

## Pitfalls

- Do not make GUI call MCP stdio directly. Share the underlying client logic.
- Do not add a second setpoint publisher path outside px4_ros2 active-mode gating.
- Do not execute CustomOperation actions unless CustomOperation is active.
- Do not queue CustomOperation actions.
- Do not block Tk callbacks on long ROS actions.
- Do not rely on QGroundControl virtual joystick for agent workflows; use fly-to-position/custom operation.
- Do not hand-edit generated `build/`, `install/`, or `log/`.
- Be careful with submodules and existing dirty worktree changes.

## Good First Slice

Implement nonblocking CustomOperation controls in the GUI:

1. Extend `OperationsClient` with nonblocking start/status/cancel.
2. Add `IIIGCNode` wrappers.
3. Add GUI panel:
   - CustomOperation active indicator
   - Fly-to-position fields
   - Start button
   - Cancel button
   - current goal status
4. Test with rendered simulation:
   - activate CustomOperation
   - start fly-to-position
   - verify GUI stays responsive
   - cancel or complete

This slice proves the architecture before porting PX4, mission deploy, and perception workflows.

