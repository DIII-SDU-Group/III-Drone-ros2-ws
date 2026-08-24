# III-Drone ROS2 Workspace Documentation

This `docs/` folder captures a full workspace-level technical overview of the project at:
`/home/ffn/Workspace/III-Drone-ros2-ws`.

## Document Map

1. `workspace-overview.md`
   High-level architecture, repo composition, and system boundaries.

2. `submodules-and-packages.md`
   Submodule inventory, package purposes, build types, and dependency relationships.

3. `build-and-environments.md`
   Devcontainer setup, native onboard runtime assumptions, build flow, runtime environment variables, and deployment context.

4. `runtime-launch-and-node-graph.md`
   Runtime orchestration, canonical launch graph, system profiles, and communication patterns.

5. `interfaces-reference.md`
   Project-specific ROS interfaces (actions/messages/services) and their operational roles.

6. `configuration-system.md`
   Configuration architecture, parameter file strategy, configuration server/client behavior, and parameter semantics.

7. `mission-and-behavior-layer.md`
    Mission package internals, PX4 mode integration, behavior tree execution model, and mission specification flow.

8. `core-control-perception.md`
   Core package deep-dive: perception pipeline, maneuver stack, control references, and adapter architecture.

9. `supervision-and-process-management.md`
    System-manager daemon, daemon-managed services, dependency-based lifecycle management, managed process wrappers, and supervision model.

10. `simulation-and-px4-integration.md`
    Gazebo/PX4 integration, bridge paths, simulation assets, and SITL-related mechanics.

11. `ground-control-and-operator-tools.md`
    Ground control GUI behavior, CLI/tooling scripts, and operator/developer workflows.

12. `repo-boundary-map.md`
   Recommended target split between workspace-owned integration glue and separate reusable repos.


13. `dependency-governance.md`
   Dependency lock model for submodules, team workflow, and CI enforcement.

14. `testing.md`
   III-only test command set, generated TypeScript freshness checks, frontend
   verification, and GC compose smoke commands.

15. `findings-risks-and-clarifications.md`
    Observed inconsistencies, technical risks, and clarification questions for follow-up.

16. `perception-dataset-flight-suite.md`
    Exact-topic simulated perception dataset flights, artifact layout, safety recovery, resumption, and verification.

17. `field-inspection-operations.md`
    Authoritative real-aircraft operator workflow, authority boundaries,
    takeover behavior, link loss, and stop criteria.

18. `host-development-commands.md`
    Workspace-root commands for devcontainer, simulation, III CLI, tmux, and
    ground-control operation from the development host.

Domain language and context ownership are indexed by the root
`CONTEXT-MAP.md`.

## Scope Notes

- This documentation is generated from the local checked-out code and scripts in this workspace.
- It intentionally focuses on architecture, behavior, and integration points useful for continued engineering work.
- Generated artifacts (`build/`, `install/`, `log/`) are excluded from functional architecture except where relevant to runtime behavior.
