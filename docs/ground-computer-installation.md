# Ground-computer installation

The same checkout installs the operator interface, native `iii` command, and
QGroundControl on a Linux x86_64 workstation or field ground computer. Use the
`dev` install profile on the workstation that runs SIM and HIL. Use `deploy` on
the ground computer that controls an OptiTrack or real-aircraft runtime. These
are *computer install* profiles; `sim`, `hil`, `opti_track`, and `real` select
the *runtime* for an individual command.

Start from a checkout with its III submodules present. The installer is a
standalone script, not an `iii` subcommand:

```bash
git clone --recurse-submodules git@github.com:DIII-SDU-Group/III-Drone-ros2-ws.git iii-drone
cd iii-drone
./scripts/git/verify_submodule_lock.sh
python3 scripts/install_gc.py --profile dev --dry-run
python3 scripts/install_gc.py --profile dev
```

On the field ground computer, substitute `--profile deploy`. The script checks
Linux architecture, Python/venv, Docker and Compose, Git, curl, SSH, and the
user systemd manager before installation. It verifies the QGroundControl
download against the checkout pin. To reuse an already downloaded AppImage,
pass `--qgc-asset /path/to/QGroundControl.AppImage`; the same size and SHA256
checks still apply. The installer builds the GUI images but does not start the
UI, QGroundControl, a runtime, or a vehicle. It records the checkout revision,
dirty source hashes, installed paths, and profile in `install.json`. If source
inputs change while it runs, it refuses promotion. Rerunning the same profile
replaces the managed install. A recognized earlier `iii` entry point and
QGroundControl user unit are backed up under
`~/.local/share/iii/gc-migration-backups/`; unrelated files are never replaced.
An occupied install root without this installer's `install.json` ownership
record is rejected and left intact.

The default paths are:

| Component | Installed path |
| --- | --- |
| Native command | `~/.local/bin/iii` |
| Install manifest | `~/.local/share/iii/gc/install.json` |
| GUI launcher | `~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh` |
| QGroundControl | `~/.local/share/iii/gc/qgc/QGroundControl.AppImage` |
| QGroundControl user unit | `~/.config/systemd/user/iii-qgc.service` |
| GUI lifecycle logs | `~/.local/state/iii/ground-control/` |

`III_GC_INSTALL_ROOT` changes the root under `~/.local/share/iii/gc` in this
table. The installed `iii` wrapper exports that root and clears a leaked
`PYTHONPATH`, so a shell sourced for ROS does not accidentally import checkout
modules into the native command. Ensure `~/.local/bin` is on `PATH`; a new
login shell normally picks it up. Verify with `iii --help` and `iii qgc status`.
If `iii deploy --help` still lists the older `stage`/`activate` deployment
commands, run `type -a iii` in that same shell. An earlier checkout on `PATH`
can shadow this install; use `~/.local/bin/iii` directly for the first
deployment, or source this checkout's `setup/setup_hil.bash` and run `hash -r`
before retrying.
QGroundControl is pinned by `deps/qgroundcontrol.json` to v5.0.8, upstream
commit `e0816c957602789200ae5ba0af45217f0f2f1db4`, and SHA256
`06969c67ef58ea063def0a8271447a1cc385438c4a7df36813315b4475146737`.
The same artifact is selected for SIM, HIL, OptiTrack, and real operations from
this checkout.

The installed GUI launcher uses its own content snapshot, so it does not need
the source checkout to remain at the same path. Put local GUI settings in
`~/.config/iii-ground-control.env`, then run the installed launcher with
`start`, `status`, `logs`, or `stop`. An aircraft GUI profile (`real` or
`opti_track`) needs the expected runtime and system identity configured before
start; the launcher and the GUI proxy refuse an unpinned one. QGroundControl
is a separate native application managed with `iii qgc start|status|stop` and
its user service; starting the web UI never starts QGroundControl itself. For
real and OptiTrack flights it connects through the telemetry radio plugged into
the ground computer.

On a `dev` workstation, `./iii-dev sim start` launches the devcontainer SIM
and rendered workstation applications; `./iii-dev hil start` coordinates the
Pi HIL runtime and workstation SITL. Add `--headless` to either command to
skip desktop windows. The runtime itself stays in the devcontainer for SIM
and on the Pi for HIL. For a native runtime command, select the target:

