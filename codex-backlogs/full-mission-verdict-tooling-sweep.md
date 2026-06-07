# Full Mission Verdict Tooling Sweep Backlog

## Context

This sweep addresses the full rendered mission test issues and frictions recorded in `/tmp/iii_drone/full_mission_rendered_20260511_142901/issues_and_frictions.md`, explicitly excluding the underlying `hover_on_cable` awareness validation rejection. That maneuver bug remains a known mission behavior issue to handle later.

Goal:
- A rendered `reach_cable` mission test must fail when the mission fails internally, even if PX4 remains safe and the vehicle stays in the expected corridor.
- Mission observation tools should expose mission semantic state in verdicts, not only geometry and PX4 safety.
- Full mission testing should be reusable via checked-in MCP batch config and a robust runner pattern that avoids ROS setup shell pitfalls.
- PX4 ULog event records should expose a canonical human-readable `message` field in addition to any legacy fields.
- Known `rclpy` service-response timeout warnings should be captured with better attribution or at least represented as explicit non-fatal diagnostic noise, not silently mixed with unrelated stderr.

Relevant code:
- `tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`
  - `_SafetyMonitor.after_call()` samples PX4 and node crashes but currently does not inspect mission-mode failure logs/status.
  - `_SafetyMonitor.finalize()` writes `mcp_batch_safety_summary.json` and scans PX4 ULog critical events.
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`
  - `activate_mission_mode()` activates a registered PX4 mode but returns before semantic mission completion.
  - `_sim_observe_window()` computes geometry/PX4-failsafe verdicts but does not validate mission mode status/result.
  - `_collect_observation_state()` already samples `/mission/modes/reach_cable/status`.
  - `px4_ulog_events()` currently emits `classified_events` with `line`, not canonical `message`.
- `tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py`
  - Tool schema for `sim.observe_window` must expose any new mission-state expectation arguments.
- Existing reusable configs live under `tools/III-Drone-MCP/config/`.

Constraints:
- Do not fix or change the `hover_on_cable` target-id/awareness rejection in this sweep.
- Do not weaken strict safety for PX4 safety failures.
- Keep artifacts in `/tmp/iii_drone`.
- Only run III package builds/tests.

## Incomplete


## In-Progress



## Completed

### T0: Make Strict Safety Fail On Mission Semantic Failures

Description:
Extend `_SafetyMonitor` in `tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py` so strict safety records mission-semantic failure events before cleanup. It should catch mission mode failures and behavior/maneuver action rejections from captured mission/maneuver logs after each call/finalize.

Use log scanning because that is the evidence currently available and does not require changing mission executor architecture. Add targeted patterns for:
- `Mode <name> failed with result`
- `Goal was rejected by server`
- `ManeuverActionNode::onFailure(): ... rejected`
- `ManeuverServer::handleGoal(): ... rejecting goal`

Ignore events older than the batch start, similar to `_critical_node_failures()`. Keep dedupe state to avoid recording the same log line repeatedly.

Acceptance:
- [x] Strict safety records a `mission_mode_failure` or `mission_action_rejected` event for the recorded artifact log text.
- [x] A strict batch exits nonzero when these events occur before cleanup.
- [x] Existing node-crash and PX4 ULog critical detection still work.
- [x] The implementation does not require fixing `hover_on_cable`.

Tests:
- Add or run a direct Python check against synthetic log text or a temporary artifact log containing the recorded failure lines.
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`

