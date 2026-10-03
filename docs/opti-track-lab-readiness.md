# OptiTrack Lab Readiness

This is a capture worksheet and acceptance boundary for the uncommissioned
`opti_track` profile. It is not an instruction to boot that profile, change a
PX4 parameter, or fly the aircraft.

## Authority and safety boundary

Use this only during the OptiTrack lab session with the aircraft disarmed and
without a propulsion battery. PX4 remains connected to the Pi solely by its
dedicated Ethernet/uXRCE link; the workstation USB connection is for
configuration and read-only MAVLink inspection only. Keep the profile
non-bootable until every input below is recorded and the bridge has passed its
software tests.

## Record before implementation

Record observed values, not defaults:

| Required fact | Observed value | Evidence retained |
| --- | --- | --- |
| Motive/OptiTrack server address |  |  |
| Transport mode and data route (unicast or multicast) |  |  |
| Aircraft rigid-body name and ID |  |  |
| Motive world-frame axes, origin, and handedness |  |  |
| Rigid-body forward/up alignment relative to the aircraft body frame |  |  |
| Bridge host and ROS domain/network route to the Pi |  |  |
| Measured pose rate, timestamp source, and observed latency |  |  |

Stop if any value is unavailable, ambiguous, or inconsistent with the Motive
display. Do not select a rigid body by a guessed name or infer an ENU/NED/FRD
conversion from a diagram alone.

## Fixed integration seam

The bridge must convert the agreed OptiTrack pose contract into
`px4_msgs/msg/VehicleOdometry` and publish it on the existing PX4 uXRCE input
`/fmu/in/vehicle_visual_odometry`. The checked-in PX4 DDS table already exposes
that input, and `VehicleOdometry` requires the pose frame, position,
orientation, velocity-frame data, variances, timestamp, reset counter, and
quality to be explicit.

The bridge implementation must be configured from the recorded lab facts. It
must reject missing data, a different rigid body, unknown frame metadata, stale
pose data, or an unavailable PX4 input; it must never silently publish a
guessed transform or re-enable the profile after a loss of pose freshness.

## Acceptance sequence

1. Capture a stable selected-rigid-body stream while the aircraft remains
   disarmed. Retain the raw source identity, frame statement, rate, and
   timestamp evidence.
2. Add and test the bridge with a fixture representing that recorded contract.
   The test must prove the chosen frame conversion, message fields, and stale
   input rejection.
3. Make `opti_track` bootable only when supervision owns the bridge and waits
   for its readiness/freshness contract.
4. With the aircraft still disarmed, observe the bridge input and
   `/fmu/in/vehicle_visual_odometry`, then verify PX4 reports the corresponding
   accepted external-vision/vehicle-odometry response.
5. Record the successful stream, PX4 response, exact candidate revisions, and
   final safety state in the deployment backlog before considering a field
   profile.

## Recovery and stop conditions

If the source drops, its identity changes, timestamps regress, or pose
freshness expires, keep the profile unavailable and stop field validation. If
the PX4 response does not agree with the bridge output, preserve both streams
and resolve the frame/message contract before any retry. Do not reset PX4
parameters globally and do not substitute HIL odometry for the missing
OptiTrack input.

Next: bring the completed table and one recorded rigid-body sample to the
bridge implementation step. Until then, HIL remains the only commissioned
physical profile.
