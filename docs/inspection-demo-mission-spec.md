# Inspection Demo Mission Implementation Spec

## Purpose

Implement a proper inspection demo mission that is live reproducible and does not rely on simulation-only cheating inside the mission behavior.

The demo should show that the drone can:

- Fly around the powerline corridor using stored, live-acquired scene information.
- Inspect conductors and pylons in a deterministic sequence.
- Trigger recharge automatically or manually.
- Run the existing `Reach Cable -> Cable Charging -> Leave Cable` flow.
- Resume the inspection mission after leaving the cable.

The current demo world has one powerline span between two pylons. The mission path may be somewhat hardcoded for this setup, but should be dynamically computed from stored powerline and pylon overview data.

## High-Level Mission Model

The demo mission is implemented as a mission-owned PX4 mode using the same generic maneuver mode and behavior tree setup as the existing modes:

- `Reach Cable`
- `Cable Charging`
- `Leave Cable`

The new mode should be named something like:

- `Inspection Demo`
- mode key: `inspection_demo`

It should not be implemented as a standalone custom state machine or standalone controller. It should use the existing mission executor, maneuver mode, behavior tree, and maneuver controller architecture.

The mission specification should chain modes conceptually like:

```text
Inspection Demo -> Reach Cable -> Cable Charging -> Leave Cable -> Inspection Demo -> ...
```

`Inspection Demo` continues indefinitely. A successful termination of `Inspection Demo` means only:

```text
Recharge requested; proceed to Reach Cable.
```

It does not mean that the inspection mission is globally complete. The demo stops only through normal external interruption, for example switching to Hold or another PX4 mode.

## Required Stored Scene Data

### Powerline Overview

The inspection demo requires a stored powerline overview, using the existing powerline overview provider.

The powerline overview provides:

- conductor positions
- conductor IDs
- powerline direction
- projection plane / corridor geometry
- top conductor altitude
- target conductor information for `Reach Cable`

### Pylon Overview

Add a new pylon overview mechanism similar to the existing powerline overview provider.

For the current demo, it is acceptable to manually store pylon locations by flying above each pylon and calling a service/tool.

The pylon overview should store only the information needed to define the
powerline span in the horizontal plane:

- pylon ID
- pylon XY position in `world` frame
- frame ID
- timestamp

Do not require pylon role, pylon height, bounding box, or a full pose for the
initial implementation. Pylon height for waypoint generation is derived from the
highest-altitude conductor in the stored powerline overview plus configured
margins.

Waypoint generation must not rely on which pylon is numbered `1` or `2`. The
two pylons are treated as unordered span endpoints. Any entry/opposite-side
logic must be derived geometrically from the current drone position, powerline
overview, and pylon endpoint projections.

The pylon-to-pylon XY vector is the authoritative span direction for inspection
endpoint stations. The stored powerline overview direction is used to validate
that the pylon span direction is consistent. If the angular mismatch exceeds a
configured threshold, overview validation should fail.

Suggested parameter:

```text
inspection_demo.max_pylon_powerline_direction_mismatch_rad
```

It should be possible to query whether a pylon overview is available.

For this single-span demo, a valid pylon overview requires exactly two pylons.
One pylon is insufficient, and more than two pylons should be rejected until
multi-span inspection is explicitly implemented.

Suggested services:

- `StorePylonOverview`
- `GetPylonOverview`
- `ClearPylonOverview`

For an initial implementation, `StorePylonOverview` may store the current drone
XY position as the pylon position, with an explicit pylon ID. The ID is for
operator clarity and replacement/update semantics, not for path-direction logic.
Storing a pylon with an existing ID replaces that pylon entry. Validation then
checks whether exactly two distinct pylon IDs are present.

## Inspection Demo Behavior Tree

The inspection demo tree should be behavior-tree driven.

Startup validation:

```xml
<Sequence>
  <VerifyPowerlineOverview/>
  <VerifyPylonOverview/>
  <EnsureInspectionPlanInitialized/>
  ...
</Sequence>
```

`Inspection Demo` only consumes pre-stored pylon overview. It does not discover
or store pylons itself. Pylon overview storage is performed before mission
activation, for example through the existing deploy/setup workflow and MCP/GUI
tooling.

