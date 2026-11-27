# Developer Field Deployment Backlog

## Completed

### P2.T1: Canonical split-host HIL inspection-mission acceptance

Bring up the maintained split-host HIL surface: PX4 SITL and Gazebo on the
workstation, the supervised ROS graph on the Pi, and the physical PX4 retained
disarmed with no propulsion battery. Run the installed `inspection_demo`
mission long enough to exercise the recurring mission loop and retain mission,
setpoint, PX4-SITL, Gazebo, and Pi-runtime evidence. This is distinct from the
already-completed uXRCE transport and managed-node bringup gate.

Acceptance:

- [x] The coordinated launcher proves healthy Gazebo/PX4-SITL adapters and a
  fresh, fully active Pi HIL graph without powering off the Pi or arming the
  physical PX4.
- [x] The installed canonical inspection mission obtains/validates its scene
  overviews, becomes the active mission owner, and remains active through a
  sustained observation window.
- [x] Retained artifacts prove Gazebo motion, PX4-SITL state/setpoint flow,
  Pi mission lifecycle, and that the physical PX4 remained disarmed.

Evidence (2026-09-29, commit `88c1f5f`, `runtime/qualification/20260929T084358Z-88c1f5f233/`):
`run_qualification_campaign.py` ran strict SIM, deployed, then ran strict HIL on
the same tracked sim parameter set (provenance-checked on the Pi). HIL: 1800 s,
5 automatic Inspection -> Reach Cable -> Charge -> Leave Cable cycles, 6 full-charge
proofs, 0 ERROR/WARN/FATAL, final landed/disarmed. The physical PX4 heartbeat
record (1328 samples over 2065 s, max gap 2.0 s, from `10.41.10.2`) shows it
never armed. SIM in the same campaign: 1800 s, 4 cycles, 0 ERROR/WARN/FATAL.

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

### P3.T0: OptiTrack flight-basics profile (prepared; lab acceptance pending)

The lab's setup document (kept locally at `docs/references/OptiTrack_setup.pdf`,
excluded from Git because it contains credentials) settled the inputs that
blocked this item: Motive 3.1.4 at `192.168.10.3` streams NatNet unicast at
about 120 Hz to the lab gateway `192.168.10.1`, which publishes each rigid body
as `/body_splitter/body_<id>/pose` (`PoseStamped`, best effort) in ROS domain 0;
the rigid-body name is its numeric Motive ID; the drone Pi joins the lab Wi-Fi
`OptiTrack_5G`. The facts, the data flow, and commissioning are in
[`docs/opti-track-lab-readiness.md`](../docs/opti-track-lab-readiness.md); the
flight procedure is
[`docs/opti-track-lab-session-checklist.md`](../docs/opti-track-lab-session-checklist.md).

User decisions: `opti_track` is a reduced flight-basics profile (no cable,
payload, perception, or overviews; flies with and without the payload). A pose
relay on the drone Pi bridges the lab domain 0 into the stack domain (default
42) as PX4 `/fmu/in/vehicle_visual_odometry` and sets PX4's EKF global origin
once per flight-controller boot. Motion capture is the height reference with the
barometer as backup; `UXRCE_DDS_SYNCT=0` with `EKF2_EV_DELAY`. QGroundControl
uses the telemetry radio; a safety pilot flies with RC; 6S battery; the flight
controller runs the latest PX4 built from the pinned fork. Missions: M1 "OT
Hover" (default), M2 "OT Maneuvers", M3 OT Cycle (Takeoff, Shuttle, Land), M4
OT Mode Loop.

Prepared on branch `claude/optitrack-profile-prep-7c0d12`; the package work
lands through integration:

- Platform: the stack domain is provisioned for every aircraft profile
  (`iii host provision --ros-domain-id`), an optional Wi-Fi client
  (`--wifi-ssid`), onboard `setup_real.bash`/`setup_opti_track.bash` that adopt
  `/etc/iii/runtime.env`, `iii px4 inspect` with an `opti_track` DDS proof, and
  the PX4 baseline `deployment/px4/opti-track.nsh`.
- Packages: the relay `opti_track_pose_relay` (III-Drone-Core), the reduced
  graph with the relay service and its freshness readiness
  (III-Drone-Supervision), the relay parameters and a bootable `opti_track`
  (III-Drone-Configuration), the OptiTrack missions (III-Drone-Mission), and
  profile capabilities with the external-vision state (III-Drone-Contracts,
  -Runtime, -GC).

Still to capture at the lab: the rigid-body ID, Motive's up axis and axes, the
pose header-stamp clock, Wi-Fi latency, the cage dimensions for the geofence,
`MPC_THR_HOVER` per payload configuration, the tuned `EKF2_EV_DELAY`, and a
review of the `real` parameter set that `opti_track` uses.

Acceptance:

- [x] The lab contract (network, stream, topic, QoS, domain, naming, Wi-Fi,
  watchdog, lab rules) is recorded without credentials.
- [x] Platform: provisioning, Wi-Fi client, onboard setup profiles, PX4 link
  inspection, and the PX4 baseline are implemented and unit-tested.
- [ ] The relay receives the configured rigid body, converts it to PX4 external
  vision with a tested frame conversion, and stops publishing on stale input.
- [ ] The reduced supervised profile owns the relay and gates on its freshness.
- [ ] The OptiTrack missions are in the onboard catalog for `opti_track`.
- [ ] Lab commissioning: the Pi is on `OptiTrack_5G`, the PX4 baseline is
  applied, and `iii px4 inspect --profile opti_track` passes at the lab.
- [ ] Disarmed validation: PX4's local position and heading follow Motive with
  the external-vision estimator flags set; streams retained.
- [ ] Flight ladder: pilot-flown hover, `MPC_THR_HOVER`, GUI custom operations,
  then M1, M2, M3, M4.

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
