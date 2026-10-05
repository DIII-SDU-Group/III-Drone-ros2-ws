# III-Drone Documentation

Core runtime documentation remains in the package and subsystem guides:

- `runtime-launch-and-node-graph.md` for the supervised ROS graph.
- `simulation-and-px4-integration.md` for SITL, PX4, and HIL topology.
- `px4-hil-ethernet-baseline.md` for the one-time physical-PX4 HIL transport baseline.
- `opti-track-lab-readiness.md` for the OptiTrack lab facts, the motion-capture
  data flow, one-time commissioning (lab Wi-Fi, stack ROS domain, PX4 baseline
  `deployment/px4/opti-track.nsh`), and the acceptance boundary of the
  `opti_track` profile.
- `opti-track-lab-session-checklist.md` for the per-session OptiTrack lab flight
  procedure, the mission ladder, and the motion-capture outage response.
- `field-inspection-operations.md` for physical flight operation.
- `host-provisioning.md` for the editable Pi workflow.
- `deployment-hardware-roles.md` for the Pi, PX4, Wi-Fi, and OptiTrack links
  and the stack ROS domain.
- `ground-computer-installation.md` for the native operator workstation and
  field ground-computer install, pins, paths, and runtime routing.
- `host-development-commands.md` for everyday workstation commands.
- `adr/0010-developer-field-deployment.md` for the deployment decision.

Deployment is intentionally developer-first: ordinary SSH, rsync, an editable
Pi workspace, a cached workstation-side ARM64 cross-build, and normal systemd
services.  It does not use release signing, a receiver, trust stores,
qualification gates, or immutable release slots.