The tree should use the persistent global blackboard already provided by the mission executor tree launch path. This blackboard should preserve inspection state between activations of `Inspection Demo`.

Suggested persistent blackboard keys:

```text
inspection_demo.plan_initialized
inspection_demo.active_phase
inspection_demo.phase_waypoints
inspection_demo.current_waypoint_index
inspection_demo.active_conductor_slot
inspection_demo.active_direction_endpoint
inspection_demo.phase_kind
inspection_demo.initial_partial_span_anchor
inspection_demo.recharge_requested
inspection_demo.recharge_reason
inspection_demo.resume_state
inspection_demo.completed_phase_count
charging.bypass_battery_full_check
charging.interrupt_requested
```

## Inspection Phases

The mission should execute recurring inspection phases based on the stored
powerline overview and pylon overview. The conductor slot cycle is:

```text
slot 1 -> slot 2 -> slot 3 -> slot 4 -> repeat
```

When the demo starts from an arbitrary position, it should not blindly begin at
slot 1. `PhaseWaypointProvider` chooses the closest conductor slot and the
closest point on that conductor span as the initial starting phase. It then
generates a custom staging path from the current drone position to that selected
starting point. After that, the normal cyclic order continues.

Closest conductor selection is based on the generated inspection path geometry,
not the raw conductor geometry. For side conductors, distance is computed to the
outside-offset inspection line. For the top conductor, distance is computed to
the above-top inspection line. The selected start point is the closest clamped
point on that inspection segment.

Initial staging to the selected inspection start uses only normal
`FlyToPosition` waypoints. By default, stage through top-clearance altitude:

1. fly vertically from current XY to `top_clearance_z`
2. fly horizontally at `top_clearance_z` to the selected inspection start XY
3. fly vertically to the selected inspection start Z if needed

Exception: if the drone starts inside the corridor and between the two pylons,
use the same style of staging waypoints as `Reach Cable` before transitioning to
top-clearance routing:

1. bottom entry-clearance waypoint in the mid corridor
2. bottom entry-clearance waypoint outside the corridor on the entry side
3. transition through top-clearance altitude

This keeps the initial escape from inside-corridor positions consistent with the
existing `PowerlineWaypointProvider` logic while still avoiding CAFTP.

`PhaseWaypointProvider` should not invoke `PowerlineWaypointProvider` as a BT
node. Instead, extract shared geometry helpers used by both providers:

- corridor classification
- top/middle reference conductor selection
- one-side/two-side conductor split
- entry-side side selection
- bottom entry-clearance waypoint generation
- top-clearance waypoint generation

The first conductor phase may be partial. If the closest start point is in the
middle of a span, the drone may inspect from that closest point to the selected
next pylon endpoint instead of first backtracking to cover the full span. Full
coverage is achieved over subsequent indefinite cycles.

Progress should be stored as both logical phase state and the generated waypoint
queue for the active phase. The active phase queue remains stable across recharge
interruptions, so the same unfinished waypoint is retried after `Leave Cable`.
Waypoint queues are regenerated at phase boundaries, or when the stored state is
missing/invalid.

Progress advances at waypoint granularity:

- increment `inspection_demo.current_waypoint_index` immediately after a waypoint action succeeds
- when the waypoint index reaches the end of the active queue, advance the phase and generate the next phase queue
- if recharge interrupts while a waypoint is running, do not advance the waypoint index

If the stored powerline or pylon overview changes while a phase is already
running, do not automatically replan mid-phase. The active queue should carry the
overview timestamp/hash used to generate it for diagnostics. Updated overview
data is applied only at the next phase boundary. This avoids target jumps during
long-running FTP actions.

Inspection progress should persist across the intentional
`Inspection Demo -> Reach Cable -> Cable Charging -> Leave Cable -> Inspection Demo`
cycle because that is normal mission sequencing. However, the mission executor
must clear the global blackboard automatically when any mode is interrupted,
fails, or completes as a terminal mode. This prevents stale inspection progress
or intent flags from surviving manual Hold, external mode changes, failures, or
mission completion.

No separate reset-progress service is required for the initial implementation.

