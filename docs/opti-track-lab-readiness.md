# OptiTrack Lab Readiness

This is the commissioning worksheet and acceptance boundary for the
`opti_track` runtime profile in the SDU OptiTrack lab. It records the lab facts,
describes how the aircraft consumes motion capture, and lists the one-time
commissioning steps. The per-session flight procedure is the
[OptiTrack lab session checklist](opti-track-lab-session-checklist.md). Nothing
here arms the aircraft or authorizes a flight on its own.

## Authority and safety boundary

- Target profile: `opti_track`, a reduced "flight basics" aircraft profile. The
  lab has no cable, so the canonical cable/inspection missions cannot run
  there; the profile has its own OptiTrack missions. It must work with and
  without the payload mounted.
- Every commissioning step below runs with the aircraft disarmed and no
  propulsion battery connected. Flights need a safety pilot on RC and follow the
  session checklist.
- III deployment and provisioning never arm the vehicle and never write PX4
  firmware or parameters. The PX4 baseline below is an explicit manual NSH
  operation.
- Lab rules: change nothing in Motive or on the lab gateway except your own
  rigid body. Motive must stay on, and be returned to, its default
  configuration. Only the lab maintainer calibrates the system.

## Lab reference document

The lab's setup document is kept locally at
`docs/references/OptiTrack_setup.pdf`. It is excluded from Git (listed in
`.git/info/exclude`) because it contains credentials. Never commit it and never
copy a password, Wi-Fi passphrase, or login from it into the repository; take
them from the document or the lab maintainer when needed.

## Lab facts settled by the lab document

| Fact | Value |
| --- | --- |
| Motion-capture software | Motive 3.1.4 on the lab Windows PC, fixed address `192.168.10.3` |
| Stream | NatNet unicast from `192.168.10.3` to the lab gateway `192.168.10.1` (command UDP 1510, data UDP 1511), about 120 Hz |
| Lab gateway | Raspberry Pi at `192.168.10.1`: router, DHCP (pool `.10`–`.254`, 60 min leases), NAT, and internet for `192.168.10.0/24` |
| ROS topics on the gateway | `/rigid_bodies` (`mocap4r2_msgs/msg/RigidBodies`, about 120 Hz) and `/body_splitter/body_<id>/pose` (`geometry_msgs/msg/PoseStamped`, one topic per rigid body, header copied from `/rigid_bodies`) |
| Pose topic QoS | best effort, volatile, keep last 1 |
| ROS domain | 0 (no explicit `ROS_DOMAIN_ID`); no ROS security, trusted private network |
| Rigid-body naming | Motive rigid-body name = its numeric Motive ID (lab convention), so ID `<id>` publishes on `/body_splitter/body_<id>/pose` |
| Wi-Fi | Asus access point (AP mode, no DHCP), SSIDs `OptiTrack` (2.4 GHz) and `OptiTrack_5G` (5 GHz). Use `OptiTrack_5G`: the 2.4 GHz network collapsed under 120 Hz multicast |
| Stream watchdog | The gateway checks `/rigid_bodies` once a minute and restarts its mocap service after 8 s without data; recovery takes about 25 s, so outages of up to about 1.5 min are possible |

## Still to capture at the lab

Record observed values, not defaults. Keep the evidence (screenshot, command
output, or log name).

| Required fact | Observed value | Evidence retained |
| --- | --- | --- |
| Aircraft rigid-body ID (sets `/opti_track/pose_relay/rigid_body_id`) |  |  |
| Motive up axis and world axes/origin as published on `/body_splitter/body_<id>/pose` |  |  |
| Rigid-body axes relative to the aircraft body (created with the nose along Motive +x) |  |  |
| Clock of the pose header stamps (Motive frame time or gateway receive time) |  |  |
| Pose rate on the drone Pi over `OptiTrack_5G`, and Wi-Fi round-trip time to `192.168.10.1` |  |  |
| Cage dimensions for the geofence (distance from the takeoff point to the nearest net, ceiling height) |  |  |
| `MPC_THR_HOVER` per payload configuration (with and without the payload) |  |  |
| Tuned `EKF2_EV_DELAY` from the first flight logs |  |  |

Stop if a value is unavailable, ambiguous, or inconsistent with the Motive
display. Do not select a rigid body by a guessed ID and do not infer the axis
convention from a diagram alone: verify it with the disarmed acceptance in the
session checklist.

## How the aircraft uses motion capture

```text
Motive (192.168.10.3) --NatNet unicast--> lab gateway (192.168.10.1, ROS domain 0)
   /body_splitter/body_<id>/pose
        |  lab Wi-Fi OptiTrack_5G (drone Pi wlan0, DHCP, owns the default route)
        v
drone Pi: opti_track_pose_relay   (lab side: domain 0, subscription only)
        |  /fmu/in/vehicle_visual_odometry (stack domain 42, PX4 NED/FRD)
        v
micro_ros_agent UDP 8888 --Ethernet 10.41.10.1 <-> 10.41.10.2--> PX4 EKF2
QGroundControl <--telemetry radio--> PX4
```