```bash
iii --runtime-target sim system status
iii --runtime-target hil system status
iii --runtime-target hil --host alternate.local system status
```

The native `iii` selects only the devcontainer labeled for this checkout or
the explicitly configured SSH host. It checks that runtime host's CLI source
identity against the installed checkout before execution; a missing or
mismatched target is rejected. A `deploy` install permits `real` and
`opti_track` targets, for example:

```bash
III_SSH_HOST=iii.local iii --runtime-target real system status
III_SSH_HOST=iii.local iii --runtime-target opti_track system status
```

If you source `setup/setup_field.bash`, its `real` target is a shell default;
an explicit `--runtime-target opti_track` selects OptiTrack for that command.

In the OptiTrack lab the ground computer joins the lab Wi-Fi `OptiTrack_5G`,
where the provisioned Pi is also a Wi-Fi client; select the Pi as `iii.local`
or by its `192.168.10.x` address with `III_SSH_HOST` or `--host`. For the GUI,
set `III_GC_EXPECTED_PROFILE=opti_track` with the aircraft's runtime and system
identity in `~/.config/iii-ground-control.env`. QGroundControl reaches the
flight controller over the telemetry radio connected to the ground computer,
not through the Pi. The [OptiTrack lab session checklist](opti-track-lab-session-checklist.md)
has the flight procedure.

When logged into the devcontainer or Pi, source its local `setup/` runtime
profile and run `iii` directly; there is no SSH hop from the runtime host to
itself. `iii-dev` remains the workstation helper for SIM/HIL orchestration,
container access, and simulation tmux sessions. The Pi CLI is installed by
`iii deploy dev` and uses the checked-out Pi workspace. Deploy it from the
same checkout before expecting native GC routing to pass its identity check.

## Aircraft clock synchronization

The Pi has no trusted real-time clock. On `real` and `opti_track` the runtime
API refuses arming and mission activation until the Pi's chrony reports
`Leap status: Normal` with an offset of at most 0.1 s. The Pi normally reaches
NTP itself: in the OptiTrack lab over the lab network's internet (the lab
gateway provides NAT). When an aircraft appears with an unsettled clock, the
installed GUI runs, once per appearance:

```bash
iii host clock sync --profile <real|opti_track> --host iii.local \
  --confirm --non-interactive --json
```

Run the same command by hand when needed; `--dry-run` shows the plan without
contacting the aircraft. It runs on the ground computer and:

1. refuses (exit 20) unless the aircraft's runtime API reports the selected
   profile and live PX4 state showing the aircraft disarmed and landed;
   unknown, stale, or disputed state also refuses;
2. leaves a settled clock alone, and steps a Pi that already follows a time
   source but is too far off (`chronyc makestep`);
3. otherwise adds this ground computer, at the address the Pi sees for the SSH
   connection, as a runtime-only chrony source on the Pi
   (`sudo chronyc add server <address> iburst`, through the passwordless sudo
   that provisioning grants), then steps once it is selected;
4. exits 0 only when the clock is settled; otherwise it exits non-zero within
   40 s (the GUI stops it after 45 s) and prints one JSON result.

It never changes this ground computer's configuration, and nothing it adds to
the Pi survives a chrony restart. The ground-computer fallback needs chrony on
this computer serving the Pi, which Ubuntu's default `systemd-timesyncd` does
not do. Install `chrony`, then add to `/etc/chrony/chrony.conf`:

```text
# Serve the aircraft: lab Wi-Fi and the direct USB-Ethernet link.
allow 192.168.10.0/24
allow 10.42.0.0/24
# Keep serving without internet (field use).
local stratum 10
```

and run `sudo systemctl restart chrony`. Allow UDP 123 through any local
firewall. Use the Pi's IPv4 address with `--host` if SSH to `iii.local` uses
IPv6.

The installer and routing tests use staged files and fake Docker/SSH commands.
A successful dry run or unit test does not establish Pi deployment, rendered
window health, or physical flight readiness; verify each environment before
operating it.
