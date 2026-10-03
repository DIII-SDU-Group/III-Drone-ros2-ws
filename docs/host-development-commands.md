# Host Development Commands

`iii-dev` is the workspace-root command for SIM/HIL orchestration, container
access, and development tmux helpers. Install the [native `dev`
ground-computer profile](ground-computer-installation.md) first. The installed
`iii` command owns runtime, API, and recorder controls; the installed GC
launcher owns the web UI. PX4/Gazebo remains owned by the simulation launcher
and the ROS graph by the III daemon.

## Prerequisites

- Run commands from this checkout on the development host.
- Run `python3 scripts/install_gc.py --profile dev` on the host and put
  `~/.local/bin` on `PATH`.
- Docker must be installed and accessible to the current user.
- The workspace devcontainer must either exist or be creatable through the Dev
  Container CLI. `container up` uses an installed `devcontainer` executable and
  falls back to `npx --yes @devcontainers/cli`.
- X11/GPU access is established by the devcontainer configuration when Gazebo
  and QGroundControl are required.

Container discovery uses the exact
`devcontainer.local_folder=<canonical-workspace-path>` label and rejects zero or
multiple running matches. Commands run as `iii` in `/home/iii/ws` after sourcing
`setup/setup_dev.bash`.

Every `iii-dev` command group supports `-h` and `--help` without contacting
Docker. Use `iii --help` for ordinary runtime commands.

## Normal Simulation Workflow

Start the complete rendered operator stack:

```bash
./iii-dev stack start
```

This starts the simulation without attaching, waits for PX4 and Gazebo
transport, boots and starts the daemon-owned III graph, starts and health-checks
the runtime API, and then starts ground control. It does not recreate an
already-running simulator or clear PX4 parameters.

Useful views:

```bash
./iii-dev stack status
./iii-dev sim attach
iii --runtime-target sim system attach
~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh logs
```

Stop all three ownership domains in reverse order:

```bash
./iii-dev stack stop
```

The supervision daemon remains systemd-owned and available after runtime
shutdown. The runtime API is stopped with the operator stack, and `stack stop`
does not stop the devcontainer.

## Explicit Operations

Container and configured shell access:

```bash
./iii-dev container status
./iii-dev container up
./iii-dev container down
./iii-dev shell
./iii-dev exec ros2 node list
```

`container down` stops the running devcontainer associated with this checkout.
It leaves other workspaces' containers alone and succeeds if this one is already stopped.

Simulation operations:

```bash
./iii-dev sim start
./iii-dev sim start --headless
./iii-dev sim restart
./iii-dev sim attach
./iii-dev sim status
./iii-dev sim stop
```

`sim restart` is deliberately destructive to the selected SITL instance: it
recreates the canonical simulation session and applies the simulation
launcher's PX4 parameter-reset policy. `sim start` is idempotent and does not
attach to tmux. `sim attach` never creates a missing session.

The native installed CLI checks this checkout's devcontainer identity and
routes runtime commands into it. Select SIM explicitly from a native shell:

```bash
iii --runtime-target sim system boot
iii --runtime-target sim system start
iii --runtime-target sim system status
iii --runtime-target sim system logs mission_executor --follow
iii --runtime-target sim system service restart micro_ros_agent
iii --runtime-target sim system shutdown
```

Inside the devcontainer, source `setup/setup_dev.bash` and omit
`--runtime-target`; `iii` then operates on the local SIM runtime. The
separately systemd-owned Runtime API has explicit native controls:

```bash
iii --runtime-target sim api start
iii --runtime-target sim api status
iii --runtime-target sim api logs --follow
iii --runtime-target sim api restart
iii --runtime-target sim api stop
```

Manual recorder controls also use `iii`:

```bash
iii --runtime-target sim rosbag status
iii --runtime-target sim rosbag list
iii --runtime-target sim rosbag start --id inspection
iii --runtime-target sim rosbag stop --id inspection
```

The installed web UI remains host-side:

```bash
~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh start
~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh status
~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh stop
```

`iii-dev system`, `api`, `gui`, and `rosbag` now print migration guidance and
exit without acting on the runtime. `iii-dev stack` composes the native CLI
and installed GUI launcher; it does not duplicate their command surfaces.

## Stack Variants

```bash
./iii-dev stack start --headless
./iii-dev stack start --recreate-sim
./iii-dev stack start --no-gui
./iii-dev stack attach system
./iii-dev stack attach sim
```

Mutating stack commands take a non-blocking workspace lock. Readiness waits are
bounded; override the simulation timeout when rebuilding PX4 takes longer:

```bash
III_DEV_SIM_READY_TIMEOUT_SEC=600 ./iii-dev stack start --recreate-sim
```

The wrapper performs no broad process sweeps. Shutdown and cleanup remain
scoped to the canonical GUI Compose project, III runtime, and simulation tmux
session/PX4 instance.
