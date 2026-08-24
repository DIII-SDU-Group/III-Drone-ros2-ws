# Agent-Orchestrated Testing Plan

This plan validates the operator and MCP paths for simulation bringup, PX4/QGroundControl-equivalent control, CustomOperation maneuvers, mission coexistence, perception/control services, inspection artifacts, and cable-aware flight planning.

## Scope

Validate that:

- The workspace builds with the MCP server living under `tools/III-Drone-MCP`.
- The normal mission executor path still works.
- The standalone `CustomOperation` PX4 mode runs primitive maneuvers without conflicting with mission setpoint publication.
- MCP tools can drive the same operations exposed to the GUI/operator.
- QGroundControl/PX4-equivalent commands work through MAVSDK/MAVLink where possible.
- Perception, maneuver, configuration, Gazebo, and ROS inspection tools provide enough observability for agent diagnosis.
- Cable-aware fly-to-position uses the new dedicated action and planner path, without changing the legacy `fly_to_position` path.

## Roles

- Operator: the human at the workstation. Responsible for starting the devcontainer, watching QGroundControl/RViz/Gazebo, selecting PX4 modes when a visual confirmation is needed, and stopping the vehicle if behavior is unsafe.
- Agent: Codex or an MCP client. Responsible for running build/test commands, calling MCP tools, collecting logs/artifacts, and reporting pass/fail evidence.

## Safety Rules

- Run this plan in simulation first.
- Keep QGroundControl open and ready to switch to `Position` or land.
- Do not run manual joystick movements from the agent. Use `fly_to_position` or `cable_aware_fly_to_position`.
- Never run mission executor and CustomOperation maneuvers intentionally at the same time. PX4 mode ownership should prevent concurrent setpoint publication, but this plan must verify it.
- If PX4 mode, arming state, odometry, or setpoint behavior is unclear, stop the test and inspect before continuing.

## Phase 0 - Branch And Workspace Sanity

Operator:

1. Open the workspace at `/home/ffn/Workspace/III-Drone-ros2-ws`.
2. Confirm the active workspace and affected III repos are on `simulation-improvements`.
3. Confirm a devcontainer is running for this workspace.

Agent commands:

```bash
git status --short --branch
git -C src/III-Drone-Core status --short --branch
git -C src/III-Drone-Interfaces status --short --branch
git -C src/III-Drone-Mission status --short --branch
git -C src/III-Drone-GC status --short --branch
docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}\t{{.Names}}'
```

Pass criteria:

- Workspace and affected III repos are on `simulation-improvements`.
- Devcontainer is discoverable.
- No unexpected generated artifacts are present in source directories.

## Phase 1 - Static Build And Test Baseline

Operator:

1. Keep the devcontainer running.
2. No GUI interaction is required.

Agent commands:

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon build --base-paths src \
    --packages-select iii_drone_interfaces iii_drone_configuration iii_drone_core iii_drone_mission iii_drone_gc \
    --symlink-install \
    --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
'
```

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon test --base-paths src \
    --packages-select iii_drone_interfaces iii_drone_configuration iii_drone_core iii_drone_mission iii_drone_gc iii_drone_simulation iii_drone_supervision \
    --ctest-args --output-on-failure
  colcon test-result --verbose
'
```

Pass criteria:

- Build completes.
- All selected III tests pass.

## Phase 2 - MCP Package Smoke Test

Operator:

1. No GUI interaction is required.

Agent commands:

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  source /home/iii/ws/install/setup.bash
  cd /home/iii/ws
  PYTHONPATH=/home/iii/ws/tools/III-Drone-MCP:$PYTHONPATH \
    tools/III-Drone-MCP/bin/iii_drone_mcp_server --help
'
```

```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  source /home/iii/ws/install/setup.bash
  cd /home/iii/ws
  PYTHONPATH=/home/iii/ws/tools/III-Drone-MCP:$PYTHONPATH python3 - <<PY
