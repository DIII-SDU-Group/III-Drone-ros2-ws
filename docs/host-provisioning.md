# Developer Field Host Provisioning

The Raspberry Pi is provisioned as a normal editable development host.

1. Flash the prepared Ubuntu Raspberry Pi image once with
   `iii host image write --image <ubuntu-raspi-image> --device <sd-card>`.
   Its default first-boot seed creates the normal `iii` account, installs this
   computer's SSH key, grants passwordless sudo, and assigns `10.42.0.15` as a
   workstation-link fallback. The USB Ethernet adapter also accepts DHCP, so a
   router or DHCP-serving workstation can assign its normal LAN address.
2. Connect with `iii@10.42.0.15` on a direct static link, or use `iii.local` /
   the DHCP lease when routed. A directly connected workstation must either
   provide DHCP or assign itself `10.42.0.1/24`.
3. From this workspace, run:

   ```bash
   iii host provision --host <pi-host-or-ip> --profile hil
   ```

4. Deploy the workspace directly:

   ```bash
   iii deploy dev --host <pi-host-or-ip> --build --restart
   ```

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

The Pi deployment builds the on-aircraft runtime and intentionally skips the
desktop-only `iii_drone_simulation` package. HIL sensor and transform peers are
workstation-owned, so installing Gazebo on the aircraft would add a large,
unused dependency without improving HIL coverage.

`iii deploy dev --dry-run` shows the exact SSH and rsync commands.  Use
`--mirror` only when deliberately removing remote files absent locally.

The deployment path never arms the vehicle or writes PX4 firmware.
It never powers off the Pi. Rebooting is allowed when required; normal edits,
builds, and runtime restarts happen online.

Before the first physical-PX4 HIL validation, apply the explicit
[PX4 HIL Ethernet Baseline](px4-hil-ethernet-baseline.md). This is a separate
PX4 operation and is not performed by provisioning or developer deployment.
