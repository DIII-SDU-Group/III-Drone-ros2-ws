# Rendered Mission Observed Issues Backlog

## Context

This backlog processes the remaining issues observed after the clean rebuild and rendered `reach_cable` mission run.

Latest validation artifact:
- `/tmp/iii_drone/rendered_mission_clean_rebuild_20260511_131427`

Observed state after clean rebuild:
- All operational MCP batch calls succeeded: rendered simulation restart, system boot/start, PX4 health, powerline overview update, arm, takeoff, snapshots, `reach_cable` activation, observation, status, landing, disarm, system shutdown, simulation stop.
- Mission executor and maneuver controller exited cleanly during shutdown. The previous executor-association shutdown failure was resolved by rebuilding current source.
- Strict safety still failed because PX4 ULog reported `mc_pos_control` invalid setpoints and `Failsafe: stop and wait` during the `hover_on_cable` phase.
- ULog extraction used raw `strings`, which misclassified a binary fragment as a critical event and lost actual PX4 event severity.
- `perception.update_powerline_overview` rejected `timeout_sec`; it accepts `service_timeout_sec` instead.
- Runtime logs showed repeated PX4 time-sync jumps, one TF fallback warning in `VerifyPowerlineDetectedConditionNode`, and ROS service response timeout warnings in MCP stderr.

Important code findings:
- `HoverOnCableManeuverServer::GetReference()` currently returns `position={NaN,NaN,NaN}`, `yaw=NaN`, `velocity={NaN,NaN,target_z_velocity}`, `acceleration={NaN,NaN,NaN}`.
- PX4 ULog showed invalid setpoints around mission time 114-116s with `velocity[0]=NaN`, `velocity[1]=NaN`, `velocity[2]=-0.1`.
- `TrajectorySetpoint::getConfiguration()` enables position, velocity, acceleration, attitude, and rates, so partial NaN references must be generated carefully.
- `DroneAgentTools.update_powerline_overview()` uses `timeout_s` for the ROS request and `service_timeout_sec` for the service wait; many other MCP tools accept `timeout_sec`.
- `px4_ulog_events()` currently scans ULog bytes with `strings` and classifies text heuristically; `pyulog` and `ulog_messages` are available in the devcontainer.

Assumptions:
- `hover_on_cable` should command vertical motion while not commanding undefined horizontal velocity. Horizontal velocity should be finite zero when vertical velocity is active.
- The strict safety monitor should still fail a rendered mission when PX4 reports invalid setpoints or failsafe/recovery events, but it should not fail on binary garbage extracted from ULog files.
- Timing warnings should be reduced or accurately classified where locally possible; persistent PX4 time-sync warnings that come from SITL/micro XRCE should be reported as residual if not locally fixable without changing third-party/PX4 code.

## Incomplete

## In-Progress


## Completed

### T3: Reduce Remaining Timing and Service Warning Friction

Description:
Address the warnings that remain after the mission succeeds:
- `VerifyPowerlineDetectedConditionNode` emits a warning when a stamped transform is slightly in the future and latest-transform fallback succeeds.
- MCP stderr shows repeated ROS service response timeout warnings.
- PX4 ULog shows repeated `timesync` reset/no-longer-converged messages.

Use local fixes where appropriate:
- If latest-transform fallback succeeds, demote that specific TF fallback from warning to info/debug once, because the behavior is intentional and recovered.
- Improve MCP strict safety/reporting so PX4 time-sync warnings are classified as warnings and do not create false mission-failure verdicts by themselves.
- If service response timeout warnings are caused by overly short MCP sampling/service waits, adjust the MCP calls used by strict safety to avoid hammering lifecycle/service endpoints during cleanup.

Acceptance:
- [x] Successful latest-transform fallback is no longer a high-noise warning in mission logs.
- [x] PX4 time-sync messages are classified as warnings/startup/timing, not critical.
- [x] Strict safety output distinguishes residual warnings from mission-failing safety events.
- [x] No new mission executor or maneuver controller lifecycle failures are introduced.

Tests:
- Targeted build of touched package(s).
- Rendered mission MCP batch with strict safety.

