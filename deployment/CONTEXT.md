# III Developer Field Deployment

Deployment exists to make rapid research iteration easy.  The aircraft is an
editable developer machine, not a release appliance.

## Working model

- The Pi workspace is `/home/iii/ws` and is owned by `iii`.
- `iii` is an ordinary interactive SSH account with passwordless sudo.
- `iii deploy dev` uses normal SSH and `rsync` to copy changed source,
  setup, or tooling paths.  `--build` cross-builds the ARM64 runtime and the
  Micro XRCE-DDS agent on the workstation, synchronizes their install tree,
  and `--restart` restarts the normal supervised runtime services.  The Pi is
  never used as a compiler.
- `iii host provision` applies the ordinary Ansible developer-host playbook.
- `iii host image write` writes an explicitly selected image to an explicitly
  selected removable device.
- There are no release slots, deployment receiver, signing keys, trust stores,
  enrollment, nonces, immutable selectors, firewall policy, or host-contract
  activation gate.

## Boundaries retained for flight work

Deployment does not arm a vehicle, write PX4 firmware, or change PX4 parameters.
Those actions remain deliberate flight-system operations.  Read-only PX4 link
inspection is available through `iii px4 inspect`.

The direct Pi--PX4 Ethernet topology and HIL workstation link are kept because
they are required for communication, not because they are access controls.
An optional Wi-Fi client (for example the OptiTrack lab network) is provisioned
only on request; its passphrase stays on the Pi and never enters the repository
or a command line.