The path alternates between conductor inspection and pylon vertical inspection
where appropriate, so the route remains continuous instead of repeatedly
returning to a fixed origin. Pylon order must be chosen geometrically from the
current conductor traversal direction and the two unordered pylon endpoints.

When finishing any conductor inspection segment, transition to the next conductor
through a top-clearance pylon sequence:

1. Fly directly up from the conductor-inspection endpoint to the top-clearance
   altitude.
2. Fly horizontally to the pylon top-clearance point.
3. Execute the pylon down/up scan.
4. Fly horizontally to the next conductor's start XY at top-clearance altitude.
5. Fly directly down to the next conductor's correct inspection altitude.

All of these transition waypoints use normal `FlyToPosition`.

### Conductor Inspection Geometry

For side-conductor slots, the drone
flies next to the conductor at the same altitude as the conductor. The lateral
clearance is defined by a parameter. The offset direction is away from the other
conductors, so the drone remains outside the corridor on that conductor's side.

Side-conductor inspection endpoints are constructed from both pylon and
powerline overview geometry:

- the along-span coordinate comes from the two pylon XY positions
- the orthogonal cross-corridor coordinate comes from the conductor slot in the
  powerline overview
- altitude comes from the conductor slot altitude in the powerline overview
- outside-corridor clearance is applied along the cross-corridor direction away
  from the other conductors

This avoids relying on noisy perceived conductor endpoint positions while still
using the stored powerline overview to locate each conductor in the cross-section.

For the `top` conductor slot, the drone flies directly above the top conductor
with the same configured clearance. The start and stop XY positions are the two
pylon XY positions. The top conductor is used only to determine the altitude:

```text
top_clearance_z = top_conductor_z + inspection_demo.inspection_clearance_m
```

The top inspection path therefore follows the pylon-to-pylon span in XY, rather
than relying on possibly noisy top-conductor projected XY endpoints.

Initial parameter:

```text
inspection_demo.inspection_clearance_m
```

### Pylon Vertical Inspection Geometry

Pylon inspection starts at top-cable clearance altitude beyond the pylon XY
position, then descends near the ground, then ascends back to the same
above/beyond pylon position.

The top-clearance altitude is the same value used by top-conductor inspection
and all conductor-to-pylon transition staging:

```text
top_clearance_z = top_conductor_z + inspection_demo.inspection_clearance_m
```

The pylon scan waypoints are:

```text
pylon_xy + beyond_pylon_offset at top_conductor_z + inspection_clearance_m
pylon_xy + beyond_pylon_offset at ground_z + pylon_low_height_above_ground_m
pylon_xy + beyond_pylon_offset at top_conductor_z + inspection_clearance_m
```

The beyond-pylon offset magnitude uses the same clearance distance unless a
dedicated pylon clearance parameter is introduced later.

The pylon vertical scan itself must use normal `FlyToPosition`.

Initial parameter:

```text
inspection_demo.pylon_low_height_above_ground_m = 1.5
```

`ground_z` should come from the live combined drone awareness ground estimate,
matching the approach already used by `PowerlineWaypointProvider` for minimum
waypoint altitude. If that estimate is unavailable, use a fixed configurable
fallback ground altitude.

The exact geometric details should be refined during implementation, but the architecture should support each phase being defined as a generated queue of waypoints.

Conductor ordering for the demo is local to waypoint computation. Do not store
semantic conductor roles in the powerline overview, and do not require semantic
conductor labels in the demo BT. `PhaseWaypointProvider` derives the ordered
sequence from the stored overview:

1. the conductor on the side where there is only one conductor
2. the top/middle conductor used as the cross-section reference
3. the highest conductor on the side where there are two conductors
4. the lowest conductor on the side where there are two conductors

The top/middle conductor is first identified as the reference conductor in the
powerline cross-section. The remaining three conductors are divided by which
side they appear on in the axis perpendicular to the powerline direction,
relative to that top/middle conductor. One side should contain one conductor;
the other side should contain two conductors. The two-conductor side is sorted
by altitude to produce slots 3 and 4.

