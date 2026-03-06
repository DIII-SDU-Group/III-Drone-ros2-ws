# III-Drone ROS2 Workspace Documentation

This `docs/` folder captures a full workspace-level technical overview of the project at:
`/home/ffn/Workspace/III-Drone-ros2-ws`.

## Document Map

1. `workspace-overview.md`
   High-level architecture, repo composition, and system boundaries.

2. `submodules-and-packages.md`
   Submodule inventory, package purposes, build types, and dependency relationships.

3. `build-and-environments.md`
   Container/dev setup, build flow, runtime environment variables, and deployment/cross-compile context.

4. `runtime-launch-and-node-graph.md`
   Runtime orchestration, launch files, node startup topology, and communication patterns.

5. `interfaces-reference.md`
   Project-specific ROS interfaces (actions/messages/services) and their operational roles.

6. `configuration-system.md`
   Configuration architecture, parameter file strategy, configuration server/client behavior, and parameter semantics.

7. `mission-and-behavior-layer.md`
    Mission package internals, PX4 mode integration, behavior tree execution model, and mission specification flow.

8. `core-control-perception.md`
   Core package deep-dive: perception pipeline, maneuver stack, control references, and adapter architecture.

9. `supervision-and-process-management.md`
    Supervisor behavior, dependency-based lifecycle management, managed process wrappers, and supervision YAML model.

10. `simulation-and-px4-integration.md`
    Gazebo/PX4 integration, bridge paths, simulation assets, and SITL-related mechanics.

11. `ground-control-and-operator-tools.md`
    Ground control GUI behavior, CLI/tooling scripts, and operator/developer workflows.

12. `repo-boundary-map.md`
   Recommended target split between workspace-owned integration glue and separate reusable repos.

13. `findings-risks-and-clarifications.md`
    Observed inconsistencies, technical risks, and clarification questions for follow-up.

## Scope Notes

- This documentation is generated from the local checked-out code and scripts in this workspace.
- It intentionally focuses on architecture, behavior, and integration points useful for continued engineering work.
- Generated artifacts (`build/`, `install/`, `log/`) are excluded from functional architecture except where relevant to runtime behavior.
