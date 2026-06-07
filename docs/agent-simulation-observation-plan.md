# Agent Simulation Observation Backlog

## Goal

Give the agent enough structured and visual information to monitor an ongoing Gazebo/PX4/ROS simulation in place of an operator watching Gazebo.

The observation path must support:

- validating drone behavior during missions and CustomOperation maneuvers
- choosing meaningful camera viewpoints automatically
- preserving geometric truth without overwhelming the agent with raw data
- producing evidence artifacts that can be inspected after a run
- returning concise verdicts for automated test scenarios

Gazebo snapshots should be evidence, not the primary truth source. The primary truth should come from structured simulation geometry, TF, PX4 state, maneuver state, trajectory/path samples, and perception outputs.

## Incomplete

- None.

## In Progress

- None.

## Complete

### Backlog Item: `sim.geometry_state`

Implemented in `tools/III-Drone-MCP/iii_drone_mcp/simulation_observation.py` and exposed as MCP tool `sim.geometry_state`.

Capabilities:

- Loads simulation geometry from `tools/III-Drone-MCP/config/hca_full_pylon_setup_geometry.json`.
- Returns current or provided `world`-frame drone pose.
- Returns compact conductor geometry, pylon geometry, and powerline corridor model.
- Computes drone corridor membership.
- Computes nearest conductor id, closest point, and distance.
- Includes PX4 odometry velocity when available.

Verified:

- Light MCP batch: `sim.geometry_state` returned compact geometry and nearest conductor data.
- Rendered E2E batch: geometry was used by observation verdict and plots.

### Backlog Item: `sim.visibility_state`

Implemented and exposed as MCP tool `sim.visibility_state`.

Capabilities:

- Uses current or provided drone pose.
- Computes expected conductor visibility using range, bearing, and upward cone assumptions.
- Returns per-conductor range, bearing, elevation, and expected visibility.
- Includes nearest recorded simulation fixture expectation.

Verified:

- Light MCP batch: expected visible conductors returned for a known corridor pose.
- Rendered E2E batch: observation verdict confirmed visibility state was available.

### Backlog Item: `sim.trajectory_state`

Implemented and exposed as MCP tool `sim.trajectory_state`.

Capabilities:

- Samples recent `world -> drone` TF path.
- Returns current pose and decimated recent path.
- Returns maneuver queue state, CustomOperation reference mode, and trajectory setpoint echo when available.

Verified:

- Python compile passed.
- Shared sampling path verified by `sim.observe_window` in rendered E2E.

### Backlog Item: `sim.render_snapshot`

Implemented and exposed as MCP tool `sim.render_snapshot`.

Capabilities:

- Keeps explicit `custom` camera pose support.
- Adds view presets: `follow_drone`, `topdown`, `corridor`, `target`, and `perception_fov`.
- Returns artifact path, camera pose quaternion, topic, image size, mode, and nonblank bounding box.

Verified:

- Live Gazebo preset test captured `preset_topdown_test.png`.
- Rendered E2E captured topdown, follow-drone, corridor, and explicit Gazebo snapshots.

### Backlog Item: `sim.render_snapshot_set`

Implemented and exposed as MCP tool `sim.render_snapshot_set`.

Capabilities:

- Captures multi-view bundles using named presets.
- Returns all artifact paths and camera poses.

Verified:

- Rendered E2E `sim.observe_window` captured start and end snapshot bundles.

### Backlog Item: `sim.plot_state`

Implemented and exposed as MCP tool `sim.plot_state`.

Capabilities:

- Generates top-down path/corridor/conductor plot.
- Generates side-view altitude/conductor-height plot.
- Generates nearest-conductor-distance plot.
- Works from live samples or provided `path_samples`.

Verified:

- Light MCP batch wrote plot PNGs from supplied path samples.
- Rendered E2E wrote `custom_operation_observation_topdown.png`, `custom_operation_observation_side.png`, and `custom_operation_observation_conductor_clearance.png`.

### Backlog Item: `sim.observe_window`

Implemented and exposed as MCP tool `sim.observe_window`.

Capabilities:

- Records live drone pose samples for a configured duration.
- Accepts supplied `path_samples` for replay/offline verdict generation.
- Optionally captures start/end rendered snapshot bundles.
- Writes diagnostic plots.
- Writes verdict JSON with sample count, distance traveled, altitude range, corridor membership, visibility state, and nearest conductor clearance.

Verified:

