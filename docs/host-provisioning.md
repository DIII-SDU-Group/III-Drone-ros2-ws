# Developer Field Host Provisioning

The Raspberry Pi is provisioned as a normal editable development host.

1. Flash the prepared Ubuntu Raspberry Pi image once with
   `iii host image write --image <ubuntu-raspi-image> --device <sd-card>`.
   Its default first-boot seed creates the normal `iii` account, installs this
   computer's SSH key, grants passwordless sudo, and assigns `10.42.0.15` as a
   workstation-link fallback. The USB Ethernet adapter also accepts DHCP, so a
   router or DHCP-serving workstation can assign its normal LAN address.
2. Connect with `iii@iii.local` when name resolution is available. The static
   `iii@10.42.0.15` link remains a recovery option when it is not; a DHCP lease
   also works on a routed LAN. A directly connected workstation must either
   provide DHCP or assign itself `10.42.0.1/24`.
3. From this workspace, run:

   ```bash
   python3 scripts/install_gc.py --profile dev
   iii host provision --host <pi-host-or-ip> --profile hil
   ```

   Install the native host CLI, GUI, and pinned QGroundControl once per
   checkout as described in the [ground-computer install guide](ground-computer-installation.md).
   Use `--profile deploy` for a field ground computer. The installer does not
   deploy to or start the Pi.

4. Deploy the workspace directly:

   ```bash
   iii deploy dev --host <pi-host-or-ip> --build --restart
   ```

   The build runs on the workstation in the pinned ARM64 cross-builder. It
   also builds the Pi's Micro XRCE-DDS agent against the pinned ARM64
   Fast-DDS sysroot. It synchronizes the resulting install tree to the Pi;
   no compiler or colcon build is run on the Pi.

5. For the HIL profile, inspect the dedicated Pi--PX4 link before starting a
   test:

   ```bash
   iii px4 inspect --host <pi-host-or-ip> --profile hil
   ```

   A ready HIL host reports the Pi address `10.41.10.1/24`, a route to the PX4
   peer at `10.41.10.2` through `eth0`, and UDP listeners for DDS `8889` and
   MAVLink `14542`. The command is read-only; it never arms the vehicle or
   changes PX4.

The provisioner creates `/home/iii/ws`, grants `iii` passwordless sudo, enables
normal interactive SSH capabilities, configures the Pi--PX4 Ethernet link, and
installs unsandboxed systemd units that run the workspace after it has been
built.  It does not need a receiver bundle, signing key, trust store,
enrollment file, runtime token, immutable release, or finalization pass.

For every profile it also writes the stack's ROS 2 domain (`--ros-domain-id`,
default 42) and Fast DDS settings into `/etc/iii/runtime.env`; the flight
controller's `UXRCE_DDS_DOM_ID` must equal that domain.

## Switching profiles

`iii host provision --host <pi> --profile hil|opti_track|real` is how the Pi
changes profile. It ends by restarting the system daemon and the Runtime API,
so the new profile is live when it returns; no deployment is needed unless the
code changed. Afterwards bring the flight controller to the same profile with
`iii px4 param-baseline --profile <profile>`
(see [PX4 parameter baselines](px4-parameter-baselines.md)).

`real` and `opti_track` require a Wi-Fi client, and each keeps its own on the
Pi. Give the network once:

```bash
iii host provision --host <pi> --profile opti_track \
  --wifi-ssid OptiTrack_5G --wifi-psk-file ~/.config/iii/optitrack-wifi.psk --wifi-country DK
```

Later runs of `iii host provision --profile opti_track` need no Wi-Fi options:
they activate that profile's stored client again. `--wifi-ssid` replaces the
stored client of the profile being provisioned. Provisioning one of these
profiles without a stored client and without `--wifi-ssid` is refused. For
`hil` the Wi-Fi client is optional: `--wifi-ssid` sets it, `--remove-wifi`
removes it, and without either the active client stays as it is. See
[Pi, PX4, and HIL links](deployment-hardware-roles.md).

## Vehicle gate

`iii host provision` and `iii deploy dev` restart the system daemon and the
Runtime API, which stops a running aircraft system. Both are therefore allowed
only while the aircraft is provably disarmed and landed:

- A Pi without a provisioned profile, and a Pi on `hil` (which flies the
  simulated PX4), pass.
- A Pi on `real` or `opti_track` must report a fresh disarmed and landed
  vehicle state through its Runtime API.
- `--force` proceeds when that state cannot be read, for example with the
  flight controller unpowered on the bench. It never overrides an aircraft
  reported armed or in flight.

The cross-deployed Pi runtime intentionally skips the desktop-only
`iii_drone_simulation` package and build-only sample packages. HIL sensor and
transform peers are workstation-owned, so installing Gazebo on the aircraft
would add a large, unused dependency without improving HIL coverage.

`iii deploy dev --dry-run` shows the exact SSH and rsync commands.  Use
`--mirror` only when deliberately removing remote files absent locally. Every
deployment ends by restarting the system daemon and the Runtime API (`--restart`
is accepted and no longer needed).

The deployment path never arms the vehicle or writes PX4 firmware or parameters.
It never powers off the Pi. Rebooting is allowed when required; normal edits,
builds, and runtime restarts happen online.

Before the first physical-PX4 HIL validation, apply the explicit
[PX4 HIL Ethernet Baseline](px4-hil-ethernet-baseline.md) with
`iii px4 param-baseline --profile hil`. This is a separate PX4 operation and
is not performed by provisioning or developer deployment.
