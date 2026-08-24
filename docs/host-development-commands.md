# Host Development Commands

`iii-dev` is the workspace-root command for operating the development stack
without opening a terminal inside the devcontainer. It is a transport and
coordination layer: PX4/Gazebo remains owned by the simulation launcher, the
ROS graph remains owned by the III daemon, and ground control remains owned by
its host-side Docker Compose project.

## Prerequisites

- Run commands from this checkout on the development host.
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

Every command group and subcommand supports both `-h` and `--help`. Help for
wrapper-owned commands is rendered on the host without contacting Docker;
nested `system` help is forwarded to the canonical in-container III CLI.

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
./iii-dev system attach
./iii-dev gui logs --follow
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
./iii-dev shell
./iii-dev exec ros2 node list
```

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

Every in-container system command can be forwarded without duplicating its
argument model:

```bash
./iii-dev system boot
./iii-dev system start
./iii-dev system status
./iii-dev system logs mission_executor --follow
./iii-dev system service restart micro_ros_agent
./iii-dev system shutdown
```

The separately systemd-owned runtime API has explicit controls:

```bash
./iii-dev api start
./iii-dev api status
./iii-dev api logs --follow
./iii-dev api restart
./iii-dev api stop
```

Ground-control operations remain host-side:

```bash
./iii-dev gui start
./iii-dev gui status
./iii-dev gui logs --follow
./iii-dev gui restart
./iii-dev gui stop
```

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