Implementation notes:
- Added timestamped mission-log scanning to `_SafetyMonitor` for mission mode failures and action/maneuver goal rejection lines.
- Mission failures are deduped and ignored when older than batch start or after cleanup starts.
- Finalize also scans mission logs, so failures discovered after observation still affect strict safety.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py` passed in the devcontainer.
- Synthetic log test produced one `mission_mode_failure` and three `mission_action_rejected` events from the recorded failure line shapes.


### T1: Add Mission Semantic Checks To Observation Verdicts

Description:
Extend `sim.observe_window` in `agent_tools.py` and its schema in `mcp_server.py` with optional mission expectation arguments:
- `expected_mission_mode`: default absent; currently useful value `reach_cable`.
- `expected_mission_success`: optional boolean.
- `fail_on_mission_failure`: optional boolean default `false`.

The observe verdict should inspect mission status from sampled state when available and include a compact `mission` section in `verdict["metrics"]` or `verdict["checks"]`. If a sampled mission status reports `tree_finished=true` and `tree_success=false`, `fail_on_mission_failure=true` must make the tool fail. If `expected_mission_success=true`, the verdict must require a successful finished mission status when present; if no terminal status is observed, include a clear check/metric showing it was not confirmed.

Acceptance:
- [x] `sim.observe_window` output includes mission status summary when sampled mission status is available.
- [x] With `fail_on_mission_failure=true`, a sampled failed mission status makes the tool return `success=false`.
- [x] With `expected_mission_success=true`, lack of confirmed success is visible and fails the tool.
- [x] Existing geometry/PX4 checks continue to work when no mission expectation arguments are supplied.

Tests:
- Direct Python/unit-style check of verdict helper behavior if factored out, or a scripted invocation using supplied `path_samples` with embedded mission status data.
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py`

Implementation notes:
- Added mission status summarization for sampled `/mission/modes/<mode>/status` payloads.
- `sim.observe_window` now accepts `expected_mission_mode`, `expected_mission_success`, and `fail_on_mission_failure`.
- Mission summary is included in verdict metrics whenever mission status samples are available.
- Existing observation behavior is unchanged when mission expectation arguments are omitted.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py` passed in the devcontainer.
- Direct helper checks verified failed, running, and succeeded mission status samples are summarized correctly.


### T2: Normalize PX4 ULog Event Message Field

Description:
Update PX4 ULog classified event records in `agent_tools.py` so each event has a canonical `message` field equal to the cleaned event text. Preserve `line` for backward compatibility. Update strict safety event messages in `mcp_batch.py` to prefer `message` over `line`.

Acceptance:
- [x] `px4_ulog_events()` classified events include both `message` and `line`.
- [x] `mcp_batch_safety_summary.json` warning/critical events are readable by consumers expecting `message`.
- [x] Existing `line` consumers remain compatible.

Tests:
- Direct Python classification check.
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`

