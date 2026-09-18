# Ground Control And Operator Tools

Ground control is a normal development workstation. QGroundControl communicates
directly with PX4, while the `iii` CLI communicates with the Pi through ordinary
SSH and the runtime API.

There is no receiver gateway, signed field bundle, trusted-signer store, release
cache, or special deployment account. The normal development loop is:

```bash
source setup/setup_field.bash
iii deploy dev --host iii.local --build --restart
iii host inspect --host iii.local
iii px4 inspect --host iii.local
```

For a fresh Pi, run `iii host provision --host iii.local` after it has network
access and a normal `iii` login. Use QGroundControl directly for explicit PX4
firmware and parameter work; the CLI’s PX4 command is inspection-only.

No command here arms the vehicle or controls motors. Validate the selected
simulation, OptiTrack, HIL, or field setup separately before flight.
