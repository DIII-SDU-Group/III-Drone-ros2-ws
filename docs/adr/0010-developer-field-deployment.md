# ADR 0010: Developer Field Deployment

## Decision

The III research aircraft is an editable developer host.  Deployment uses
ordinary SSH, rsync, an on-target workspace build, and normal systemd runtime
services.

The former production-oriented receiver, signed bundle, trust store,
per-machine enrollment, replay nonce, immutable release selector, firewall
policy, systemd sandbox, evidence gate, and reimage-only credential recovery
model are removed.

## Consequences

Field iteration is fast and transparent: a developer can inspect and edit the
same workspace the runtime executes.  A faulty source change can still be
diagnosed with normal systemd logs and redeployed immediately.

Physical flight safety remains independent of deployment: deployment never
arms the aircraft or writes PX4 firmware/parameters.
