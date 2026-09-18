# III-Drone Documentation

Core runtime documentation remains in the package and subsystem guides:

- `runtime-launch-and-node-graph.md` for the supervised ROS graph.
- `simulation-and-px4-integration.md` for SITL, PX4, and HIL topology.
- `px4-hil-ethernet-baseline.md` for the one-time physical-PX4 HIL transport baseline.
- `field-inspection-operations.md` for physical flight operation.
- `host-provisioning.md` for the editable Pi workflow.
- `host-development-commands.md` for everyday workstation commands.
- `adr/0010-developer-field-deployment.md` for the deployment decision.

Deployment is intentionally developer-first: ordinary SSH, rsync, an editable
Pi workspace, on-target builds, and normal systemd services.  It does not use
release signing, a receiver, trust stores, qualification gates, or immutable
release slots.
