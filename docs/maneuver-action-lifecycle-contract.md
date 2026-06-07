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