Reference conductor selection should reuse the same logic as
`PowerlineWaypointProvider`. Prefer factoring the shared cross-section/reference
selection into a helper so `Reach Cable` and `Inspection Demo` do not drift. If a
valid one-side/two-side split cannot be computed around the reference conductor,
`PhaseWaypointProvider` should fail.

These ordered slots are internal planning roles only. The raw stored overview
remains a geometric observation with conductor IDs and poses.

## PhaseWaypointProvider

Add a BT action node:

```text
PhaseWaypointProvider
```

This should be modelled after the existing `PowerlineWaypointProvider` node used in `Reach Cable`.

Inputs:

- current active inspection phase from blackboard
- stored powerline overview
- stored pylon overview
- current drone state or pose
- previous phase end position from blackboard, if available

Outputs:

- queue of waypoints for the active phase
- optional metadata about the phase path

Responsibilities:

- Generate the waypoints for the current active phase.
- Include safe staging waypoints from the current position or previous phase end position to the current phase.
- Generate all waypoints so they can be executed with normal `FlyToPosition`.
- Handle phase-specific transitions, for example:
  - conductor inspection to pylon scan
  - pylon scan back to conductor inspection
  - one conductor side to another conductor side
- Encode special transition logic inside this node, instead of spreading it across the tree XML.

This node owns inspection phase path generation. The behavior tree owns sequencing and interruption.

## Waypoint Consumption

After `PhaseWaypointProvider` creates a waypoint queue, the BT should loop over that queue and execute one waypoint at a time using the existing `FlyToPosition` action-node pattern.

This should be similar to the current `Reach Cable` tree pattern:

```text
Generate waypoints -> consume waypoints with FTP
```

All inspection demo waypoints should be executed with normal `FlyToPosition`.
Do not use `CableAwareFlyToPosition` in the inspection demo. Any required safety
or corridor avoidance must be baked into the waypoints produced by
`PhaseWaypointProvider`.

The existing action nodes are haltable. If a recharge interruption occurs while an FTP maneuver is executing:

- the running FTP action is halted
- the underlying maneuver is stopped/cancelled through existing action halt behavior
- current phase and waypoint index remain stored
- `Reach Cable` already stores the position/path from which it was activated
- `Leave Cable` consumes the stored reach-cable waypoints in reverse order and returns the drone to the position from which inspection was interrupted
- when `Inspection Demo` resumes, it attempts the same unfinished waypoint again

This avoids needing a custom resume controller.

No new inspection-specific resume-pose handoff is required for `Leave Cable`.
The existing `Reach Cable` / `Leave Cable` waypoint storage and reverse-path
behavior should be reused.

## Recharge Interruption While Actions Run

Recharge must be triggerable during any stage of the inspection demo, including while a `FlyToPosition` action is running.

Use BehaviorTree.CPP reactive control nodes for this.

Preferred structure:

```xml
<ReactiveFallback name="RechargeInterrupt">
  <Sequence>
    <ShouldRecharge/>
    <PrepareRechargeExit/>
  </Sequence>

  <ExecuteCurrentInspectionWaypoint/>
</ReactiveFallback>
```

Normal case:

- `ShouldRecharge` returns `FAILURE`
- the waypoint action runs

Recharge case:

- `ShouldRecharge` returns `SUCCESS`
- `ReactiveFallback` halts the running waypoint action
- `PrepareRechargeExit` stores any needed resume state
- the tree returns `SUCCESS`
- the mission specification advances to `Reach Cable`

This relies on the existing haltable action-node implementation. No additional setpoint publisher should be introduced.

## Recharge Conditions

`ShouldRecharge` should check both automatic and manual triggers.

A global parameter should be able to bypass battery checks entirely. When this
parameter is true, automatic battery-low recharge and battery-full charging exit
checks are disabled. The demo then relies only on manual recharge and manual
charging interruption.

Suggested parameter:

```text
mission.bypass_battery_checks
```

When `mission.bypass_battery_checks = true`:

- `Inspection Demo` ignores automatic battery-low recharge triggers
- `Inspection Demo` does not fail because the charger/gripper battery telemetry
  topic is missing or stale
- only `TriggerRechargeNow(true)` can make `Inspection Demo` exit for recharge
- `Cable Charging` ignores battery-full exit checks
- `Cable Charging` stays on the cable indefinitely until
  `InterruptRechargingNow(true)` is called or the operator externally changes
  mode
