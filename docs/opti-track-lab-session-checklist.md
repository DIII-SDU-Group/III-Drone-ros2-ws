# OptiTrack Lab Session Checklist

Per-session procedure for flying the `opti_track` profile in the SDU OptiTrack
lab. The aircraft must already be commissioned as described in
[OptiTrack lab readiness](opti-track-lab-readiness.md) (provisioned Wi-Fi and
stack domain, deployed runtime, PX4 OptiTrack baseline, link inspection).

## Authority and safety

- A safety pilot with RC is present for every flight and may take over at any
  time. RC loss lands the aircraft; position loss in Position mode falls back
  to Altitude mode.
- QGroundControl runs on the ground computer over the telemetry radio. The III
  GUI and `iii` reach the Pi over the lab Wi-Fi.
- Disarmed checks run with the propellers removed or the propulsion battery
  disconnected.
- Lab rules: change nothing in Motive or on the lab gateway except your own
  rigid body; Motive stays on its default configuration; only the lab
  maintainer calibrates. Never restart services on the lab gateway.

Session ladder: lab health → rigid body → runtime → disarmed acceptance →
pilot-flown hover → `MPC_THR_HOVER` → GUI custom operations → M1 → M2 → M3 →
M4. Do not skip a rung; stop at the first failed check.

The commands below use `<pi>` for the drone Pi (`iii.local`, or its
`192.168.10.x` address on the lab Wi-Fi) and `<id>` for the aircraft's Motive
rigid-body ID. On the Pi, `source ~/ws/setup/setup_opti_track.bash` selects the
stack domain (42); prefix lab-topic commands with `ROS_DOMAIN_ID=0`.

## 1. Before each session: lab health

- [ ] Motive shows the NatNet streaming green check mark (bottom right) and the
      cameras are powered. If not, ask whoever runs Motive; do not change its
      configuration.
- [ ] On a lab-network computer with `mocap4r2_msgs`:
      `ros2 topic hz /rigid_bodies --window 50` reads close to 120 Hz.
- [ ] The ground computer is on `OptiTrack_5G` (not the 2.4 GHz `OptiTrack`).
- [ ] On the drone Pi, `ip -4 address show wlan0` has a `192.168.10.x` address
      and `ping -c 20 192.168.10.1` shows a stable round trip. Record it.
- [ ] On the drone Pi:
      `ROS_DOMAIN_ID=0 ros2 topic hz /body_splitter/body_<id>/pose --window 50`
      reads close to 120 Hz (after the rigid body exists).

## 2. Rigid body (first session, or after the markers change)

- [ ] The marker layout is asymmetric, so Motive cannot confuse orientations.
- [ ] Place the aircraft in the volume with its nose along Motive +x, select
      its markers, and create the rigid body.
- [ ] Translate the rigid-body pivot to the flight controller.
- [ ] Name the rigid body with its numeric Motive ID (lab convention) and
      confirm `ROS_DOMAIN_ID=0 ros2 topic list | grep body_<id>` on the Pi.
- [ ] Export the rigid body to your own file for the next session; leave
      Motive's default configuration in place.
