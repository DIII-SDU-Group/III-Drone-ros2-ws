# MCP Observation Tooling Test Suite Backlog

## Goal

Build and run an exhaustive test suite for the MCP observation and simulation-control tools.

The suite proves:

- MCP tool registry exposes all expected tools.
- Geometry and visibility logic works without ROS/Gazebo.
- Replay/offline observation verdicts work from supplied samples.
- Rendered Gazebo snapshots are meaningful and nonblank.
- PX4, simulation, and CustomOperation control flows work in rendered and headless simulation.
- Observation artifacts are generated consistently.
- Failure modes are explicit and useful.

## Incomplete

- None.

## In Progress

- None.

## Complete

### Backlog Item: Static MCP And Config Checks

Implemented in `tools/III-Drone-MCP/iii_drone_mcp/observation_test_suite.py`.

Coverage:

- Python compile checks for MCP modules.
- JSON validation for MCP batch/config fixtures.
- MCP tool registry validation.
- Required tools:
  - `sim.geometry_state`
  - `sim.visibility_state`
  - `sim.trajectory_state`
  - `sim.render_snapshot`
  - `sim.render_snapshot_set`
  - `sim.plot_state`
  - `sim.observe_window`
  - `operation.activate`

Verification:

- Final full suite: `PASS static`.

### Backlog Item: Pure Geometry Unit Tests

Implemented in `phase_geometry`.

Coverage:

- Geometry fixture load.
- Conductor sample parsing.
- Corridor span/lateral/z range validation.
- Corridor membership checks for in-corridor, outside-corridor, and vertical-band poses.
- Nearest conductor geometry checks.
- Visibility checks for known visible pose, yaw-away pose, range gate, and FOV gate.

Verification:

- Final full suite: `PASS geometry`.

### Backlog Item: MCP Offline Replay Batch

Implemented in:

- `tools/III-Drone-MCP/config/offline_observation_tests.json`
- `phase_offline`

Coverage:

- `sim.geometry_state` with explicit pose.
- `sim.visibility_state` with explicit pose.
- `sim.plot_state` with supplied `path_samples`.
- `sim.observe_window` with supplied `path_samples`.
- Expected-failure verdict case with intentionally wrong corridor expectation.
- Artifact existence checks for plots and verdict JSON.

Verification:

- Final full suite: `PASS offline`.

### Backlog Item: Gazebo Snapshot Preset Tests

Implemented in:

- `tools/III-Drone-MCP/config/rendered_observation_e2e.json`
- `phase_snapshots`

Coverage:

- Rendered simulation restart.
- Snapshot presets:
  - `custom`
  - `topdown`
  - `follow_drone`
  - `corridor`
  - `target`
  - `perception_fov`
- `sim.render_snapshot_set`.
- PNG artifact existence, dimensions, file size, and nonblank bbox.
- Camera pose differentiation.
- Topdown/follow image hash differentiation.

Verification:

- Final full suite: `PASS snapshots`.

### Backlog Item: PX4 And Simulation Control Tests

Implemented across rendered and headless runtime phases.

Coverage:

- Rendered simulation restart/status/stop.
- Headless simulation restart/status/stop.
- PX4 persisted-param reset on simulation recreate.
- PX4 health.
- Arm.
- Takeoff.
- Land.
- Disarm.

Verification:

- Final full suite: `PASS rendered-e2e`.
- Final full suite: `PASS headless-e2e`.

### Backlog Item: CustomOperation Activation Tests

Implemented through `operation.activate` checks in rendered and headless E2E phases.

Coverage:

- `operation.status`.
- Runtime `mode_id` parsing from `/mission/custom_operation/status`.
- Activation without hardcoded nav-state id.
- Assertion that PX4 `nav_state == custom_operation_mode_id`.
- Clean exit through landing/disarm/shutdown.

Verification:

- Final full suite: `PASS rendered-e2e`.
- Final full suite: `PASS headless-e2e`.

### Backlog Item: CustomOperation Maneuver Observation Tests

Implemented in rendered and headless E2E phases.

Coverage:

- Arm/takeoff.
- Activate CustomOperation.
- Run `operation.fly_relative`.
- Run `sim.observe_window`.
- Validate pose samples, corridor verdict, conductor clearance metric, plots, snapshots where applicable, and verdict JSON.

Patch from testing:

- Raised rendered and headless maneuver test altitude to `2.0 m` so maneuver validation has enough AGL margin.

Verification:

- Final full suite: `PASS rendered-e2e`.
- Final full suite: `PASS headless-e2e`.

### Backlog Item: Perception Coupling Tests

Implemented in rendered E2E.

Coverage:

- Starts PL mapper.
- Records `/perception/pl_mapper/powerline`.
- Verifies perception artifact exists.
- Uses observation/visibility geometry in the same run.

Verification:

- Final full suite: `PASS rendered-e2e`.

### Backlog Item: Full Rendered E2E Batch

Implemented and hardened in:

- `tools/III-Drone-MCP/config/e2e_smoke_batch.json`
- `phase_rendered_e2e`

Coverage:

- Simulation start.
- System boot/start.
- PX4 health.
- Arm/takeoff.
- Dynamic CustomOperation activation.
- Maneuver execution.
- `sim.observe_window`.
- Perception topic capture.
- Gazebo snapshot.
- Land/disarm.
- System shutdown.
- Simulation stop.

Verification:

- Final full suite: `PASS rendered-e2e`.

### Backlog Item: Failure-Mode Tests

Implemented in `phase_failure_modes`.

Coverage:

- Bad geometry path.
- Unknown snapshot view.
- Gazebo stopped plus `sim.render_snapshot`.
- No TF plus live `sim.observe_window`.
- Impossible `expected_corridor`.
- Too-high `min_conductor_clearance_m`.
- CustomOperation inactive plus operation command.
- Simulation stopped plus PX4 health.

Verification:

- Final full suite: `PASS failure-modes`.

### Backlog Item: Headless Compatibility Tests

Implemented in:

- `tools/III-Drone-MCP/config/headless_observation_e2e.json`
- `phase_headless_e2e`

Coverage:

- Headless simulation restart.
- System boot/start.
- PX4 health/arm/takeoff.
- CustomOperation activation.
- `operation.fly_relative`.
- `sim.geometry_state`.
- `sim.visibility_state`.
- `sim.trajectory_state`.
- `sim.plot_state`.
- `sim.observe_window` with `capture_snapshots=false`.
- Headless rendered snapshot classification artifact.

Verification:

- Final full suite: `PASS headless-e2e`.

### Backlog Item: Artifact Audit

Implemented in `phase_audit`.

Coverage:

- Required JSON/PNG/YAML artifacts exist.
- PNGs are nonempty.
- No root-owned artifacts under the run artifact root.
- Simulation status reports stopped after suite.

Verification:

- Final full suite: `PASS audit`.

### Backlog Item: Test Runner Wrapper

Implemented in:

- `tools/III-Drone-MCP/iii_drone_mcp/observation_test_suite.py`
- `tools/III-Drone-MCP/bin/run_mcp_observation_tests.sh`
- `tools/III-Drone-MCP/config/offline_observation_tests.json`
- `tools/III-Drone-MCP/config/rendered_observation_e2e.json`
- `tools/III-Drone-MCP/config/headless_observation_e2e.json`

Runner behavior:

- Detects devcontainer from host workspace.
- Runs as `iii` inside the devcontainer.
- Sources ROS/install/setup environment.
- Archives artifacts under timestamped run directories.
- Prints compact pass/fail phase summary.

Verification:

- Final full suite command passed:

```bash
III_DRONE_HOST_WORKSPACE_ROOT="$(pwd)" tools/III-Drone-MCP/bin/run_mcp_observation_tests.sh --clean
```

Final artifact run:

```text
/home/iii/ws/artifacts/mcp_observation_suite/20260508_111238
```

Final phase result:

```text
PASS static
PASS geometry
PASS offline
PASS snapshots
PASS rendered-e2e
PASS headless-e2e
PASS failure-modes
PASS audit
```

