# III-Drone Agent Tooling

This workspace has an MCP server for operational III-Drone work. Agents should prefer the MCP tools for simulation, system bringup, PX4 commands, mission workflows, ROS graph inspection, rosbags, logs, and Gazebo observation.

The MCP implementation lives in `tools/III-Drone-MCP`. The Codex registration is expected to launch the server inside the devcontainer and source `setup/setup_dev.bash`. That registration may use a single `docker exec -i` bridge as transport plumbing. Once the MCP tools are visible, do not use ad hoc `docker exec` for normal operational control.

If the III-Drone MCP tools are not visible in Codex, search for them first with `tool_search` using a query such as `iii drone mcp system simulation px4`.

## Preferred MCP Map

Use these MCP tools instead of shelling into the container:

- Simulation lifecycle: `mcp__iii_drone__.simulation`
  - `start`, `restart`, `stop`, `status`
  - use `headless=true` for PX4/Gazebo backend-only runs
- III system lifecycle: `mcp__iii_drone__.system`
  - `boot`, `start`, `stop`, `restart`, `shutdown`, `status`
  - pass `entity_id` for selected-node start/stop/restart when needed
  - pass `include_dependencies=true` for selected-node operational restarts unless deliberately isolating a node
- Logs: `mcp__iii_drone__.logs`
  - capture entity logs instead of running `iii system logs` through `docker exec`
- Mission deploy flow: `mcp__iii_drone__.workflow_start_mission_deploy`, `workflow_mission_deploy_status`, `workflow_cancel_mission_deploy`
  - use this instead of invoking `iii-drone-mcp-mission-deploy` manually
- PX4/QGroundControl-equivalent actions: `mcp__iii_drone__.px4`, `px4_safety`, `px4_health`
  - arm/disarm, takeoff/land, mode activation/status, health and failsafe checks
- Mission/custom operation modes: `mcp__iii_drone__.mission_activate_mode`, `operation_activate`
- CustomOperation goals: nonblocking `operation_start*` tools plus `operation_goal_status`, `operation_wait_goal`, `operation_cancel_all`
  - use nonblocking goal tools when observing snapshots/data during execution
- Fixture-based CustomOperation flights: `mcp__iii_drone__.operation_fly_to_fixture`
  - use this for named simulation positions such as `low_inside_corridor`, `high_inside_corridor`, `low_entry_side`, `high_entry_side`, and `above_mid`
  - it maps stored Gazebo ground-truth fixture poses into the live ROS `world` frame, activates `CustomOperation`, and starts CAFTP when a stored powerline overview is available
- Runtime/container discovery: `mcp__iii_drone__.runtime_discover_container`
  - use this to identify the workspace devcontainer instead of inlining `docker ps --filter label=devcontainer.local_folder=...`
- ROS topics: `mcp__iii_drone__.topic`
  - list topics, list endpoints, record seconds, or record message counts
- Rosbag recording: `mcp__iii_drone__.rosbag_record`
  - one recording at a time; use for mission/debug capture
- Gazebo observation: `mcp__iii_drone__.gazebo` and `sim_observation_timeline`
  - set external camera pose, save rendered snapshots, inspect observation timelines
- Configuration state: `mcp__iii_drone__.configuration`
  - inspect/update runtime configuration instead of raw `ros2 param` when the configuration server owns the setting

## Docker Exec Audit

The following command patterns were used during the previous debugging sweep. These are the operational cases that should now be MCP-first:

- `tools/simulation/launch_simulation_tools.sh --no-attach`, `--recreate`, `--headless`, `--status`, `--stop`
  - use `mcp__iii_drone__.simulation`
- `iii system boot/start/stop/status/shutdown`
  - use `mcp__iii_drone__.system`
- `iii system start/stop/restart --select-nodes ... --include-dependencies`
  - use `mcp__iii_drone__.system` with `entity_id` and `include_dependencies=true`
- `iii system logs <entity>`
  - use `mcp__iii_drone__.logs`
- `iii-drone-mcp-mission-deploy ...`
  - use `mcp__iii_drone__.workflow_start_mission_deploy` and poll status
- `ros2 topic list/info/echo` and short topic captures
  - use `mcp__iii_drone__.topic`
- `ros2 bag record ...`
  - use `mcp__iii_drone__.rosbag_record`
- PX4 arming, takeoff, land, mode changes, safety state checks through ad hoc MAVSDK/Python snippets
  - use `mcp__iii_drone__.px4`, `px4_safety`, and `px4_health`
- Gazebo camera/snapshot snippets
  - use `mcp__iii_drone__.gazebo` and observation tools
- Runtime parameter/config inspection
  - use `mcp__iii_drone__.configuration` when possible; use shell only for code-level investigation or missing MCP coverage
- Devcontainer discovery through `docker ps --filter "label=devcontainer.local_folder=..."`
  - use `mcp__iii_drone__.runtime_discover_container`
- Inline Python snippets that call `DroneAgentTools`, `MissionDeployWorkflow._target_from_geometry`, then `start_operation(...)` for named scenario positions
  - use `mcp__iii_drone__.operation_fly_to_fixture`
  - use `mcp__iii_drone__.operation_resolve_fixture_target` if only target mapping should be inspected without flying

These command patterns remain acceptable as shell commands:

- `colcon build` and package tests inside the devcontainer
- `rg`, `sed`, `git`, and direct file inspection/editing for code work
- one-off diagnosis when no MCP tool exists yet, followed by adding the missing MCP capability to this list or the MCP server
