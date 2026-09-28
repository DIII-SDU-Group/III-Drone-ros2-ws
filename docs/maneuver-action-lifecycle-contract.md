# Maneuver Action Lifecycle Contract

This contract covers the `maneuver_controller` scheduler, maneuver action
servers, mission BT action clients, and queue-clear behavior.

## States

- `queued`: A maneuver has been accepted by a maneuver action server and stored
  in the scheduler queue, but it is not yet the scheduler current maneuver.
- `accepted_not_executing`: ROS action goal was accepted/deferred, but
  `goal_handle->execute()` has not been called yet.
- `current`: The scheduler has popped the maneuver from the queue and assigned
  it as `current_maneuver_`.
- `executing`: The scheduler has called `Maneuver::Start()`, which calls
  `goal_handle->execute()`, and the maneuver server may publish feedback and
  references.
- `canceling`: The ROS action goal is canceling or the scheduler has decided to
  terminate the maneuver unsuccessfully.
- `terminated`: The maneuver has been marked done with success or failure.
- `queue_cleared`: Queued-but-not-current maneuvers were removed.
- `controller_stopped`: The lifecycle node is stopping or cleaning up.

## Invariants

- Only the scheduler owns queue/current promotion.
- Only one maneuver may be current at a time.
- `ClearManeuverQueue` clears queued maneuvers only. It must not cancel or
  mutate `current_maneuver_`.
- A stale retained reference callback from a completed hover maneuver must not
  clear a successor maneuver during handoff.
- A maneuver action server must never throw through the process because of a
  ROS action state transition. If a deferred goal must be terminated while it is
  still accepted but not executing, it must be moved through a valid ROS action
  transition or the transition failure must be logged and contained.
- Mission/custom PX4 mode layers must keep setpoint publication continuous
  while armed and active, even if maneuver reference acquisition fails.

## Queue Clear Semantics

`ClearManeuverQueue` is a handover primitive. It is used on mission/custom
activation and deactivation to discard pending work from a previous owner. It
does not imply that the currently executing maneuver should stop.

If a caller needs to stop the current maneuver, it must use the corresponding
ROS action cancellation path or switch PX4 ownership so the active mode can
perform controlled recovery.

## Terminal Position Tracking And Ownership

A stopped `FlyToPosition`, cable-aware position approach, or nonrepeating
`FollowWaypointPath` keeps its original nominal target and arrival tolerance.
After the planned stationary endpoint, Core may apply a bounded position-error
integral correction. This addresses an estimated velocity bias that otherwise
leaves a persistent position error in the downstream position/velocity loop.
Measured state is never replaced with the corrected command.

`TerminalPositionTrackingController` limits the correction to a 0.4 m ball,
0.1 m/s speed, 0.2 m/s² acceleration and 0.5 m/s³ jerk. Each committed segment
is a smooth rest-to-rest polynomial. The cable-aware maneuver further limits
the correction by its existing cable-clearance policy. Ordinary waypoint
maneuvers retain their existing geometry and guards; these bounds do not imply
a new physical cable-clearance guarantee.

Core owns the live `TerminalTrackingHold` after the ROS action finishes.
Successful action metadata still names the original target. A mission or
custom-operation client must retain the live command instead of substituting
the nominal target or current measured position. Cross-mode transfer uses
`TerminalHoldTransfer`: a fresh applied reference, the exact request and stream,
and a bounded offer/claim exchange identify the successor. A different target
first quiesces the correction and seeds its planner with the acknowledged finite
rest command. Same-target Hover retains the controller. Delayed predecessors
must not clear or report failure against the successor's ownership.

The scheduler owns whether and when a maneuver started. Worker completion reports
apply the terminal outcome to that same request without replacing its start
state or timestamps with the worker's pre-start copy. A never-started cancellation
remains unstarted and cannot establish retained command ownership.

When an action returns control, Core finalizes the exact retained callback before
publishing its availability; it does not wait for the next scheduler tick.
A transfer query during this same-owner finalization may report
`terminal callback finalizing`. Clients may wait for that specific transition
within their existing deadlines. Wrong owners, invalid generations, degraded
holds and stale acknowledgements remain failures. A degraded owner may retain
its finite stop command without becoming eligible for transfer.
Transfer queries observe callback ownership, execution and completion validity
together, serialized with token finalization and successor publication. They
must not mix observations from opposite sides of a completion transition.
A short action may finish before its newly published command generation receives
its first applied acknowledgement. An exact, fresh generation in that state
reports a pending transfer; the client may wait within its existing handoff
deadline. No transfer succeeds until the acknowledgement arrives. This does
not extend deadlines or admit stale acknowledgements, invalid generations or
degraded holds. Finite handoff seed commands use the producer's ROS clock.

