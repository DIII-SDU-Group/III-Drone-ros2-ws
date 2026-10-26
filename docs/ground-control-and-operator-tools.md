# Ground Control And Operator Tools

Ground control installs on a normal Linux x86_64 workstation or field laptop.
[Install it from the same checkout](ground-computer-installation.md) with the
standalone `scripts/install_gc.py` script. That install supplies the GUI,
checkout-pinned QGroundControl v5.0.8, and a native `iii` command. QGroundControl
communicates directly with PX4. Native `iii` routes runtime commands to the
matching SIM devcontainer or a selected Pi over SSH; the Pi/runtime CLI then
uses its local daemon and Runtime API.

There is no receiver gateway, signed field bundle, trusted-signer store, release
cache, or special deployment account. The normal development loop is:

```bash
python3 scripts/install_gc.py --profile deploy
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
