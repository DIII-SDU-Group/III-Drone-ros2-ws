# Maneuver Reference Stream Contract

Maneuver actions remain the lifecycle contract. Control references use a bounded-lifetime stream between `maneuver_controller` and the active PX4 mode.

## Data flow

- Topic: `/control/maneuver_controller/reference_stream`
- QoS: volatile, best effort, `KEEP_LAST(1)`, finite deadline and lifespan
- Acknowledgements: `/control/maneuver_controller/reference_ack`, reliable
- Every sample identifies its generation with `stream_id` and is ordered by `sequence`.
- `produced_at` and `valid_until` make stale data invalid independently of queue depth.
- `trajectory_time_s` is diagnostic time within the current generation; it resets on rebase.
- Producers and consumers interpret message timestamps in their ROS node clock domain. Receipt and
  acknowledgement watchdog durations use a monotonic steady clock and never compare against ROS time.

The consumer rejects invalid, expired, wrong-generation, and out-of-order samples. It acknowledges only references that passed continuity validation and were handed to PX4.

Blended action handoff is an explicit exception to generation pinning. When the behavior-tree action
attaches to a still-active blended predecessor, the consumer authorizes exactly one successor
generation. Samples from the predecessor remain valid until that successor arrives. The first
successor sample must still pass the normal kinematic continuity check; further unsolicited
generation changes remain invalid.

## Loss handling

If the consumer does not receive a valid reference before `/mission/reference_loss_timeout_ms`, it:

1. publishes `STATUS_PAUSING` and requests producer pause;
2. retains control and follows a jerk- and acceleration-bounded stop trajectory;
3. waits for measured velocity and yaw rate to settle;
4. asks the active producer for its recovery disposition.

The producer independently pauses when the first acknowledgement is not received, or subsequent acknowledgements stop, for longer than `/control/maneuver_controller/reference_stream_timeout_ms`. While paused it does not generate references or evaluate maneuver success/failure.

Acknowledgement expiry gates the scheduler before every execution tick. This ordering is mandatory:
after executor congestion, queued trajectory or completion callbacks must not run before the pause
decision and retire the maneuver that the consumer is preparing to rebase.

Rebase ownership is validated against the scheduler's active maneuver identity. Lifecycle flags on
the action server's private `Maneuver` copy are not authoritative.

Simulation fault injection uses `scripts/workspace/inject_reference_stream_stall.py`. It waits for
the selected provider's ACTIVE generation instead of relying on shell output timing, and guarantees
that a stopped controller process is resumed.

## Recovery handshake

After stopping, the active maneuver chooses one of two non-fatal outcomes:

- `REBASE`: transparently resume the same action using the handshake below.
- `ABORT_ACTION`: consumer enters hover and publishes `STATUS_ACTION_ABORT_READY`;
  only then does the producer abort the action, allowing the behavior tree to execute
  its explicit recovery branch without racing the consumer's control transition.

A rejected or timed-out recovery request is fatal to the active mode.

### Transparent rebase

1. Consumer sends the measured stopped state and last applied sequence to `rebase_reference_stream`.
2. The active maneuver replans its remaining objective from that state.
3. Producer creates a new `STATE_PREPARED` generation anchored exactly at the stopped state.
4. Consumer verifies and acknowledges the anchor with `STATUS_STOPPED`.
5. Consumer calls `commit_reference_stream` with the exact prepared sequence it verified. The
   request is the commit proof; correctness does not depend on topic/service arrival ordering.
6. Producer publishes the stopped anchor as `STATE_ACTIVE`, continuing the prepared generation's
   monotonically increasing sequence, but keeps maneuver progression paused.
7. Consumer applies that continuous anchor and acknowledges it with `STATUS_APPLIED`.
8. Producer releases the rebased maneuver only after that acknowledgement; subsequent active
   samples resume trajectory progression and the existing action.

No prepared generation controls the vehicle before commit. A failed or timed-out handshake fails the maneuver after the vehicle has stopped.

## Rebase support

Transparent rebase is implemented for FlyToPosition, CableAwareFlyToPosition, FlyToObject, FollowWaypointPath, CableTakeoff, and Hover. FollowWaypointPath retains the remaining cyclic route from the interrupted segment; CableTakeoff retains its original frozen clearance target.

CableLanding deliberately selects `ABORT_ACTION`. The consumer holds the bounded-stop pose while the Reach Cable behavior tree receives `ACTION_ABORTED`, flies back through its under-cable approach, and retries landing using fresh live perception.

Perception-relative hover controllers deliberately reject transparent rebase. Their phase-local controller state cannot yet be reconstructed with the same safety guarantee; a stream fault therefore stops and fails the action rather than resuming an ambiguous phase.

`/control/maneuver_controller/get_reference` remains available as a diagnostic compatibility endpoint. Mission and custom-operation flight control do not use it.
