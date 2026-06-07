# III-Drone MCP

Workspace-level MCP stdio server for agent-operated III-Drone workflows.

The server exposes operations for:

- CustomOperation maneuver actions through `iii_drone_mission.operations_client`.
- Mission executor requests and payload/perception services.
- Configuration inspection and updates.
- PX4/Gazebo/QGroundControl simulation tool lifecycle control.
- PX4/QGroundControl-equivalent MAVSDK/MAVLink commands and PX4 external nav-state activation.
- ROS topic listing, endpoint inspection, and timed/message-count capture artifacts.
- Gazebo inspection helpers, GUI camera pose control, and PNG image snapshots.

Seeded simulation geometry and test poses live in:

```bash
tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json
```

This fixture includes ground-truth powerline reference geometry, named drone
positions, perception visibility expectations, mission staging poses, and
Gazebo snapshot camera poses for the `hca_full_pylon_setup` world.

Run from a sourced III-Drone/ROS environment:

```bash
tools/III-Drone-MCP/bin/iii_drone_mcp_server
```

When invoked as root in the devcontainer, the server re-execs as the `iii`
runtime user by default so ROS 2, Gazebo, and PX4 tooling share the same user
context as the supervised system. Set `III_DRONE_MCP_ALLOW_ROOT=1` to disable
that behavior.

For editable installation into the current Python environment:

```bash
python3 -m pip install -e tools/III-Drone-MCP
iii-drone-mcp-server
```

Temporary artifacts default to `/tmp/iii_drone/...` so MCP runs do not pollute
the workspace. Override with `III_DRONE_MCP_ARTIFACT_DIR`,
`III_DRONE_MCP_OBSERVATION_TEST_ARTIFACT_ROOT`, or explicit `--artifact-dir` /
`--artifact-root` arguments when artifacts should be retained elsewhere.

## Mission Deploy Workflow

The staging sequence for reach-cable mission runs can be launched as one
workflow:

```bash
iii-drone-mcp-mission-deploy \
  --artifact-dir /tmp/iii_drone/mission_deploy/manual \
  --position-id mid_corridor_taken_off_conductors_visible
```

It first checks the stored powerline overview. If a valid overview is already
stored, it skips staging/perception, takes off if PX4 is not already in flight,
then activates the mission mode. If no valid overview is stored, it takes off if
needed, activates CustomOperation, flies to the staging pose, waits until the
`world -> drone` pose is physically near that target, starts the PL mapper,
waits for the configured number of published powerline lines, stores the
powerline overview, then activates the mission mode.

The same sequence is exposed through MCP as nonblocking tools:

- `workflow.start_mission_deploy`
- `workflow.mission_deploy_status`
- `workflow.cancel_mission_deploy`

`workflow.start_mission_deploy` returns immediately with a `workflow_id`, PID,
status path, and log path. Progress is written under `/tmp/iii_drone` and can be
queried while the workflow is running.

## Operation Goal Handles

Nonblocking `operation.start*` tools return an MCP-side `goal_id`. That handle
is process-local: it is valid only while the MCP server or `mcp_batch` process
that created it is still alive. Responses include `mcp_session_id` and
`goal_registry_started_at` so operators can tell which process owns a handle.

If a process exits, active process-local goals are cancelled during shutdown.
If an old `goal_id` is queried from a new process, goal tools return
`unknown_goal_id` with `goal_not_recoverable=true`. Use
`operation.goal_registry_status` to inspect the current process registry and
`operation.discover_active_goals` to report the relevant ROS action surfaces.
ROS action status topics do not expose enough typed operation metadata to
reconstruct lost MCP goal records after restart.

Nonblocking operation goal payloads use a stable schema across start, status,
feedback, result, wait, cancel, and list tools. Important fields are:
`goal_id`, `mcp_session_id`, `ros_goal_id`, `action_name`, `accepted`, `state`,
`started_at`, `accepted_at`, `completed_at`, `cancel_requested_at`,
`cancelled_at`, `failed_at`, `feedback_count`, `last_feedback`,
`last_feedback_at`, `result`, `error`, `target_summary`, `reference_frame`,
and `suggested_next_tools`.

Valid `state` values are `active`, `succeeded`, `failed`,
`cancel_requested`, `cancelled`, `rejected`, and `unknown`. Rejected and unknown
responses still include the schema fields with null or empty values where no
goal was accepted.

`operation.start*` uses only action-server and goal-acceptance timeouts
(`send_timeout_sec`). It does not wait for maneuver execution. Use
`operation.wait_goal` when a blocking wait is explicitly desired. By default
`operation.wait_goal` waits until the action reaches a terminal state; add
`no_feedback_timeout_sec` with `allow_no_feedback=false` to diagnose stale
actions, or `max_wait_sec` only when the caller intentionally wants a bounded
wait. Observation tools such as `sim.observe_window` use observation duration
only and do not cancel actions.

## Blocking Semantics

Use nonblocking `operation.start*` tools for operational workflows that need
concurrent observation. Legacy/simple `operation.fly_to_position`,
`operation.hover`, and other direct `operation.*` action tools block until the
underlying action result. `mission.executor_action` blocks until the mission
executor action result. Service-backed tools block until the service returns.
PX4 tools block until command acknowledgement/postcondition where applicable.
Simulation and system tools block until lifecycle/readiness checks complete.
`mcp_batch` executes calls sequentially; concurrency comes from starting a
nonblocking operation, then running observation/status/snapshot calls while the
operation goal remains active in the same batch process.