- **Network.** The drone Pi joins `OptiTrack_5G` as a provisioned Wi-Fi client.
  The PX4 Ethernet link (`10.41.10.1/24`) and the workstation USB-Ethernet link
  (`10.42.0.15/24`) are unchanged; Wi-Fi owns the default route while it is
  associated. The ground computer joins the same Wi-Fi to reach the Pi.
  QGroundControl uses the telemetry radio, not the Pi.
- **Two ROS domains.** Only the relay's motion-capture subscription joins the
  lab domain 0. Everything else, including PX4 through the agent, runs in the
  stack domain provisioned as `iii_ros_domain_id` (default 42). PX4's
  `UXRCE_DDS_DOM_ID` must equal it. The stack domain must not be 0 for
  `opti_track`; `iii host provision` refuses that.
- **Pose relay** (`opti_track_pose_relay` in III-Drone-Core, a daemon-owned
  service of the `opti_track` profile). It forwards the latest pose of the
  configured rigid body to `/fmu/in/vehicle_visual_odometry` at 50 Hz while the
  pose is fresher than 0.15 s, with the timestamp left for PX4 to stamp on
  arrival (`UXRCE_DDS_SYNCT 0`; `EKF2_EV_DELAY` models the latency). When poses
  stop, it stops publishing. Its readiness heartbeat
  `/opti_track/pose_relay/fresh` (2 Hz) is published only while fresh poses
  flow; supervision gates on it, so `iii system start` waits up to 120 s for
  motion capture. With `rigid_body_id` unset (-1) the relay stays alive but is
  never ready and reports `rigid_body_id not configured`.
- **Relay parameters** (`/opti_track/pose_relay/...`, boot-time constants):
  `rigid_body_id` (default -1), `lab_ros_domain_id` (0), `output_rate_hz` (50),
  `stale_timeout_s` (0.15), `send_origin` (true) and the origin latitude,
  longitude, and altitude.
- **EKF origin.** With `send_origin`, the relay sets PX4's EKF global origin
  once per flight-controller boot, so PX4 has the global position that Hold,
  takeoff, landing, and the geofence need without GPS.
- **Estimator.** EKF2 fuses vision horizontal position, vertical position, and
  yaw; vision is the height reference and the barometer is the backup. There is
  no GPS and no magnetometer. Without vision the EKF dead-reckons for at most
  1 s (`EKF2_NOAID_TOUT`) before PX4's position failsafes act.
- **Reduced graph.** Supervision runs `configuration_server`, `tf`,
  `trajectory_generator`, `maneuver_controller`, `rosbag_recorder`,
  `mission_executor`, `custom_operation`, `micro_ros_agent`, and the pose
  relay. There is no payload, perception, overview provider, cable camera, or
  mmWave node; see [the node graph](runtime-launch-and-node-graph.md).
- **Missions** (installed `iii_drone_mission` catalog, profile `opti_track`).
  All geometry is in lab (Motive) coordinates and is a placeholder to confirm
  against the cage at the lab:
  - M1 `opti-track-hover`, "OT Hover" (profile default): takes over from a
    pilot hover at 1.1 m or higher, holds 10 s, climbs 0.3 m, holds, descends
    0.3 m, holds, then hands over to PX4 Hold.
  - M2 `opti-track-maneuvers`, "OT Maneuvers": airborne start; a box
    (half-size 0.5 m at 1.2 m) flown stop-and-go and blended, yaw in place
    ±90° and 180°, a height step to 1.5 m, a waypoint path at 0.3 m/s, then
    Hold.
  - M3 `opti-track-cycle`: "OT Takeoff" (may arm when started on the ground;
    takes off to 1.2 m; waits up to 60 s for the operator's Proceed intent from
    the GUI and lands if none arrives), then "OT Shuttle" (±0.6 m with a 180°
    turn), then "OT Land" (PX4 land, disarm on touchdown).
  - M4 `opti-track-mode-loop`: "OT Loop Takeoff", then an endless mode loop
    "OT Lower Stop-and-Go" (1.2 m × 0.8 m rectangle at 1.2 m, stopping at each
    corner), "OT Upper Stop-and-Go" (the same at 1.6 m), "OT Lower Blended",
    "OT Upper Blended", and back to the lower stop-and-go. The pilot (stick or
    RC mode switch) or the GUI ends it. It exercises many mode transitions.

The relay (III-Drone-Core), the reduced supervision graph
(III-Drone-Supervision), the relay parameters and the bootable `opti_track`
profile (III-Drone-Configuration), the OptiTrack missions (III-Drone-Mission),
and the GUI's external-vision state (III-Drone-Runtime, -Contracts, -GC) come
from those packages. Confirm that the deployed revision contains them before
the lab session: `iii system status` on an `opti_track` boot lists the relay
service.

## One-time commissioning

