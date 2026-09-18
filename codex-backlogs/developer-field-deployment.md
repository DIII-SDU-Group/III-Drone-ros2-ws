# Developer Field Deployment Backlog

## In-Progress

### P2.T0: One-time Pi validation

The source tree, one-time self-provisioning image, and controller are ready.
When the SD card is inserted, write that image once, boot the Pi, then perform
one attended direct-host convergence and one small non-flight synchronization.
Do not write PX4 firmware, arm the vehicle, run motors, or power off the Pi.

Acceptance:

- [x] Relevant developer-deployment, CLI, and Runtime tests pass.
- [x] The direct SSH/rsync and Ansible commands have been previewed.
- [x] The developer host playbook passes syntax validation.
- [x] Official Ubuntu Pi image was downloaded and SHA-256 verified.
- [x] Image writing seeds normal `iii` SSH/sudo and the workstation link.
- [x] The 116.2 GB Pi SD card was flashed and its first-boot seed verified.
- [x] The on-target build path excludes test binaries; III tests run on the
  workstation rather than delaying field-runtime synchronization.
- [x] The on-target HIL build skips desktop-only `iii_drone_simulation`; HIL
  sensor and transform peers stay workstation-owned as specified by the runtime
  profile.
- [x] The HIL link inspector requires the profile's actual DDS/MAVLink ports
  (`8889` and `14542`) rather than the real-flight defaults.
- [x] Pi provision and direct synchronization have been observed on the
  physical device: 29 runtime packages built and both runtime services are
  active.
- [x] The Pi--PX4 physical Ethernet layer has been observed: `eth0` is
  `10.41.10.1/24`, resolves the PX4 peer `10.41.10.2`, and ICMP reaches it
  without loss. The HIL graph and MicroXRCEAgent UDP `8889` are running.
- [ ] The physical HIL link has been observed ready: Pi `eth0` has
  `10.41.10.1/24`, reaches `10.41.10.2`, and exposes DDS `8889` plus MAVLink
  `14542`.

Hardware-only next step:

1. Connect a data-capable cable from PX4 USB to a Pi USB port while keeping the
   PX4 Ethernet lead on the Pi native Ethernet port (`eth0`) and the
   workstation link attached. The current network-only path has no PX4 console
   or MAVLink endpoint through which its persisted transport settings can be
   inspected.
2. Inspect the existing disarmed PX4 parameters, then apply the checked-in
   [`hil-ethernet.nsh`](../deployment/px4/hil-ethernet.nsh) baseline and reboot
   the PX4 only. This selects the Pi MicroXRCEAgent at UDP `8889` and MAVLink
   broadcast at UDP `14542`; it neither arms the vehicle nor changes firmware.
3. Capture one `/fmu/out/vehicle_local_position_setpoint` DDS message and
   observed PX4 Ethernet UDP traffic after reboot. A Pi listener, ARP entry, or
   ping alone does not close this gate.

The flash seeds the normal developer account and network path. Repeated
development iterations use online deployment only; never power off the Pi.

## Incomplete

### P3.T0: OptiTrack pose ingress and PX4 external-vision bridge

The current `opti_track` profile selects the real-aircraft graph and real
parameter family, but it contains no OptiTrack/NatNet receiver, rigid-body
mapping, coordinate-frame transform, or publisher to PX4
`vehicle_visual_odometry`. It is therefore not a functional OptiTrack
integration yet and must not be represented as field-ready.

Required external inputs before implementation:

1. OptiTrack server address and whether this is unicast or multicast delivery.
2. The aircraft rigid-body name/ID and the authoritative OptiTrack world-frame
   orientation/origin.
3. The intended host for the bridge (OptiTrack workstation or Pi) and the ROS
   domain/network route to the aircraft.

Acceptance:

- [ ] A maintained bridge receives the selected rigid body and publishes a
  stamped ROS pose/odometry contract at the agreed frame and rate.
- [ ] The bridge converts that contract to PX4 external vision on the existing
  uXRCE-DDS path with a tested, documented ENU/FLU to PX4 frame conversion.
- [ ] The `opti_track` supervised profile owns or depends on the bridge and
  refuses active field operation when pose freshness is lost.
- [ ] Disarmed hardware validation captures both the OptiTrack pose and the
  PX4 accepted external-vision/vehicle-odometry response.

## Completed

### P0.T0: Direct developer deployment command

- Added `iii deploy dev`: ordinary SSH/rsync with optional remote build and
  runtime restart.
- Added direct command previews and readable local receipts; no signing,
  receiver, nonce, clean-Git, enrollment, or confirmation flow is involved.

### P0.T1: Writable developer-host provisioning

- Replaced receiver/bootstrap/finalization, firewall, immutable release tree,
  trust inputs, and hardened service units with a normal `iii` account,
  passwordless sudo, writable `/home/iii/ws`, ordinary SSH, and editable
  workspace services.
- The Pi--PX4 link remains `10.41.10.1/24` to `10.41.10.2`; HIL uses DDS UDP
  8889 and MAVLink UDP 14542.

### P1.T0: Production deployment surface removal

- Removed signed release, receiver, qualification, evidence, enrollment,
  immutable activation, firewall, restricted SSH, PX4 release mutation, and
  governance tooling from the tracked deployment surface and public CLI.
- Removed the receiver clock workflow, including its system command and the
  runtime activation-health publisher.
- Runtime API browser, CLI, and WebSocket access are direct developer access.
  Vehicle-state checks remain only for physical flight mutations.
- Runtime event logs now write immediately to a normal writable session path;
  they do not wait for receiver clock state or a flush token.

### P1.T1: Documentation and workflow simplification

- Replaced deployment, provisioning, HIL, ground-control, ADR, and workspace
  guidance with the direct developer workflow.
- Superseded the original production redesign backlog and its Q114--Q131 style
  assumptions rather than leaving them normative.

## Verification

- Developer host and CLI-focused tests: 43 passed, including the on-target
  runtime-only build contract (`BUILD_TESTING=OFF`).
- Full III CLI test suite: 84 passed in the sourced ROS workspace environment.
- Full III Runtime suite: 300 passed.
- Developer-host Ansible playbook syntax check: passed.
- Developer systemd unit verification: passed (only unrelated host warnings).
