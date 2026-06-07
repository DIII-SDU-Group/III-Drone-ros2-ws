# Rendered Mission Observability Hardening Backlog

## Incomplete

None.

## In-Progress

None.

## Completed

### 1. Classify PX4 ULog failsafe events in strict safety

Completed.

Changed files:
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`
- `tools/III-Drone-MCP/iii_drone_mcp/mcp_batch.py`

Validation:
- Compiled MCP Python package in devcontainer.
- Verified representative event classification: runtime failsafe/RTL-unresponsive are critical, preflight is warning, landing is info.
- Ran an empty strict batch against latest ULog and verified `px4_ulog_critical` makes safety summary fail with nonzero batch exit.

### 2. Make `perception_fov` snapshot audit semantics match the view

Completed.

Changed files:
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`

Validation:
- Compiled MCP Python package in devcontainer.
- Re-ran semantic audit against the prior `perception_fov` image with conductor-visible/drone-absent projection and verified success without `drone_projected_visible`.

### 3. Add timeline warmup before sample zero

Completed.

Changed files:
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`
- `tools/III-Drone-MCP/iii_drone_mcp/mcp_server.py`

Validation:
- Compiled MCP Python package in devcontainer.
- Ran a short `sim.observation_timeline` with `warmup_sec`; schema remained stable without active simulation and output includes `warmup_sec`.

### 4. Mark cached topic data stale

Completed.

Changed files:
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`

Validation:
- Compiled MCP Python package in devcontainer.
- Injected an old cached message and verified metadata includes `age_sec`, `stale_after_sec`, and `stale: true`.

### 5. Reduce non-fatal mission warning noise

Completed.

Changed files:
- `src/III-Drone-Mission/src/behavior/condition_nodes/store_current_state_condition_node.cpp`
- `src/III-Drone-Mission/src/behavior/condition_nodes/verify_powerline_detected_condition_node.cpp`
- `src/III-Drone-Core/src/control/maneuver/maneuver_scheduler.cpp`
- `src/III-Drone-Core/src/control/maneuver/hover_on_cable_maneuver_server.cpp`

Validation:
- Built `iii_drone_core` in devcontainer.
- Built `iii_drone_mission` in devcontainer.

### 6. Make MCP system tooling robust to missing workspace setup

Completed.

Changed files:
- `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`

Validation:
- Compiled MCP Python package in devcontainer.
- Ran `system status` through MCP without `setup/setup_dev.bash` on `PATH`; tool resolved `/home/iii/ws/tools/III-Drone-CLI/bin/iii` instead of failing with missing executable. The CLI then correctly reported missing setup environment (`CLI_CONFIGURATION environment variable is not set`), which is actionable.
