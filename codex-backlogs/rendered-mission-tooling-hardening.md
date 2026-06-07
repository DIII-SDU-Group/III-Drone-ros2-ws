# Rendered Mission Tooling Hardening Backlog

## Incomplete

None.

## In-Progress

None.

## Completed

### 1. Make `px4.safety` source-aware

Completed.

Validation:
- `PYTHONPATH=tools/III-Drone-MCP python3 -m compileall tools/III-Drone-MCP/iii_drone_mcp`
- In devcontainer with ROS sourced, `python3 -m iii_drone_mcp.mcp_call px4.safety '{"timeout_sec":0.5}' --json`

Observed current-environment result:
- Failure because both MAVSDK and ROS PX4 topics are unavailable, which is expected with no active simulation/system.
- Result includes `degraded_sources`, `verdict_source`, `source_sufficient`, and a non-empty `TimeoutError` MAVSDK error string.

### 2. Ensure strict MCP batches always run cleanup

Completed.

Validation:
- In devcontainer with ROS sourced, compiled `tools/III-Drone-MCP/iii_drone_mcp`.
- Ran a synthetic batch with `no.such.tool` followed by `simulation stop` tagged `cleanup: true` and `--always-run-cleanup`.

Observed result:
- Batch exited nonzero for the original failure.
- Cleanup call still ran and returned success.
- Progress JSON marked the cleanup start/finish with `cleanup_after_failure: true`.

### 3. Use cached subscribers for observation timeline state

Completed.

Validation:
- In devcontainer with ROS sourced, compiled `tools/III-Drone-MCP/iii_drone_mcp`.
- Ran `sim.observation_timeline` for a short current-environment sample.

Observed result:
- Timeline succeeded without active simulation.
- PX4, maneuver queue, and perception entries now include cache metadata with `source: cache`, `cache_hit`, and `age_sec`.

### 4. Harden mission clear-queue client wait path

Completed.

Implementation note:
- Replaced the synchronous future wait with an asynchronous response callback after a 2 s service availability wait. This avoids blocking the same executor that must receive the service response and removes the misleading timeout path.

Validation:
- In devcontainer with ROS sourced, built `iii_drone_mission` successfully.
