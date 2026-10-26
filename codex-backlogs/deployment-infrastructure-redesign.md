# Superseded: Production Deployment Redesign

This backlog was retired on 2026-09-17.  Its receiver, signing, qualification,
immutable-release, evidence-gating, restricted SSH, firewall, and recovery
decisions do not fit the III research drone's rapid-prototyping workflow.

The active implementation backlog is
[`developer-field-deployment.md`](developer-field-deployment.md).  The
canonical deployment model is an editable Pi workspace, ordinary SSH/rsync,
on-target builds, and normal systemd runtime services.  Physical flight safety
remains separate from deployment.