Non-MPC `FlyToObject` must satisfy its original arrival predicate and establish
that the consumer has applied its own current command generation before the
action succeeds. A current-generation initialization command establishes that
ownership; it is not proof that a final target command was delivered. Published
but unacknowledged commands, predecessor acknowledgements and stale or negative
acknowledgements cannot satisfy this condition. Existing reference-loss and
cancellation behavior remains in force; a producer pause alone is not an
action-abort deadline.

Same-target Hover carrying a terminal tracking hold must also establish that
its own current generation has been applied before reporting success. An
unsustained Hover must not complete immediately and leave its caller waiting
for the next control tick inside a shorter retention-query deadline. Ordinary
Hover and sustained-duration behavior remain unchanged; this ordering rule
does not extend acknowledgement or retention deadlines.

Result cleanup must not report a handover complete while its request remains
pending because a predecessor's bounded terminal stop was deferred. The client
preserves the applied command and unresolved identity, reports refusal, and the
mission action propagates the existing ownership failure instead of reporting
behavior-tree success or dispatching another goal.
If a successor never becomes available, a failure callback must likewise retain
an exact still-applied terminal command rather than replacing it with measured
position. Repeated timeouts do not authorize that replacement. Explicit control
release and the existing Core stop/degradation paths remain responsible for
retiring the owner.

Native PX4 Hold ends that continuity contract. Fresh, advancing vehicle-status
evidence of Hold after the external-owner epoch retires only the matching
completed owner, callback and stream. Missing or stale status is insufficient,
and a newer execution or consumer claim fences the old evidence. Retirement
records the previous hold phase and failure reason without clearing action
outcomes or mission fault latches. The next explicit mission activation receives
`NoOffer` and starts from fresh measured state. This also applies when the old
hold degraded before the Hold status arrived; its old command is never treated
as continuously applied through native control.
Retirement and callback ownership changes share one serialization boundary.
Repeated consumer claims preserve the latest external-mode transition fence;
a delayed native-Hold observation cannot clear a newer owner.

FWP completion additionally requires the original position/yaw gate and an
acknowledged finite rest command. The planner fixes the acceleration of its
stationary start and nonrepeating final endpoint within jerk projection. A
final Blend waypoint without a successor still ends at rest, as required by
the existing zero terminal-speed rule; periodic loop seams retain their moving
boundary. `EstimatedPositionStopProof` measures the
total three-dimensional position travel over a full second, followed by the
existing settling dwell, with the existing speed and yaw-rate limits. Unique
source timestamps, fresh receipts and an unchanged estimator reset counter are
required; repeated polling cannot advance the proof. This avoids treating a
biased EKF velocity magnitude as conclusive evidence that a stationary position
estimate is moving. It does not establish independent physical rest or absolute
GNSS accuracy.

Planned cancellation of a moving FWP retains the existing bounded stop profile.
After the same applied-rest and position-history proof, it retains the finite
stop command without enabling integral tracking. The BT action waits for the
canceled result before handing off to the next mode. Missing or stale ownership,
a failed proof, or unavailable transfer service must fail the operation; halt
failure is recorded without throwing through tree teardown.

Stale state, estimator resets, exhausted correction authority or nonconvergence
cause terminal tracking to stop integrating and finish its committed segment.
The terminal stream reports degradation after reaching that segment's rest
endpoint. An invalid clock that prevents this stop is an explicit unrecoverable
state. Neither case authorizes an automatic reset of continuity checks or a
successful mission transition. Absolute position bias still requires independent
relative perception or another navigation reference; terminal tracking alone
cannot correct an unobservable error.

PX4's odometry reset counter aggregates position, velocity and heading resets.
Core compares it with fresh, source-matched `VehicleLocalPosition` reset-type
and origin metadata. Only a proved heading-only increment may preserve the
position integrators' continuity identity; position, velocity, origin, source
reversal or missing evidence retain the bounded fault-stop behavior. Both
awareness and world-to-drone TF publish the unshifted PX4 local position (with
the existing axis conversion), matching Mission feedback and setpoints. A
qualified heading correction still changes the **raw** reset counter and
restarts the measured one-second rest proof; it never certifies physical rest.

## Object Approach Tracking

Non-MPC `FlyToObject` and its following `HoverByObject` share an
`ObjectTrackingSession` when the configured cable-landing controller supports
its command-seed transition (currently `line_pid`). Selection depends on the
controller capability, not on a SIM/HIL profile name. Other landing controller
branches retain their existing approach behavior: the MPC API does not yet
consume a separate outgoing command anchor alongside measured-state feedback.
The original live object target, filtered nominal
target, tracking correction and commanded target remain distinct. Arrival,
feedback and successful results refer to the original nominal target; the
correction never changes the mission's requested stand-off or arrival tolerance.