- `StayOnCable` remains available but is effectively redundant while the global
  bypass is enabled

This bypass only applies to battery decision logic. Gripper/latch state required
by `Reach Cable`, `Cable Charging`, or `Leave Cable` remains mandatory.

Automatic trigger:

- battery voltage or battery state from the charger/gripper node topic
- threshold configured as a mission/mode parameter

There is no fallback to PX4 battery status for automatic recharge decisions. The
charger/gripper node topic is the authoritative source for this demo behavior.
If the topic is missing or stale, `Inspection Demo` should fail rather than
silently request recharge or continue. Each battery check should sample/retry a
configured number of times before declaring failure, so transient timing gaps do
not cause immediate mission failure.

Suggested parameters:

```text
inspection_demo.battery_topic_timeout_s
inspection_demo.battery_check_retry_count
inspection_demo.battery_check_retry_interval_s
inspection_demo.battery_voltage_threshold_v
inspection_demo.battery_voltage_debounce_s
```

The recharge check should be a subscriber-backed, nonblocking BT condition during
normal operation. It should use the latest received charger/gripper battery
message on each BT tick. Bounded retry/sampling is used only when data is missing
or stale, not as a blocking operation on every tick.

Manual trigger:

- blackboard flag set by a runtime intent service

When recharge is triggered:

- set `inspection_demo.recharge_requested = true`
- set `inspection_demo.recharge_reason`
  - `battery_low`
  - `manual_trigger`
- set `charging.bypass_battery_full_check = true` if reason is `manual_trigger`
- terminate the inspection demo tree with `SUCCESS`

The charging bypass flag is interpreted in `Cable Charging`, not in `Inspection Demo`.

## Cable Charging Behavior

`Cable Charging` should support these blackboard-controlled behaviors:

- `charging.bypass_battery_full_check`
- `charging.interrupt_requested`

If `charging.bypass_battery_full_check` is true:

- charging should stay on the cable even if the normal battery-full condition is met
- this is automatically set when recharge reason is manual trigger

If `charging.interrupt_requested` is true:

- charging should terminate successfully
- clear or consume the interrupt flag in the tree
- mission specification proceeds to `Leave Cable`
- this interrupt is immediate and overrides the minimum stay-on-cable duration

`Cable Charging` should enforce a minimum stay-on-cable duration before it may
exit for any normal reason. This prevents immediately transitioning away from
the cable after a successful latch and gives charger/gripper state time to
stabilize.

Initial parameter:

```text
cable_charging.minimum_stay_on_cable_s = 10.0
```

The bypass flag should be cleared by the behavior tree at the appropriate point, for example when `Cable Charging` exits or when `Leave Cable` begins.

## Runtime Intent Services

Avoid hardcoding custom service servers directly as hand-written mission executor node logic for each feature.

Instead, augment the mission specification to support soft specification of runtime intent services.

Example mission specification section:

```yaml
intent_services:
  - service_name: /mission/inspection_demo/trigger_recharge_now
    flag_name: inspection_demo.manual_recharge_requested
    type: bool

  - service_name: /mission/cable_charging/stay_on_cable
    flag_name: charging.bypass_battery_full_check
    type: bool

  - service_name: /mission/cable_charging/interrupt_recharging_now
    flag_name: charging.interrupt_requested
    type: bool
```

Initial implementation supports only bool intent services.

Use a generic bool service type. The service request sets the named blackboard flag to the requested value. This supports both set and clear:

```text
true  -> set flag
false -> clear flag
```

For `/mission/inspection_demo/trigger_recharge_now` specifically:

- `true` sets `inspection_demo.manual_recharge_requested`
- `false` clears `inspection_demo.manual_recharge_requested` if the tree has not consumed it yet
- once the tree has consumed the flag and exited toward `Reach Cable`, clearing the flag must not undo the already-started mode transition
- when consumed, the tree sets `inspection_demo.recharge_reason = manual_trigger` and `charging.bypass_battery_full_check = true`

For `/mission/cable_charging/stay_on_cable` specifically:

- `true` sets `charging.bypass_battery_full_check`
- `false` clears `charging.bypass_battery_full_check`
- clearing the flag does not directly command a mode transition
- on the next BT tick, `Cable Charging` resumes normal battery-full checks and exits successfully if those checks are satisfied

For `/mission/cable_charging/interrupt_recharging_now` specifically:

- `true` sets `charging.interrupt_requested`
- `false` clears `charging.interrupt_requested` if the tree has not consumed it yet
- once consumed and `Cable Charging` has exited toward `Leave Cable`, clearing the flag must not undo the already-started mode transition

The service callback should not activate modes, publish setpoints, or implement behavior. It only writes the configured global blackboard flag.

To avoid races with behavior tree execution, service callbacks should not mutate
the blackboard directly. Instead, callbacks enqueue intent updates into a
thread-safe pending-intent queue owned by the mission executor:

```text
{flag_name, value, sequence_id, timestamp}
```

The relevant behavior trees include an `ApplyPendingIntentUpdates` node near the
root. This node runs on the BT tick thread, drains the pending-intent queue, and
updates the global blackboard synchronously before downstream condition nodes
read the flags.

Service calls return after the update is enqueued. They do not block until the
next BT tick applies the flag. Internal sequence IDs should be logged and may be
exposed in status tooling for diagnostics.

Intent services are globally registered by the mission executor from mission
specification, not owned by individual tree instances. Even though they are
global service endpoints, each service may declare valid mission modes. If a
service is called outside its valid mode set, the callback should reject the
request with a clear message instead of enqueueing a stale flag update.

Initial valid-mode guidance:

```text
/mission/inspection_demo/trigger_recharge_now:
  valid only while Inspection Demo is active

/mission/cable_charging/stay_on_cable:
  valid only while Cable Charging is active
  also set internally by Inspection Demo when manual recharge is consumed

/mission/cable_charging/interrupt_recharging_now:
  valid only while Cable Charging is active
```

Flag lifecycle and clearing must be enforced in behavior trees, not in the
intent service layer. The service layer only validates, enqueues, and reports
intent update requests.

Tree-enforced lifecycle:

```text
inspection_demo.manual_recharge_requested:
  cleared by Inspection Demo when consumed

charging.bypass_battery_full_check:
  may intentionally survive Inspection Demo -> Reach Cable -> Cable Charging
  cleared by Cable Charging or Leave Cable tree logic

charging.interrupt_requested:
  cleared by Cable Charging when consumed
  cleared on Cable Charging activation to avoid stale interrupts
```

Behavior interpretation and flag clearing must happen inside the behavior trees.

This keeps the mission executor generic and keeps behavior decisions inside the BT layer.

## Mission Specification Integration

The mission specification should define:

- the new `Inspection Demo` mode
- the new tree file for inspection demo
- the repeated recharge chain
- runtime intent services

Conceptually:

```text
Inspection Demo
  success -> Reach Cable

Reach Cable
  success -> Cable Charging

Cable Charging
  success -> Leave Cable

Leave Cable
  success -> Inspection Demo
```

`Inspection Demo` itself should loop phases indefinitely. It returns success only to request recharge.

## Deploy Workflow Integration

The deploy/setup workflow should support `Inspection Demo` as a mission target.
For inspection demo deployment, the workflow should fail early with a clear
setup error if required overview data is missing:

- stored powerline overview exists
- stored pylon overview exists
- pylon overview contains exactly two pylons

The behavior tree should still validate these at runtime, but deploy-time checks
give better operator feedback. Pylon storage remains a separate MCP/GUI/manual
workflow before mission activation.

## Reach Cable Pylon-Aware Waypoint Updates

The flight needed to bring the drone from an inspection demo position to the target conductor should remain inside `Reach Cable`.

Update the existing `PowerlineWaypointProvider` to be pylon-aware while preserving current behavior.

This extended Reach Cable case must be entirely contained inside
`PowerlineWaypointProvider`. The behavior tree structure and FTP waypoint
consumer should not need special pylon-aware branching.

Backward compatibility:

- if no pylon overview is stored, keep current behavior unchanged
- `PowerlineWaypointProvider` receives pylon overview through an optional BT
  input port/blackboard value, similar to stored powerline overview
