# MCP E2E Friction Log

This file is intentionally temporary. Add every bug, missing tool, custom operational command, caveat, and workflow friction here during the active loop. Clear it only after all entries are addressed and verified.

## Open

- None.

## Last Cleared

Cleared after the rendered MCP end-to-end scenario completed successfully on 2026-05-07.

Verified coverage:
- rendered simulation restart with PX4 readiness gate
- supervision daemon clean restart, system boot, and system start
- PX4 health, arm, takeoff with minimum-altitude postcondition, CustomOperation nav-state handoff, land, and disarm
- CustomOperation `fly_relative` and `hover` primitives through the maneuver execution system
- PL mapper start and topic message capture from `/perception/pl_mapper/powerline`
- external Gazebo camera snapshot capture to PNG
- entity-based supervised log capture
- full system shutdown and simulation stop

Artifacts from the successful pass:
- `/home/iii/ws/artifacts/mcp_e2e_current/mcp_batch_progress.jsonl`
- `/home/iii/ws/artifacts/mcp_e2e_current/powerline_mapper_powerline.yaml`
- `/home/iii/ws/artifacts/mcp_e2e_current/rendered_external_snapshot.png`
- `/home/iii/ws/artifacts/mcp_e2e_log_probe/maneuver_controller_entity.log`
