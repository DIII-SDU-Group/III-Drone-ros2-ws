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

This synchronizes the clean source components directly, builds on the Pi, and
restarts the existing supervised runtime. It leaves unrelated dirty components
local; use `--path src/<component>` for an intentional work-in-progress
transfer, and `--dry-run` to preview it. There is no release packaging or
cross-compilation requirement for ordinary research iteration.
