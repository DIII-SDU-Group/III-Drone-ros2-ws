# MCP End-to-End Test Approach

Follow this loop strictly until the broad operational scenario works without friction.

## Loop

1. Maintain `docs/mcp-e2e-friction-log.md` while testing.
2. Run the scenario using MCP tooling as the default operational path.
3. When any issue, bug, problem, missing affordance, unclear state, or workflow friction appears, write it to the friction log immediately.
4. When any custom CLI command or custom code is needed, decide whether it should be operational tooling. If yes, write it to the friction log.
5. Keep the test scope broad: simulation lifecycle, system lifecycle, PX4/QGroundControl-equivalent commands, CustomOperation maneuvers, perception control/data capture, Gazebo snapshots, geometry fixtures, ROS inspection, artifact handling, and shutdown.
6. When the test is done or blocked, close the environment.
7. Implement, fix, patch, or otherwise address every item in the friction log.
8. Verify the implementation against every friction-log item.
9. Clear the friction log only after all items are addressed and verified.
10. Repeat from step 2 until the scenario completes without friction.

## Scenario Baseline

1. Start rendered simulation.
2. Boot and start the supervised III system.
3. Verify micro-ROS/PX4 topic flow and supervised node readiness.
4. Verify PX4 health before arming.
5. Arm and take off through MCP tooling.
6. Activate CustomOperation mode through MCP/PX4 tooling.
7. Run small primitive CustomOperation maneuvers.
8. Start perception tooling.
9. Capture topic data and semantic perception outputs.
10. Set external Gazebo camera pose and capture rendered snapshots.
11. Inspect artifacts and verify expected conductor visibility from the known low-altitude simulation position.
12. Land, disarm, stop system, and stop simulation.

## Rule

No caveats are accepted as success. Every caveat becomes either completed tooling, a fixed bug, or an explicit remaining blocker in the friction log.
