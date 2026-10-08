# Field Inspection Operations

This document is the authoritative operator workflow for the field inspection
demo on the `real` runtime profile. Simulation staging helpers are development
tools and are not part of this workflow. OptiTrack lab flights use the reduced
`opti_track` profile instead, which has no cable, payload, or perception
workflow; follow the [OptiTrack lab session checklist](opti-track-lab-session-checklist.md)
for them.

## Control Authority

Only one system owns flight setpoints at a time. The GUI reports the fused owner
as PX4 manual/Position/Hold, Mission, CustomOperation, or a control transition.

| Activity | Authority |
| --- | --- |
| Arm, takeoff, manual positioning, manual landing | RC or QGroundControl |
| Inspection, reach cable, charge, leave, resume | onboard Mission executor |
| Typed engineering maneuvers | CustomOperation executor |
| Safety response | PX4 failsafe, RC, or QGroundControl |
| Configuration and mission intent | GUI through `iii-runtime-api` |

The GUI Flight page is for simulation and commissioning. It is not a joystick
and is not the primary field flight-control path.

## Aircraft Prerequisite: Hover Thrust

Before flying cable releases, set PX4's `MPC_THR_HOVER` to the aircraft's
measured hover thrust, for example the `hover_thrust_estimate` of a steady
hover in a flight log. PX4 restores it on every disarm, including on the cable,
so every CableTakeoff starts from it: an untuned value (PX4's default is 0.5)
makes the takeoff from the cable sag by tens of centimetres. The Leave Cable
push sizes itself from the hover thrust measured in flight, but only if the
aircraft flew steadily before landing on the cable. The runtime never writes
PX4 parameters; this is a manual commissioning step. See
[Leaving the cable](mission-and-behavior-layer.md#leaving-the-cable). Measure it
per payload configuration: the OptiTrack lab flies with and without the payload.

## Aircraft Prerequisite: PX4 DDS Domain

`iii host provision` writes the stack's ROS 2 domain for every aircraft profile
(`--ros-domain-id`, default 42) into `/etc/iii/runtime.env`. Set the flight
controller's `UXRCE_DDS_DOM_ID` to the same value (PX4's default is 0) and
reboot it; otherwise no PX4 topic reaches the stack. `iii px4 param-baseline
--profile real --host <pi>` sets it together with the rest of the
[real PX4 baseline](px4-parameter-baselines.md), and the Pi refuses to boot or
start the system while the flight controller differs from that baseline. `iii
px4 inspect --host <pi>` proves the domain with a `/fmu/out/vehicle_status_v1`
sample in it.

## Preparation And Start

1. On the aircraft, provision the real-profile runtime environment
   (`iii host provision --profile real`) and start the independently
   supervised `iii-runtime-api.service`. The runtime API is unauthenticated
   (developer access); the ground-control proxy refuses a runtime whose identity
   or profile does not match the pinned values.
2. On the operator laptop, [install the `deploy` ground-computer profile](ground-computer-installation.md),
   provision `~/.config/iii-ground-control.env`, and run
   `~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh start`.
   Confirm the pinned aircraft, runtime, and profile before connecting.
3. From Mission, use **Start aircraft system** for the canonical supervised
   boot/start path and confirm every readiness stage. The flight controller must
   be powered and connected first: the runtime API refuses runtime lifecycle
   commands unless live PX4 state shows the aircraft disarmed and landed.
4. Before arming, confirm the **Onboard clock settled** preflight item. The
   Pi has no trusted real-time clock, so chrony steps the clock when it first
   reaches a time source. A step during flight shifts every ROS timestamp and
   PX4's synchronized time. The item passes only when chrony is synchronized
   (`Leap status: Normal`) with a residual offset within 0.1 s. It is a hard
   gate for inspection activation and Runtime API arming on aircraft profiles.
   The Pi therefore needs a reachable time source in the field. On the Pi,
   `chronyc tracking` shows the same state. When the clock is unsettled, the
   GUI runs `iii host clock sync`, which steps the Pi or, without internet,
   makes the ground computer its time source; see
   [aircraft clock synchronization](ground-computer-installation.md#aircraft-clock-synchronization).
5. Arm and take off with RC/QGroundControl.
6. Fly manually to the powerline overview position.
7. Start PL mapper and inspect the live vector and orthogonal projection views.
8. Store the powerline overview after visual approval.
9. Fly manually to each pylon and capture endpoint 1 and endpoint 2. Capturing a
   slot again replaces it; clear removes both.
10. Position the aircraft outside the corridor, between pylons. Starting from
   either side is supported. The onboard eligibility check is authoritative.
11. Confirm every hard preflight item and start the constant inspection mission.

Stored overviews use global coordinates and are reprojected into the current
local world frame after a local-reference change. There is one stored powerline
overview and one two-endpoint pylon overview; a fresh store overrides it.

Battery reset is a simulation-only engineering control. It is absent from the
real-profile GUI and runtime command surface; field operation always uses the
aircraft's measured PX4 battery state.

## Configuration Changes

Every configuration change requires the configuration server to be available
and to accept the typed value. Changes are rejected while Mission owns control.
Non-constant parameters may be updated while disarmed and landed or while PX4
is in Hold. Constant parameters may be edited only while disarmed and landed;
the GUI marks them **Only valid after system restart** after persistence.

Use **Parameter cold restart** on the engineering Runtime page to restart all
managed nodes except the configuration server and promote accepted constant
values. An ordinary full cold restart also consumes the same saved values.
Neither restart is automatic, and both remain gated by fresh disarmed-and-landed
state.

## Takeover And Restart

For unexpected behavior, select Hold or Position with RC/QGroundControl. PX4
changes mode independently of the browser and deactivates the onboard executor.
The GUI records the takeover and reconciles to the new owner. It never
reactivates autonomy automatically. Inspect the cause, restore an eligible
state, and issue a fresh **Start Inspection** command.

Use RC/QGroundControl in every phase, including inspection, reach, charging,
leave, and recovery. On cable, first understand latch and gripper state before
commanding movement.

## Link And Process Loss

| Loss | Aircraft behavior | GUI behavior |
| --- | --- | --- |
| Browser close or lease expiry | Onboard autonomy continues | Mutations stop; a new lease is required |
| GC proxy or operator network loss | Onboard autonomy continues | State is marked stale; commands are rejected, never queued |
| Runtime API restart | Onboard autonomy continues | Reconnect waits for a complete snapshot before enabling commands |
| PX4 telemetry loss | Existing PX4/onboard behavior continues | Safety-dependent commands fail closed |
| ROS/uXRCE source loss or disagreement | Onboard safety remains authoritative | Affected gates fail closed and both sources remain visible |

Reconnect never replays a command. Every new mutation gets a new request ID and
requires current authoritative state.

## Stop Criteria

Stop mission operation and use RC/QGroundControl when the GUI reports PX4
failsafe, mission error, perception loss, charging failure, control-transition
timeout, stale safety telemetry, or an unexpected setpoint owner. Keep the
aircraft in Hold/Position unless a more urgent PX4/RC action is required.

The persistent alert links to Mission details. Preserve the inspection rosbag
and runtime logs; they include recent command, mode, ownership, and event context.

## Shutdown

After manual landing and disarm, verify fresh landed state, stop the managed
aircraft nodes from Runtime, and retain the runtime API until logs and rosbag are
exported. Stop the operator stack with
`~/.local/share/iii/gc/workspace/scripts/workspace/iii_ground_control.sh stop`;
it captures timestamped Compose
logs. Simulation fixture staging is documented separately in
`src/III-Drone-GC/docs/gui-v2-sim-e2e-smoke.md` and is never a field procedure.