Implementation notes:
- Demoted successful latest-transform fallback in `VerifyPowerlineDetectedConditionNode` from `WARN_ONCE` to `INFO_ONCE`.
- Updated MCP ULog classification so `time sync converged` is informational, while `time jump` and `time sync no longer converged` remain timing warnings.
- Extended strict safety output with separate `px4_warning_events`, `px4_critical_events`, and corresponding counts.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py` passed.
- `colcon build --base-paths src --packages-select iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` passed.
- Rendered mission strict run in devcontainer artifact `/tmp/iii_drone/rendered_mission_final_sweep_20260511_140709`: all operational batch calls succeeded, `safety.summary` passed, `event_count=0`, `px4_critical_event_count=0`, `px4_warning_event_count=7`.
- Latest mission log line for the recovered TF fallback is `INFO`, not `WARN`; older warning lines in the same captured log came from previous generations.
- Mission executor and maneuver controller latest run-end entries both have `returncode=0`.

Residual note:
- `mcp_batch_stderr.log` still contains repeated `rclpy/service.py: RuntimeWarning: failed to send response (timeout)` lines. The rendered mission passed, all MCP batch calls returned success, and no mission/maneuver lifecycle failure correlated with these warnings. This is preserved as non-blocking runtime noise rather than folded into the strict safety verdict.

### T2: Make PX4 ULog Event Extraction Severity-Aware

Description:
Replace or augment `strings`-based ULog event extraction in `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py::px4_ulog_events()` with a severity-aware path using `pyulog` logged messages when available, falling back to `ulog_messages`, then only falling back to `strings` as a last resort. Preserve artifact shape (`events`, `classified_events`, counts, max severity), but include parsed severity/time when available. Filter binary garbage such as `!6?rTL;I{`.

Acceptance:
- [x] Binary garbage from raw ULog data is not reported as a PX4 event.
- [x] `invalid setpoints` and `Failsafe: stop and wait` are reported with accurate warning/critical handling.
- [x] Strict safety still records a failure for invalid setpoints/failsafe-like events, but the event message is the real PX4 text.
- [x] `px4_ulog_events.json` records the extraction method used.

Tests:
- Run `px4_ulog_events()` against `/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/rootfs/log/2026-05-11/13_14_38.ulg` if present.
- Rendered mission MCP batch with strict safety after T0.

Implementation notes:
- `px4_ulog_events()` now uses `pyulog` logged messages as the primary extraction path and falls back to `strings` only if parsing fails.
- Classified events now include parsed `timestamp_s`, `log_level`, and PX4-native `px4_severity` when available.
- Safety classification still escalates invalid setpoints, failsafe, RTL/recovery, unresponsive, and no-response events to critical for strict mission validation.

Verification:
- `python3 -m py_compile` passed for MCP Python files.
- Parsed T0 ULog `/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/rootfs/log/2026-05-11/13_56_02.ulg`: extraction method `pyulog`, no binary `V;Rtl<`, critical count 0.
- Parsed earlier invalid-setpoint ULog `/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/rootfs/log/2026-05-11/13_14_38.ulg`: real `[mc_pos_control] invalid setpoints` and `[mc_pos_control] Failsafe: stop and wait` are reported as strict safety-critical with `px4_severity=warning`.


### T0: Fix HoverOnCable Invalid PX4 Setpoints

Description:
Update `src/III-Drone-Core/src/control/maneuver/hover_on_cable_maneuver_server.cpp` so `HoverOnCableManeuverServer::GetReference()` returns a PX4-valid velocity-mode setpoint. The current reference leaves x/y velocity as NaN while z velocity is finite, producing PX4 `mc_pos_control` invalid setpoints. Use finite zero x/y velocity with the configured z velocity. Keep acceleration/yaw behavior consistent unless inspection shows PX4 still rejects the setpoint.

Acceptance:
- [x] `hover_on_cable` no longer publishes trajectory setpoints with `velocity[0]` or `velocity[1]` as NaN while `velocity[2]` is finite.
- [x] Rendered `reach_cable` mission no longer logs PX4 `invalid setpoints` or `Failsafe: stop and wait`.
- [x] Mission still completes `reach_cable` successfully and shuts down cleanly.

Tests:
- `colcon build --base-paths src --packages-select iii_drone_core iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
- Rendered mission MCP batch with strict safety.
- Inspect ULog trajectory setpoints around `hover_on_cable`.

Implementation notes:
- Updated `HoverOnCableManeuverServer::GetReference()` to publish finite zero x/y velocity with the requested z velocity.

Verification:
- `colcon build --base-paths src --packages-select iii_drone_core iii_drone_mission --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` passed.
- Rendered mission T0 artifact: `/tmp/iii_drone/rendered_mission_t0_20260511_135551`. All operational batch calls succeeded.
- `ulog_messages` for `/home/iii/ws/PX4-Autopilot/build/px4_sitl_default/rootfs/log/2026-05-11/13_56_02.ulg` showed no `invalid setpoints` or `Failsafe: stop and wait`; remaining strict failure was a bogus strings-extracted `V;Rtl<` event covered by T2.


### T1: Normalize MCP Timeout Handling for Powerline Overview

Description:
Update `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py` and schema if needed so `perception.update_powerline_overview` accepts `timeout_sec` as the standard MCP service-call timeout alias. Preserve `timeout_s` for the ROS request payload and `service_timeout_sec` for explicit service timeout control.

Acceptance:
- [x] Calling `perception.update_powerline_overview` with `timeout_sec` does not raise `unexpected keyword argument`.
- [x] `timeout_s` still maps to `UpdatePowerlineOverview.Request.timeout_s`.
- [x] `service_timeout_sec` remains supported.

Tests:
- `python3 -m iii_drone_mcp.mcp_batch` smoke call against `perception.update_powerline_overview` with `timeout_sec` while system is running, or a direct Python signature/unit check if runtime is unavailable.

Implementation notes:
- Changed `DroneAgentTools.update_powerline_overview()` to accept `timeout_sec` as an alias when `service_timeout_sec` is not provided.
- Updated the MCP schema for `perception.update_powerline_overview` to advertise `timeout_sec`.

Verification:
- Direct Python signature/behavior check in devcontainer: `timeout_sec` and `service_timeout_sec` both accepted, and `timeout_s` still populates the ROS request.