The session estimates persistent command-to-measured-position error from fresh,
unique odometry and its own current command. It applies a slow bounded offset
upstream of `bounded_positional` interpolation (trajectory mode 4). The bounded
interpolator preserves outgoing position, velocity and acceleration on retarget
and certifies its polynomial against the configured translational and yaw
derivative limits. Ordinary interpolation, MPC and cable-takeoff selection keep
their existing behavior. Mode 4 rejects an MPC request.

Successful compensation can leave a nonzero difference between commanded and
measured position: that difference is the offset needed to make measured
position reach the nominal target. Nonconvergence checks must distinguish this
expected offset from uncompensated error. Neither a distant final target during
legitimate transit nor elapsed action time alone proves a tracking failure.

The session continues through FlyToObject's successful object-hover callback
and the subsequent explicit HoverByObject request. An empty maneuver queue does
not retire an exact still-owned object-hover callback. Successor initialization
uses the retained finite command, binds the new request/execution and requires
a real current-generation applied acknowledgement before immediate completion.
Old callbacks, acknowledgements or action results cannot recover ownership.
The reference stream identifies this ownership with `object_tracking_active`;
it does not label the moving session as a stationary terminal hold. Successful
FTO/HBO completion requires acknowledgement of a command carrying that marker,
not an earlier initialization command. The client keeps the acknowledged live
object stream when the action completes; the nominal result remains metadata.
Unmarked ordinary approaches retain their existing completion behavior.
An explicit stop of a retained object stream uses the existing ACK channel's
`STATUS_OBJECT_STOP_REQUESTED` and preserves the exact active request/stream.
The request must refer to recent actually applied object-command evidence;
it does not itself advance or refresh applied acknowledgement state. Core owns
the certified stop, reports `STATE_OBJECT_STOPPING` while emitting it, and
publishes `STATE_OBJECT_STOPPED` only at its finite rest
endpoint. The consumer continues applying the stop and switches to Hold only
after applying that same owner's stopped command, retaining its exact rest
reference. A stale timer cannot stop a successor. Neither an action's nominal
result nor a measured pose may replace the moving command at timeout. Failure
to complete the stop is explicit; it must not silently retain moving control
forever or claim successful rest. This exchange does not change the tree's
timeout or reinterpret non-sustain HBO duration as an independent stop request.
Stop admission uses the configured maneuver-start timeout. After an applied
stopping sample, the completion failure deadline uses the existing maximum
certified stop duration plus producer publication and stream-freshness budgets.
Successful applied rest ends the wait immediately; retries cannot extend it.
Local Hold continues to consume and acknowledge that exact owner's stopped
stream while emitting its fixed rest command. This retains fresh command
ownership for a delayed successor; it does not resume object tracking. A client
control reset fences that retained binding, and an evidenced PX4 native-Hold
takeover retires it under the existing navigation epoch/transition rules.
Local offboard Hold and PX4 native Hold are different authority states. A
historical STOPPED acknowledgement alone is not durable authority after local
reference control changes. Supported positional successors use the existing
fresh applied-rest and finite-start path and retire the old session on takeover;
unsupported successors fail explicitly without substituting a measured seed.
Paused or prepared cached samples retain those states even if a stop was
requested; they cannot advertise active stop progress.
Before the subsequent CableLanding handover, the session finishes an owned
bounded stop and requires an applied rest command. Line PID receives the stopped
pose anchor before taking ownership through its existing mixed position/velocity
channels. The first landing command remains subject to the ordinary reference
guard. This does not establish corrected-session continuity through MPC Landing.

`minimum_target_altitude` remains a target constraint, including the current
ground-altitude estimate. A session starting below that target level may still
recover upward. The new command safeguard uses the lower of the initial seed
altitude and initial target minimum as its fixed floor. Before accepting each
new command, the session checks that command and the complete bounded stop from
its position, velocity and acceleration against the floor. It retains the
previous accepted stop until the new command passes. A loss of tracking input,
an estimator reset, exhausted authority or cancellation must preserve command
ownership through the committed stop rather than substitute measured pose or
release a moving command. An infeasible initial seed is an explicit failure.

The 0.4 m offset cap is not a conductor-clearance policy. FlyToObject and
HoverByObject have no separate full-path cable-clearance rule; the nominal
TargetProvider stand-off and HoverByObject's maximum target-tracking distance
must not be misrepresented as one. These checks bound emitted command points
and the committed stop curve. They do not establish physical clearance under
localization uncertainty or certify unconsumed portions of every planned curve.

## Failure Semantics

- A failed current maneuver terminates current execution and clears queued
  successors.
- A failed retained hover reference callback only clears queued successors when
  the current maneuver is an active hover maneuver. If the current maneuver is
  already terminated or is a successor, the callback is stale and must be
  ignored.
- A dead maneuver action server is a mission failure, not a mission executor
  process failure.
- Scenario tooling must treat critical node nonzero exits, PX4 failsafe, and
  unexpected landing before cleanup as failed safety verdicts.
