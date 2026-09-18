# Developer Field Deployment Backlog

## Completed

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
- [x] The on-target HIL build skips desktop-only `iii_drone_simulation`; the
  Pi runs only the core TF publisher needed to combine PX4 odometry with the
  deployed static sensor extrinsics.
- [x] The HIL link inspector requires the profile's actual DDS/MAVLink ports
  (`8889` and `14542`) rather than the real-flight defaults.
- [x] The separately governed PX4 source candidate `9eabb01b` builds as
  `px4_fmu-v6x_multicopter`, and its generated uXRCE-DDS table contains
  `/fmu/out/vehicle_local_position_setpoint` with the matching installed
  `px4_msgs` interface on the Pi.
- [x] Pi provision and direct synchronization have been observed on the
  physical device: 29 runtime packages built and both runtime services are
  active.
- [x] The Pi--PX4 physical Ethernet layer has been observed: `eth0` is
  `10.41.10.1/24`, resolves the PX4 peer `10.41.10.2`, and ICMP reaches it
  without loss. The HIL graph and MicroXRCEAgent UDP `8889` are running.
- [x] The physical HIL link is ready: Pi `eth0` is `10.41.10.1/24`, reaches
  `10.41.10.2`, and owns DDS UDP `8889` plus MAVLink UDP `14542`.
- [x] The PX4 transport was configured and inspected through its workstation
  USB MAVLink connection; PX4-to-Pi transport remains Ethernet-only.
- [x] `iii px4 inspect --host 192.168.1.251 --profile hil --json` completed
  successfully, observed PX4 UDP traffic from `10.41.10.2`, and received a
  `/fmu/out/vehicle_local_position_setpoint` sample through DDS.
- [x] `iii system start` completed with the MicroXRCE agent ready and every
  HIL managed node active, including the Pi-local core TF bridge.
- [x] A final MAVLink heartbeat check confirmed `armed=False`; no propulsion
  battery was connected and no motor command was issued.

The flash seeds the normal developer account and network path. Repeated
development iterations use online deployment only; never power off the Pi.

## In-Progress

### P3.T0: OptiTrack pose ingress and PX4 external-vision bridge (lab-blocked)

The `opti_track` name remains reserved for the intended real-aircraft graph and
real parameter family, but it contains no OptiTrack/NatNet receiver, rigid-body
mapping, coordinate-frame transform, or publisher to PX4
`vehicle_visual_odometry`. It is therefore deliberately non-bootable and absent
from onboard mission catalogs until the bridge is implemented and validated; it
must not be represented as field-ready.

Required external inputs before implementation:

1. OptiTrack server address and whether this is unicast or multicast delivery.
2. The aircraft rigid-body name/ID and the authoritative OptiTrack world-frame
   orientation/origin.
3. The intended host for the bridge (OptiTrack workstation or Pi) and the ROS
   domain/network route to the aircraft.

Current blocker: OptiTrack is intentionally deferred until the lab session.
No server stream, rigid-body identity, frame convention, or target network
route is available yet, so this item cannot be implemented or hardware-closed
without inventing the required integration contract.

Acceptance:

- [x] The absent bridge cannot be mistaken for a commissioned field profile:
  `opti_track` is non-bootable and excluded from onboard catalog defaults.
- [ ] A maintained bridge receives the selected rigid body and publishes a
  stamped ROS pose/odometry contract at the agreed frame and rate.
- [ ] The bridge converts that contract to PX4 external vision on the existing
  uXRCE-DDS path with a tested, documented ENU/FLU to PX4 frame conversion.
- [ ] The `opti_track` supervised profile owns or depends on the bridge and
  refuses active field operation when pose freshness is lost.
- [ ] Disarmed hardware validation captures both the OptiTrack pose and the
  PX4 accepted external-vision/vehicle-odometry response.

## Previously Completed

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
- Candidate profile validation in the sourced ROS development workspace:
  `iii_drone_configuration` passed 113 tests and `iii_drone_mission` passed
  65 tests, both with zero failures. The built local mission catalog declares
  `opti_track` non-commissioned/non-onboard with no default; its qualified
  catalog contains only `hil` and `real` profiles.
