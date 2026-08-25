# Deployment Ownership And Offboard Builds

## Decision

Deployment infrastructure is workspace-owned integration code under `deployment/`
and is distributed as the ROS-independent `iii-deployment` Python package plus
declarative assets. The III CLI is the operator-facing thin client. Domain
repositories continue to own runtime code and source artifacts.

All application builds run on a supported operator computer or protected CI in a
pinned target-compatible environment. The Raspberry Pi never compiles III code,
and production does not require a source checkout, build tree, Docker, or
`/home/iii/ws`.

Host systemd retains top-level ownership of the receiver, III daemon, and runtime
API. The daemon and `system_spec.py` remain authoritative for the ROS launch graph;
deployment, Ansible, systemd, and GC tooling must not duplicate per-node ownership.

## Consequences

One contract implementation is shared by CLI, receiver, Ansible, CI, and tests.
Privilege boundaries use separate entry points and installed asset subsets, not a
forked policy library. A future source-independent repository may be considered
only after field operation is entirely artifact-driven.

