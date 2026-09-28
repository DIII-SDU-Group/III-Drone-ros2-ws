# Build and Environments

Development and field iteration use the same source tree.

The Core library defaults `III_DRONE_OPTIMIZE_CONTROL_KERNELS` to `ON`. In
GNU/Clang Debug builds it adds `-O2` only to the bounded trajectory planner
and kinematic stop translation units, keeping debug symbols and assertions
while reducing their execution cost. Other files and configurations retain
their normal build flags. Set
`-DIII_DRONE_OPTIMIZE_CONTROL_KERNELS=OFF` for source-level debugging of those
two kernels.

| Environment | Role |
| --- | --- |
| Native workstation (`dev` GC install) | Installed GUI, pinned QGroundControl, and `iii` routing to SIM devcontainer or HIL Pi. |
| Workstation devcontainer | Build, test, simulation, and source editing. |
| Raspberry Pi | Editable `/home/iii/ws` workspace for real, OptiTrack, and HIL runs. |
| Field ground computer (`deploy` GC install) | Installed GUI, pinned QGroundControl, and `iii` routing to the selected Pi over SSH. |
| PX4 | Flight controller connected to the Pi over its dedicated Ethernet link. |

Install the native ground-computer components from this checkout with
`python3 scripts/install_gc.py --profile dev` on the workstation or
`--profile deploy` on a Linux x86_64 field computer. See the
[ground-computer guide](ground-computer-installation.md) for prerequisites,
pin, paths, and runtime-target selection. The `dev`/`deploy` setting selects
computer capabilities; it does not change the onboard runtime profile.

For a Pi change, use:

```bash
iii deploy dev --host <pi-host-or-ip> --build --restart
```

`--build` runs the repository's pinned ARM64 cross-builder on the workstation,
including `px4_msgs` and the runtime packages, builds the Pi's Micro XRCE-DDS
agent against the pinned ARM64 Fast-DDS sysroot, then synchronizes the
resulting install tree to `/home/iii/ws/install` and restarts the existing
supervised runtime. The Pi is never used as a compiler. The cross-build is
cached in the workspace cache and is incremental on subsequent changes; the
builder image is `iii-arm64-cross-builder:p1` (override with
`III_CROSS_BUILDER_IMAGE` when needed).

Without `--build`, this remains a source/config synchronization only. When
building, all current source components are included so an intentional dirty
change cannot be omitted from the matching install tree. Use `--path` to limit
the source synchronization, and `--dry-run` to preview the SSH/rsync and local
cross-build commands. There is no release packaging, signing, receiver, or
Pi-side build step in this developer workflow.