from iii_drone_mcp.mcp_server import DroneMcpServer
from iii_drone_mcp.agent_tools import DroneAgentTools
from iii_drone_mcp.px4_command_client import Px4CommandClient
from iii_drone_mission.operations_client import OperationsClient
print("mcp imports ok")
PY
'
```

Pass criteria:

- MCP launcher prints help.
- MCP package imports from `tools/III-Drone-MCP`.
- Shared `iii_drone_mission.operations_client` still imports for GUI/MCP reuse.

## Phase 3 - Runtime Bringup

Operator:

1. Start QGroundControl.
2. Start the PX4/Gazebo simulation workflow normally used for this workspace.
3. Open a terminal in the workspace or devcontainer.
4. Source the dev environment.
5. Run canonical system bringup.

Operator commands:

```bash
source setup/setup_dev.bash
iii system boot
iii system attach
```

Agent checks:

```bash
iii system status
iii system start
ros2 node list
ros2 topic list -t
ros2 action list -t
ros2 service list -t
```

Pass criteria:

- QGroundControl shows a connected PX4 vehicle.
- Gazebo shows the simulation world and vehicle.
- `iii system status` shows expected services/nodes.
- ROS graph includes configuration, perception, control, mission, maneuver, and custom operation endpoints.

## Phase 4 - MCP Protocol And Tool Discovery

Operator:

1. Keep the simulation running.
2. Do not arm yet unless PX4 requires arming for mode visibility.

Agent:

Start the MCP server through a stdio MCP client, or run a direct JSON-RPC smoke test:

```bash
PYTHONPATH=/home/iii/ws/tools/III-Drone-MCP:$PYTHONPATH tools/III-Drone-MCP/bin/iii_drone_mcp_server
```

Send:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Pass criteria:

- Server returns `iii-drone-mcp`.
- Tool list includes `operation.fly_to_position`, `operation.cable_aware_fly_to_position`, `operation.cancel`, `maneuver.clear_queue`, `mission.executor_action`, `payload.gripper`, `perception.pl_mapper`, `configuration`, `px4`, `system`, `inspect`, and `gazebo`.
- Tool list includes `simulation`.

## Phase 5 - Passive Inspection Tools

Operator:

1. Keep QGroundControl, Gazebo, and tmux visible.
2. Confirm the vehicle is connected but do not trigger movement.

Agent MCP calls:

- `simulation` with `command=status`.
- `simulation` with `command=start` if PX4/Gazebo/QGroundControl are not already running.
- `inspect` with `command=ros_nodes`.
- `inspect` with `command=ros_topics`.
- `inspect` with `command=ros_actions`.
- `configuration` with `command=current_file`.
- `configuration` with `command=get_yaml`.
- `gazebo` with `command=topics`.
- `px4` with `command=status`.

Pass criteria:

- ROS graph data is returned.
- Current configuration file and YAML are returned.
- Gazebo topics are returned.
- PX4 status reports arm state, flight mode, and in-air state.
- No motion is commanded.

## Phase 6 - Perception And Service Controls

Operator:

1. Watch the perception panes/logs in tmux.
2. Watch relevant RViz displays if available.

Agent MCP calls:

- `perception.pl_mapper` with `command=start`, `reset=true`.
- `perception.update_powerline_overview`.
- `perception.pl_mapper` with `command=pause`.
- `perception.pl_mapper` with `command=start`, `reset=false`.
- `payload.gripper` with `command=open`.
- `payload.gripper` with `command=close`.

Pass criteria:

- Services return successful acknowledgements.
- Perception logs show mapper state transitions.
- Powerline overview update succeeds when perception data is available.
- Gripper service responds correctly in simulation.

## Phase 7 - CustomOperation Mode Activation And Hover

Operator:

1. Arm the vehicle if required by PX4 for the custom mode.
2. In QGroundControl, select `CustomOperation` mode.
3. Keep the mode selector visible.
4. Be ready to switch to `Position` or land.

Agent checks:

- Call `px4` with `command=status`.
- Call `inspect` with `command=topic_once`, `topic=/fmu/out/vehicle_status_v1`.
- Verify `/mission/custom_operation/*` actions are present.

Pass criteria:

- PX4 reports the CustomOperation mode or equivalent mode state.
- CustomOperation node logs activation.
- Maneuver queue clear is requested on activation.
- Vehicle holds position using the CustomOperation hover setpoint.
- Mission executor-owned modes are not active.

## Phase 8 - Primitive CustomOperation Maneuvers

Operator:

1. Keep QGroundControl open.
2. Keep `CustomOperation` active.
3. Watch Gazebo/RViz for actual motion.

Agent MCP calls:

- `operation.hover` with a short duration.
- `operation.fly_to_position` to a nearby safe position.
- `operation.cancel` during a longer hover or movement.
- `maneuver.clear_queue`.

Suggested safe first movement:

```json
{
  "frame_id": "world",
  "x": 0.5,
  "y": 0.0,
  "z": 1.5,
  "yaw": 0.0,
  "timeout_sec": 20.0
}
```

Pass criteria:

- Goals are accepted only while CustomOperation is active.
- Feedback/status progresses through the ROS action path.
- Cancel stops the active operation.
- Queue clear clears queued work without killing the currently executing maneuver.
- Vehicle returns to stable hover after action completion or cancellation.

## Phase 9 - Setpoint Ownership And Mode Handover

Operator:

1. Start in `CustomOperation`.
2. Command a short CustomOperation maneuver.
3. While it is active, switch QGroundControl to `Position`.
4. Then switch back to `CustomOperation`.
5. Then activate a mission executor-owned mode or mission path.

Agent checks:

- Watch CustomOperation logs for deactivation.
- Watch mission executor logs for activation.
- Inspect setpoint/reference topics before and after handover.
- Call `operation.fly_to_position` while not in CustomOperation and confirm rejection.

Pass criteria:

- CustomOperation actions live only while CustomOperation mode is active.
- Switching away cancels or stops CustomOperation-owned work.
- Mission executor mode activation clears the maneuver queue at mission sequence activation/deactivation boundaries.
- No evidence appears of CustomOperation and mission executor publishing active PX4 setpoints at the same time.

## Phase 10 - Mission Regression Path

Operator:

1. Use the normal mission activation workflow.
2. Do not select CustomOperation during the initial mission run.
3. Watch mission behavior in QGroundControl/Gazebo/RViz.

Agent checks:

- `mission.executor_action` with `request=arm` if needed.
- Normal mission mode activation through the existing operator path.
- Inspect mission logs, behavior tree transitions, and maneuver status topics.

Pass criteria:

- Mission executor-owned mode runs normally.
- Behavior tree and maneuver execution are not affected by the MCP move.
- Mission can be interrupted by selecting CustomOperation, and CustomOperation takes over cleanly.

## Phase 11 - Cable-Aware Planner Validation

Operator:

1. Ensure perception is active and powerline/cable state is available.
2. Select `CustomOperation`.
3. Watch RViz/Gazebo closely.

Agent MCP calls:

- `perception.pl_mapper` with `command=start`.
- `perception.update_powerline_overview`.
- `operation.cable_aware_fly_to_position` to a nearby safe target requiring a path around known cable geometry.
- `inspect` with `command=plot_path_topic` for the planned path topic, if available.

Pass criteria:

- Dedicated `CableAwareFlyToPosition` action is used.
- Legacy `fly_to_position` behavior remains unchanged.
- Planned path avoids known cable obstacles.
- Trajectory is kinematically smooth after interpolation.
- Planner fails clearly if perception/cable state is unavailable.

## Phase 12 - Gazebo And Visual Artifact Capture

Operator:

1. Keep the PX4/Gazebo backend running.
2. If running the full rendered profile, optionally keep the Gazebo GUI visible for human cross-checking.

Agent MCP calls:

- `gazebo` with `command=topics`.
- `gazebo` with `command=topic_once` for a camera or model-state topic.
- `gazebo` with `command=set_camera_pose` using a safe known external camera pose.
- `gazebo` with `command=image_snapshot`, `x/y/z`, and either `target_x/target_y/target_z` or `qx/qy/qz/qw` to render an external world/drone PNG from the Gazebo server-side camera.
- `gazebo` with `command=ros_image_snapshot` only when an onboard/sensor ROS image topic is specifically needed.
- `inspect` with `command=topic_once`, `save=true` for key ROS topics.
- `inspect` with `command=plot_path_topic` for path topics where available.

Pass criteria:

- External Gazebo rendered snapshots are saved as PNG artifacts in headless and rendered simulation modes.
- Gazebo topic snapshots are saved as text artifacts.
- ROS topic snapshots are saved as artifacts.
- Path plots are generated for path messages.
- Artifact paths are returned to the agent for analysis.

## Phase 13 - Failure Injection And Diagnosis

Operator:

1. Keep QGroundControl ready for recovery.
2. Approve each intentional interruption before the agent runs it.

Agent scenarios:

- Call CustomOperation action while not in CustomOperation mode.
- Stop/pause perception, then call `cable_aware_fly_to_position`.
- Queue multiple operations, then call `maneuver.clear_queue`.
- Switch from mission executor mode to CustomOperation while a mission maneuver is active.
- Trigger PX4 `land` through MCP.

Pass criteria:

- Invalid mode rejects CustomOperation goals.
- Missing perception causes cable-aware planner failure with a useful error.
- Queue clear affects only queued maneuvers, not the currently executing maneuver.
- Mode handover remains stable.
- Landing command is accepted and visible in QGroundControl/PX4 telemetry.

## Phase 14 - Evidence To Capture

Agent should save or summarize:

- Build/test command output.
- MCP `tools/list` output.
- PX4 status before and after each mode transition.
- Action goal result/feedback for each operation.
- Logs around CustomOperation activation/deactivation.
- Logs around mission executor activation/deactivation.
- Maneuver queue clear responses.
- Perception start/update responses.
- Gazebo/ROS artifact paths.
- Any screenshot or plot paths used for visual inspection.

Operator should note:

- Whether QGroundControl mode display matched expected mode.
- Whether Gazebo/RViz motion matched commanded behavior.
- Whether any safety recovery action was needed.
- Any discrepancy between logs and visual behavior.

## Final Acceptance Criteria

The PRD is test-accepted when:

- Build and III test suite pass.
- MCP server runs from `tools/III-Drone-MCP`.
- GUI/operator path can still call shared operations.
- MCP can list and call all required tools.
- CustomOperation controls primitive maneuvers only while its PX4 mode is active.
- Mission executor behavior is unchanged in its normal path.
- CustomOperation and mission executor do not publish active setpoints simultaneously.
- Cable-aware flight uses the new dedicated action and planner path.
- Agent can inspect ROS, Gazebo, PX4, configuration, perception, and saved artifacts sufficiently to diagnose behavior.