All steps run with the aircraft disarmed and no propulsion battery. The
workstation reaches the Pi over the USB-Ethernet link (`10.42.0.15`), so a Wi-Fi
change cannot cut the provisioning session.

### 1. Provision the Pi for `opti_track` with the lab Wi-Fi

Prerequisites: an editable Pi as in [host provisioning](host-provisioning.md),
and the lab Wi-Fi passphrase in an owner-only file outside the checkout (for
example `~/.config/iii/optitrack-wifi.psk`, `chmod 600`, passphrase on the first
line). Preview first; the preview never reads or prints the secret:

```bash
iii host provision --host 10.42.0.15 --profile opti_track \
  --wifi-ssid OptiTrack_5G --wifi-psk-file ~/.config/iii/optitrack-wifi.psk \
  --wifi-country DK --dry-run
```

Then run the same command without `--dry-run`. It writes `/etc/iii/runtime.env`
with `III_SYSTEM_PROFILE=opti_track` and `ROS_DOMAIN_ID=42`, installs the Fast
DDS service overrides for that domain, and adds the root-only netplan file
`/etc/netplan/85-iii-wifi.yaml`. Omit the Wi-Fi options to leave an existing
Wi-Fi client unchanged; `--remove-wifi` removes it. Without
`--wifi-psk-file` the command prompts for the passphrase.

Evidence (the Wi-Fi associates only within range of the lab network; it never
delays boot elsewhere): `iii host inspect --host 10.42.0.15` lists `wlan0` with
a `192.168.10.x` address. On the Pi, `ip route show default` names `wlan0` (via
`192.168.10.1`) and `grep ROS_DOMAIN_ID /etc/iii/runtime.env` prints the
provisioned domain.

### 2. Deploy

```bash
iii deploy dev --host 10.42.0.15 --build --restart
```

The deployed revision must contain the package changes listed above.

The Pi takes its time from NTP over the lab network's internet. As a fallback
for an unsettled onboard clock, `iii host clock sync` can make the ground
computer the Pi's time source; that needs chrony on the ground computer with
`allow 192.168.10.0/24` (see
[aircraft clock synchronization](ground-computer-installation.md#aircraft-clock-synchronization)).

### 3. Apply the PX4 OptiTrack baseline

Copy [`deployment/px4/opti-track.nsh`](../deployment/px4/opti-track.nsh) to the
PX4 SD card or open it from a PX4 NSH console, then run:

```nsh
source /fs/microsd/opti-track.nsh
```

It sets the uXRCE-DDS client to Ethernet (agent `10.41.10.1`, UDP 8888, domain
42, no time sync), MAVLink instance 2 to Ethernet UDP 14540 (the telemetry
radio's instance stays untouched), vision-only EKF2 with the barometer as
backup, and the lab failsafes (RC loss: Land; position loss in Position mode:
Altitude; low battery: Land; 6S battery; takeoff altitude 1.2 m). It saves and
reboots the flight controller. The geofence lines stay commented until the
cage is measured, and `MPC_THR_HOVER` is set per payload configuration from a
measured hover, never by the script. If you change `iii_ros_domain_id`, change
`UXRCE_DDS_DOM_ID` to match.

After reboot, on PX4 NSH: `param show UXRCE_DDS_DOM_ID`,
`param show EKF2_EV_CTRL`, `uxrce_dds_client status`, `mavlink status`.

The HIL baseline uses UDP 8889 and 14542 on the same flight controller. Rerun
[`hil-ethernet.nsh`](../deployment/px4/hil-ethernet.nsh) before HIL work: the
HIL physical-PX4 disarm monitor listens on 14542.

### 4. Inspect the link

```bash
iii px4 inspect --host 10.42.0.15 --profile opti_track
```

Success requires the Pi address `10.41.10.1/24`, the PX4 peer, listeners on
UDP 8888 and 14540, PX4-originated UDP traffic, and one
`/fmu/out/vehicle_status_v1` sample in the provisioned ROS domain. A missing
sample with healthy UDP traffic means `UXRCE_DDS_DOM_ID` differs from
`ROS_DOMAIN_ID`, or the `opti_track` runtime (and its agent) is not running.

### 5. Accept at the lab

Follow the [session checklist](opti-track-lab-session-checklist.md): rigid
body, relay readiness, then the disarmed acceptance that PX4's local position
and heading follow Motive. Record the captured facts above.

## Recovery and stop conditions

- Stop if the rigid body's ID, the axis convention, or the relay's readiness is
  uncertain, or if PX4's local position disagrees with Motive. Keep the aircraft
  disarmed and keep the relay output and PX4 logs.
- If the pose stream stops, the relay stops publishing, PX4 loses vision after
  at most 1 s, and its failsafes act; the pilot takes over. Never restart
  services on the lab gateway; its watchdog recovers the stream.
- To undo the Wi-Fi client: `iii host provision --host 10.42.0.15 --profile
  opti_track --remove-wifi`. To restore the HIL transport: rerun
  `hil-ethernet.nsh`. Do not reset all PX4 parameters.