- Light MCP batch passed with provided path samples.
- Rendered E2E passed with live TF samples, rendered snapshots, plots, and verdict JSON.

### Backlog Item: E2E Integration

Implemented in `tools/III-Drone-MCP/config/e2e_smoke_batch.json`.

Capabilities:

- Uses `operation.activate` to activate the runtime CustomOperation PX4 mode id.
- Runs CustomOperation maneuver and hover.
- Runs `sim.observe_window` with rendered snapshots and plots.
- Captures perception topic data and explicit Gazebo snapshot.
- Lands, disarms, shuts down system, and stops simulation.

Verified:

- Rendered E2E batch passed end to end.
- Artifact directory: `/home/iii/ws/artifacts/mcp_e2e_observation_current`.

### Support Item: PX4 Simulation Param Reset

Implemented in `tools/simulation/launch_simulation_tools.sh`.

Capabilities:

- On simulation recreate, deletes persisted PX4 SITL parameter files by default so airframe defaults such as `COM_RC_IN_MODE=4` apply consistently.
- Can be disabled with `III_SIM_TOOLS_RESET_PX4_PARAMS_ON_RECREATE=0`.

Verified:

- Fixed PX4 preflight/arming path in rendered E2E.

### Support Item: Dynamic CustomOperation Activation

Implemented as MCP tool `operation.activate`.

Capabilities:

- Reads `/mission/custom_operation/status`.
- Extracts runtime `mode_id`.
- Sends PX4 nav-state command and verifies active nav state.

Verified:

- Live activation test succeeded with runtime mode id.
- Rendered E2E passed without hardcoded CustomOperation nav-state id.

### Support Item: MCP CLI Main Guards

Implemented for `iii_drone_mcp.mcp_call` and `iii_drone_mcp.mcp_batch`.

Capabilities:

- Allows direct `python3 -m iii_drone_mcp.mcp_call ...`.
- Allows direct `python3 -m iii_drone_mcp.mcp_batch ...`.

Verified:

- Light MCP calls and rendered E2E batch were executed through module entrypoints.

### Existing Capability: Gazebo External PNG Snapshot

Current MCP tooling can set an external Gazebo camera pose, render a frame, and save a PNG artifact.

Verified in the last E2E pass:

- artifact path: `/home/iii/ws/artifacts/mcp_e2e_current/rendered_external_snapshot.png`
- image format: PNG
- resolution: `1280x720`
- image was nonblank

Remaining work:

- Wrap this low-level capability in diagnostic view presets.
- Return richer camera metadata.
- Use snapshots as evidence alongside structured geometry and plots.

### Existing Capability: Topic Capture

Current MCP tooling can record topic messages by count or by time window.

Verified in the last E2E pass:

- topic: `/perception/pl_mapper/powerline`
- artifact path: `/home/iii/ws/artifacts/mcp_e2e_current/powerline_mapper_powerline.yaml`

Remaining work:

- Connect captured perception output to expected visibility and geometry verdicts.

### Existing Capability: MCP E2E Scenario Harness

Current MCP batch tooling can run a broad rendered simulation scenario end to end.

Verified coverage:

- rendered simulation restart with PX4 readiness gate
- supervision daemon clean restart, system boot, and system start
- PX4 health, arm, takeoff, CustomOperation handoff, land, and disarm
- CustomOperation `fly_relative` and `hover`
- PL mapper start and topic capture
- external Gazebo PNG snapshot
- full system shutdown and simulation stop

Remaining work:

- Add observation-window verdicts to the scenario.
- Use plot and geometry artifacts as first-class evidence.

## Suggested Verdict Shape

```json
{
  "success": true,
  "summary": "Drone entered CustomOperation, moved 0.21 m east, maintained 1.31-1.34 m altitude, remained in corridor, and captured expected perception output.",
  "checks": {
    "target_reached": true,
    "mode_stable": true,
    "corridor_membership_expected": true,
    "minimum_conductor_clearance_ok": true,
    "perception_visibility_expected": true,
    "perception_detection_present": true,
    "snapshot_artifacts_valid": true
  },
  "metrics": {
    "distance_traveled_m": 0.21,
    "altitude_min_m": 1.31,
    "altitude_max_m": 1.34,
    "nearest_conductor_distance_min_m": 2.4
  },
  "artifacts": {
    "snapshot_topdown": "...",
    "snapshot_follow": "...",
    "plot_path": "...",
    "plot_distance": "..."
  }
}
```