Implementation notes:
- `_classify_px4_ulog_event()` now emits `message` and legacy `line` with the same cleaned text.
- Strict safety critical event text now prefers `message` and falls back to `line`.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py` passed in the devcontainer.
- Direct classifier check verified `message`, `line`, and critical setpoint classification.


### T3: Add A Reusable Rendered Full Mission Batch Config

Description:
Add a checked-in MCP batch config under `tools/III-Drone-MCP/config/` for the rendered full `reach_cable` mission scenario. It should mirror the successful operational sequence from the recorded run, use strict-safety-compatible cleanup, and include mission semantic expectations from T1.

The batch should:
- restart rendered simulation
- daemon restart, boot, start system
- wait for PX4 health
- update powerline overview
- arm and takeoff
- capture pre-mission snapshot set
- activate `reach_cable`
- run observation timeline and observe window with mission semantic expectations
- extract ULog events and logs
- land, disarm, shutdown, stop simulation as cleanup

Acceptance:
- [x] Config exists at `tools/III-Drone-MCP/config/full_mission_rendered_reach_cable.json`.
- [x] It uses only MCP tools, no custom shell control steps inside the scenario.
- [x] It includes cleanup markers for land/disarm/system shutdown/simulation stop.
- [x] It uses mission semantic checks so the current known `hover_on_cable` rejection causes a nonzero batch rather than a false pass.

Tests:
- Validate JSON parses.
- Dry inspect tool names against MCP specs if feasible.

Implementation notes:
- Added `tools/III-Drone-MCP/config/full_mission_rendered_reach_cable.json`.
- The config mirrors the rendered full mission sequence and uses the T1 mission semantic observe checks.
- Cleanup calls are marked with `cleanup=true` for land, disarm, system shutdown, and simulation stop.

Verification:
- `python3 -m json.tool tools/III-Drone-MCP/config/full_mission_rendered_reach_cable.json` passed.
- Tool-name dry validation against MCP specs passed for all 21 calls.
- Cleanup call order and mission semantic expectation fields were checked programmatically.


### T4: Add Robust Batch Runner For Checked-In Configs

Description:
Add a small runner script under `tools/III-Drone-MCP/bin/` that runs a checked-in batch config in the devcontainer/runtime environment using the established safe sourcing pattern. It must avoid `set -u` around ROS setup files and keep artifacts under `/tmp/iii_drone/<scenario>_<timestamp>` by default.

The script should support:
- config path argument, defaulting to the full rendered mission config from T3
- `--strict-safety`
- `--always-run-cleanup`
- forwarding additional `mcp_batch` args
- printing `ARTIFACT_DIR=` and `EXIT_CODE=`

Acceptance:
- [x] Runner avoids the `AMENT_TRACE_SETUP_FILES` nounset failure.
- [x] Runner works from host by entering the devcontainer when available, like existing MCP scripts.
- [x] Runner prints artifact path and preserves stdout JSON in `batch_output.json`.
- [x] Default run target is the full rendered mission config.

Tests:
- `bash -n tools/III-Drone-MCP/bin/run_mcp_batch_config.sh`
- A lightweight config run with `[]` or a simple status config if runtime is available.

Implementation notes:
- Added `tools/III-Drone-MCP/bin/run_mcp_batch_config.sh`.
- The runner defaults to `tools/III-Drone-MCP/config/full_mission_rendered_reach_cable.json`.
- It auto-enters the devcontainer when available, maps workspace-local absolute config paths to container-relative paths, and intentionally avoids `set -u` around ROS setup.
- It defaults to `--strict-safety --always-run-cleanup` unless `III_DRONE_MCP_BATCH_DEFAULT_STRICT=0` is set.

Verification:
- `bash -n tools/III-Drone-MCP/bin/run_mcp_batch_config.sh` passed.
- Host-to-devcontainer lightweight empty batch run passed and printed `ARTIFACT_DIR` plus `EXIT_CODE=0`.


### T5: Attribute Or Summarize MCP stderr Warning Noise

Description:
Improve how `mcp_batch` handles repeated `rclpy/service.py: RuntimeWarning: failed to send response (timeout)` stderr noise. Do not hide it silently. Add a post-run stderr summary artifact, for example `mcp_batch_stderr_summary.json`, that counts known warning classes and records whether unknown stderr remains.

This should make the warning an explicit diagnostic/friction item without polluting the main result. If stderr redirection is disabled with `--log-stderr`, no summary is required.

Acceptance:
- [x] When stderr is redirected, `mcp_batch_stderr_summary.json` is written.
- [x] Repeated rclpy service response timeout warnings are counted under a stable key.
- [x] Unknown stderr lines are counted and sampled.
- [x] Summary artifact path is included in strict safety artifact paths.

Tests:
- Direct helper test with a temporary stderr log containing repeated known warnings and unknown lines.
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`

Implementation notes:
- Added `_summarize_stderr()` and `_write_stderr_summary()` to `mcp_batch.py`.
- Redirected stderr path is now tracked, summarized, and written to `mcp_batch_stderr_summary.json`.
- Strict safety summary embeds the stderr summary and includes the summary artifact path.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py` passed in the devcontainer.
- Direct helper test counted one `rclpy_service_response_timeout` warning and one unknown stderr line correctly.
- Runner smoke with `offline_observation_tests.json` wrote `mcp_batch_stderr_summary.json` and included it in strict safety artifact paths.


### T6: Classify Fast DDS Multicast Network Stderr Noise

Description:
Final full mission verification produced repeated `Exception sending a multicast message:Network is unreachable` lines in `mcp_batch_stderr.log`. Extend the stderr summarizer so this known middleware/network-environment warning is counted separately instead of reported as unknown stderr.

Acceptance:
- [x] `mcp_batch_stderr_summary.json` counts Fast DDS multicast network-unreachable lines under a stable key.
- [x] A log containing only rclpy service-response warnings plus Fast DDS multicast warnings has `unknown_line_count=0`.

Tests:
- Direct `_summarize_stderr()` helper check.
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`

Implementation notes:
- Added `fastdds_multicast_network_unreachable` to known stderr warning counts.

Verification:
- `python3 -m py_compile tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py` passed in the devcontainer.
- Direct helper check verified one rclpy service warning plus two Fast DDS multicast warnings produce `unknown_line_count=0`.
- Re-summarizing the final full mission stderr log now reports 7 `rclpy_service_response_timeout`, 92 `fastdds_multicast_network_unreachable`, and `unknown_line_count=0`.

