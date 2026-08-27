# Architecture Decision Index

These accepted records explain load-bearing III-Drone architecture decisions.
They are not operator runbooks; current executable procedures live in the
[documentation index](../README.md).

- [ADR 0001: ROS-Native Operations Interface](0001-ros-native-operations-interface.md)
- [ADR 0002: Standalone Custom Operation Mode](0002-standalone-custom-operation-mode.md)
- [ADR 0003: Cable-Aware Flight In Core](0003-cable-aware-flight-in-core.md)
- [ADR 0004: MAVSDK For QGroundControl-Equivalent Tooling](0004-mavsdk-for-qgroundcontrol-equivalent-tooling.md)
- [ADR 0005: Simulation Control In Simulation Package](0005-simulation-control-in-simulation-package.md)
- [ADR 0006: Deployment Ownership And Offboard Builds](0006-deployment-ownership-and-offboard-builds.md)
- [ADR 0007: Immutable Release And Persistent State Separation](0007-immutable-release-and-persistent-state-separation.md)
- [ADR 0008: Release Status, Readiness, And Disaster Recovery](0008-release-status-readiness-and-disaster-recovery.md)
- [ADR 0009: Evidence-Gated Deployment Cutover](0009-evidence-gated-deployment-cutover.md)

New decisions receive the next numeric prefix and must link to the canonical
contract they change. Superseded records stay in this index with their historical
status; do not rewrite past rationale as current operating instructions.