- if the pylon overview input is absent, empty, or invalid, keep current
  behavior unchanged
- `PowerlineWaypointProvider` should not call pylon overview services directly

If pylon overview exists:

1. Determine whether the drone is inside the corridor using the existing
   `PowerlineWaypointProvider` corridor classification. Do not introduce a
   second corridor definition.
2. Determine whether the drone is within the span between the pylons or beyond
   either pylon by projecting current drone XY onto the pylon span axis.
   Pylon endpoints define the span station limits, with a configurable margin to
   absorb pose noise.
3. If drone is inside corridor and beyond either pylon:
   - first fly directly upward with `FlyToPosition` to above top-cable altitude plus margin
   - then proceed with the above-corridor waypoint path
4. If drone is inside corridor and within pylon span:
   - proceed with current behavior
5. If drone is outside corridor and beyond pylons:
   - proceed with current behavior

This additional pylon-aware logic should only modify the approach path to `Reach Cable`; it should not change cable landing logic.

The vertical escape is added as the first waypoint in the existing
`PowerlineWaypointProvider` queue and consumed by the same FTP waypoint path
already used in `Reach Cable`. Do not introduce CAFTP or a new maneuver type for
this case.

Suggested parameter:

```text
inspection_demo.pylon_span_margin_m = 0.5
```

## BehaviorTree.CPP Design Notes

BehaviorTree.CPP supports this architecture.

Relevant principles:

- Long-running BT actions should be asynchronous and return `RUNNING` while active.
- `halt()` must stop running actions quickly.
- ROS action clients are a good fit because they support nonblocking start, status monitoring, result handling, and cancellation.
- `ReactiveFallback` is the right BT control node to interrupt a running asynchronous child when a higher-priority condition changes.
- BehaviorTree.ROS2 provides wrappers for ROS action clients and service clients, but external service servers still need to be owned by ROS node infrastructure.

For this project:

- keep motion actions as existing haltable BT action nodes
- use `ReactiveFallback` around waypoint execution
- use mission-spec-defined bool intent services to update blackboard flags
- keep behavior interpretation inside tree XML and BT nodes

## Implementation Slices

Recommended order:

1. Add pylon overview interfaces and provider.
2. Add MCP/manual tools to store and query pylon overview.
3. Add mission specification support for bool runtime intent services.
4. Register runtime intent services under mission executor from mission spec.
5. Add BT condition/action nodes for reading, setting, and clearing intent flags.
6. Add `Inspection Demo` mode and empty validation tree.
7. Add `PhaseWaypointProvider`.
8. Add waypoint consumption loop with recharge reactive fallback.
9. Add battery/charging-payload condition node.
10. Add `Cable Charging` BT changes for stay-on-cable and interrupt-recharging flags.
11. Add pylon-aware extension to `PowerlineWaypointProvider`.
12. Add tests for blackboard persistence across `Inspection Demo -> recharge -> Inspection Demo`.
13. Add rendered-sim scenario tests for the full recurring mission.

## Non-Goals For Initial Implementation

- Fully generic inspection planning for arbitrary pylon layouts.
- Perception-driven pylon detection.
- Persistent resume across process restart.
- New setpoint publishers.
- Standalone custom controller for inspection demo.
- Service callbacks that directly activate PX4 modes or mission transitions.

## Acceptance Criteria

- `Inspection Demo` is a mission-owned mode using the existing maneuver mode and BT path.
- It validates powerline overview and pylon overview before running.
- It generates phase waypoints dynamically from stored overview data.
- It executes waypoints using existing FTP maneuver BT action behavior.
- Recharge can interrupt a running FTP action.
- Battery low and manual recharge trigger both cause `Inspection Demo` to terminate successfully.
- Mission spec proceeds through `Reach Cable -> Cable Charging -> Leave Cable`.
- `Inspection Demo` resumes from global blackboard state after `Leave Cable`.
- Runtime intent services are declared in mission specification and generically registered.
- Runtime intent service callbacks only set/clear blackboard bool flags.
- `Cable Charging` supports stay-on-cable and interrupt-recharging behavior through blackboard flags.
- `PowerlineWaypointProvider` remains backward compatible when pylon overview is absent.