- [ ] Record the ID, the up axis, and the axes in the
      [readiness worksheet](opti-track-lab-readiness.md#still-to-capture-at-the-lab).

## 3. Runtime

- [ ] Set `/opti_track/pose_relay/rigid_body_id` to `<id>` on the GUI
      Configuration page while disarmed and landed. It is a boot-time
      constant: restart the runtime (stop, then start) after changing it. Until
      it is set, the relay stays alive but never ready and reports
      `rigid_body_id not configured`.
- [ ] Boot and start the profile, from the GUI (**Start aircraft system**) or
      from the ground computer:

      ```bash
      III_SSH_HOST=<pi> iii --runtime-target opti_track system boot
      III_SSH_HOST=<pi> iii --runtime-target opti_track system start
      III_SSH_HOST=<pi> iii --runtime-target opti_track system status
      ```

      Every managed node is active, `micro_ros_agent` is ready, and the pose
      relay is ready. `system start` waits up to 120 s for the relay's
      `/opti_track/pose_relay/fresh` heartbeat; a timeout means no fresh pose
      (wrong or unset ID, no stream, or no Wi-Fi).
- [ ] `iii px4 inspect --host <pi> --profile opti_track` succeeds, including a
      `/fmu/out/vehicle_status_v1` sample in the stack domain.
- [ ] On the Pi, `ros2 topic hz /fmu/in/vehicle_visual_odometry --window 50`
      reads about 50 Hz (the relay output rate).
- [ ] The GUI shows the external-vision state fresh and the onboard clock
      settled.

## 4. Disarmed acceptance: PX4 local position against Motive

- [ ] QGroundControl shows a valid local position and heading, without GPS or
      magnetometer errors.
- [ ] On the Pi, `ros2 topic echo --once /fmu/out/estimator_status_flags`
      shows `cs_ev_pos`, `cs_ev_hgt`, and `cs_ev_yaw` true.
- [ ] Watch `ros2 topic echo /fmu/out/vehicle_local_position` (x, y, z,
      heading) while moving the aircraft by hand: a measured move along Motive
      +x and +y changes x and y by the same distance along the axes documented
      for the relay's Motive-to-NED conversion; lifting it makes z more negative
      (NED, down positive); turning it clockwise seen from above increases the
      heading. Nothing jumps or drifts while it rests.
- [ ] Stop if any axis, sign, scale, or heading disagrees. Keep the aircraft
      disarmed and record the streams.

## 5. Pilot-flown hover

- [ ] Connect the 6S propulsion battery. The safety pilot arms in Position mode
      and hovers at about 1.2 m for at least 30 s, then lands and disarms.
- [ ] The position hold is steady and the EKF stays healthy throughout.
- [ ] From the flight log, read `hover_thrust_estimate` of the steady hover.

## 6. `MPC_THR_HOVER` for the mounted payload configuration

- [ ] Set PX4's `MPC_THR_HOVER` (QGroundControl) to the measured hover thrust
      of this configuration. Measure with and without the payload separately
      and re-measure after any payload change: PX4 restores it on every disarm,
      so every takeoff starts from it. Deployment never writes PX4 parameters.
- [ ] Before the first autonomous flight, review the `real` parameter set that
      `opti_track` uses (maneuver and trajectory limits) against the lab and
      the cage. It has not been reviewed against what SIM/HIL qualify.

## 7. GUI custom operations, then the OptiTrack missions

- [ ] From a pilot hover, run the GUI custom operations (for example a hold
      and a small position step). The pilot takes over at any doubt.
- [ ] Missions, one rung at a time, each from the profile's mission catalog.
      Their geometry is in lab (Motive) coordinates and is a placeholder:
      confirm it against the cage before each first flight.
  - [ ] M1 "OT Hover" (`opti-track-hover`, profile default): take over from a
        pilot hover at 1.1 m or higher; hold 10 s, +0.3 m, hold, -0.3 m, hold,
        then PX4 Hold.
  - [ ] M2 "OT Maneuvers" (`opti-track-maneuvers`): airborne start; box
        (half-size 0.5 m at 1.2 m) stop-and-go and blended, yaw in place ±90°
        and 180°, height step to 1.5 m, waypoint path at 0.3 m/s, then Hold.
  - [ ] M3 `opti-track-cycle`: "OT Takeoff" (may arm when started on the
        ground; takes off to 1.2 m; waits up to 60 s for the Proceed intent
        from the GUI and lands without it), "OT Shuttle" (±0.6 m with a 180°
        turn), "OT Land" (PX4 land, disarm on touchdown).
  - [ ] M4 `opti-track-mode-loop`: "OT Loop Takeoff", then the endless loop
        "OT Lower Stop-and-Go" (1.2 m × 0.8 m rectangle at 1.2 m, stopping at
        each corner), "OT Upper Stop-and-Go" (at 1.6 m), "OT Lower Blended",
        "OT Upper Blended", and back. End it from the GUI or by the pilot's
        stick or RC mode switch.

## Motion-capture outage

Symptoms: the GUI's external-vision state turns stale, the relay is no longer
ready, or QGroundControl warns about the position estimate. The relay stops
publishing; PX4 dead-reckons for at most 1 s and then loses its position
estimate. In Position mode PX4 falls back to Altitude mode; an onboard mission
or custom operation loses control authority to PX4's failsafe.

1. The pilot takes over at once in Altitude (or Stabilized) mode, keeps clear
   of the nets, lands, and disarms.
2. Wait for the stream: the lab gateway's watchdog notices within about a
   minute, restarts after 8 s without data, and needs about 25 s, so allow up
   to 1.5 min. Never restart anything on the gateway yourself.
3. Before the next takeoff, confirm the pose rate on the Pi, relay readiness,
   and the estimator flags of the disarmed acceptance.
4. If outages repeat, end the session and tell the lab maintainer.

An occluded or lost rigid body (marker fallen off, aircraft outside the
volume) has the same symptoms and the same response.

## End of session

- [ ] Land, disarm, and disconnect the propulsion battery.
- [ ] Stop the runtime from the GUI or with
      `III_SSH_HOST=<pi> iii --runtime-target opti_track system stop`, after
      exporting the logs and rosbags you need.
- [ ] Export your rigid body if it changed, and leave Motive on its default
      configuration for the next user.
- [ ] Update the readiness worksheet with the values captured this session.
