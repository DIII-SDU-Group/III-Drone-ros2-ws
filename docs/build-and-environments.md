# Build and Environments

Development and field iteration use the same source tree.

| Environment | Role |
| --- | --- |
| Workstation devcontainer | Build, test, simulation, and source editing. |
| Raspberry Pi | Editable `/home/iii/ws` workspace for real, OptiTrack, and HIL runs. |
| PX4 | Flight controller connected to the Pi over its dedicated Ethernet link. |

For a Pi change, use:

```bash
iii deploy dev --host <pi-host-or-ip> --build --restart
```

This synchronizes source directly, builds on the Pi, and restarts the existing
supervised runtime.  Use `--path` for a focused transfer and `--dry-run` to
preview it.  There is no release packaging or cross-compilation requirement for
ordinary research iteration.
