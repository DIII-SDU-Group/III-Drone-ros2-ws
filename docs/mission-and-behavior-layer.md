# Mission And Behavior Layer

## 1. Mission Package Role

`iii_drone_mission` is the high-level autonomy coordinator:
- wraps behavior trees
- links behavior decisions to maneuver action servers
- manages PX4 mode registration/execution
- provides powerline overview access services

## 2. Main Executables

1. `mission_executor` (lifecycle node)
- Configures TF buffer, mission spec, tree provider, maneuver reference client.
- Starts/stops mission execution and mode integration in lifecycle transitions.
- Exposes `get_mission_catalog` and `select_mission_catalog_entry` services.

2. `powerline_overview_provider` (lifecycle node)
- Subscribes to mapped powerline data.
- Serves/updates stored powerline overview.

## 3. Internal Mission Flow

Runtime flow implemented by `MissionExecutorNode` + `MissionExecutor`:
1. `mission_executor` loads and verifies the installed `iii_drone_mission` catalog through the ament resource index. Runtime APIs accept catalog IDs only; source paths and environment fallbacks are not supported.
2. `MissionSpecification` resolves the selected catalog entry and its content-addressed YAML/XML assets into:
- `executor_owned_mode` (the key of the mode owned by `GenericModeExecutor`)
- `entries` map (each entry contains `key`, `mode_name`, `behavior_tree_xml_file`, optional `next_mode`, optional `allow_activate_when_disarmed`).
3. `MissionExecutor` creates a `TreeProvider` and one `TreeExecutor` per mission entry key.
4. `ModeProvider` creates one `ManeuverMode` per mission entry key (same keys as tree executors).
5. `MissionExecutor` creates `GenericModeExecutor` with the owned mode from `executor_owned_mode`.
6. On `MissionExecutor::Start()`:
- `GenericModeExecutor::doRegister()` registers executor control with PX4.
- `ModeProvider::Register()` registers each `ManeuverMode` and associates each mode with its corresponding `TreeExecutor`.
- `ManeuverMode::Register()` also calls `/control/maneuver_controller/register_offboard_mode` so maneuver controller can map PX4 mode IDs to your internal mode handlers.
7. During flight:
- Activating a mode triggers `ManeuverMode::onActivate()`.
- `onActivate()` starts BT execution with `TreeExecutor::StartExecution()` using the XML from mission spec entry.
- `GenericModeExecutor` advances between modes using `next_mode` from mission spec, and can also inject action-based transitions (arm/takeoff/land/disarm).

## 4. Behavior Tree Execution Model

`TreeExecutor` characteristics:
- Creates tree from XML at runtime.
- Registers custom action/condition nodes into BT factory.
- Ticks with configurable `tick_period_ms`.
- Supports asynchronous ROS action/service wrappers via BehaviorTree.ROS2.

Registered BT nodes include command/decision primitives such as:
- maneuver actions (`FlyToPosition`, `FlyToObject`, `CableLanding`, `CableTakeoff`, `Hover*`)
- payload commands (`GripperCommand`)
- perception/state checks (`VerifyPowerlineDetected`, `SelectTargetLine`, `StoreCurrentState`)
- mission utility nodes (`PowerlineWaypointProvider`, `Update/GetPowerlineOverview`, `ModeExecutorAction`, `LogMessage`)

## 5. Mission Specification

Mission specifications are registered with `iii_register_mission(...)` in the
package CMake configuration. The build produces deterministic local, qualified,
and explicit field-candidate catalogs. Production releases install the qualified
catalog; local development catalogs retain classified test and legacy entries.

Defines:
- `executor_owned_mode`
- mode entries (`key`, display name, BT file, activation constraints, next mode)

The catalog entry is the bridge between operational mode sequencing and concrete
BT XML assets. Each entry and asset is content-addressed and verified before use.

Important behavior:
- `executor_owned_mode` must match an existing `entries[].key`.
- `next_mode` values are mode-entry keys (not display names).
- `behavior_tree_xml_file` is a package-relative logical asset name. Absolute paths, traversal, environment expansion, missing assets, unknown nodes, and node-port mismatches fail catalog generation.
- Experimental entries require explicit field-artifact inclusion and emit a prominent runtime warning. They never enter qualified catalogs.
- Runtime selection is a session-scoped transactional override; `--default` restores the profile default and a cold restart restores it automatically.

## 6. Behavior Tree Assets

Multiple XML trees exist, including:
- takeoff tests
- up/down test cycles
- cable landing/takeoff workflows
- on-cable/leave-cable workflows
- Groot model definitions

The trees encode robust patterns:
- retries
- fallback sequences
- staged approach/failback behavior
- mapper mode changes (start/pause/freeze/stop)
- gripper state handling

### Leaving the cable

`leave_cable_tree.xml` arms the vehicle while the gripper still holds the
conductor, then pushes it up against the conductor before opening the gripper:

1. `HoverOnCable` with `cable_release_push="true"` commands the vertical axis as
   an upward acceleration, `/behavior/cable_release_push_acceleration`
   (default 3.0 m/s^2). PX4 turns an acceleration setpoint into thrust = hover
   thrust x (1 + a/g), so the push force is set by that value. A velocity
   setpoint would not bound it: the conductor stops the vehicle, the velocity
   error never closes, and PX4's integrator winds thrust up to its maximum. The
   push requests PX4 takeoff with a small acceleration
   (`cable_push_takeoff_request_acceleration`) and ramps to the target at
   `cable_push_jerk` only while PX4 applies it: its land detector reports the
   vehicle airborne and its position controller commands thrust
   (`/fmu/out/vehicle_local_position_setpoint`). Far above the ground PX4
   reports airborne as soon as it arms, while its spool-up still commands zero
   thrust, so the land detector alone is not enough. The sustained action
   succeeds when the push is established (Core logs the hover thrust PX4
   assumes), and the push then continues until CableTakeoff takes over. It
   fails if PX4 does not apply it within `cable_push_start_timeout_s` or stops
   applying it.
2. `WaitForPX4Airborne` requires PX4's land detector to report not landed, not
   maybe landed and no ground contact, and PX4 to command thrust, without
   interruption for 1 s (samples older than 2.5 s and 1 s count as unknown).
3. Only then does `GripperCommand` open the gripper, and `CableTakeoff` starts.

If step 1 or 2 fails, the gripper stays closed, the vehicle is force-disarmed on
the conductor and Leave Cable fails.

PX4 cannot declare the vehicle landed during the push: from takeoff it needs
thrust near idle plus a commanded descent, and the push commands neither. The
push force relies on PX4's `MPC_THR_HOVER`, which PX4 also restores on every
disarm: a hover thrust 10% low still leaves an 18% push at the default. Tune
`MPC_THR_HOVER` on the aircraft (for example from the hover-thrust estimate of
a flight log) before flying cable releases.

Every SIM and HIL endurance run judges each release from the PX4 flight logs
(`scripts/workspace/cable_release_ulog.py`, `cable_release_report.json`): land
detector clear until CableTakeoff, push thrust above hover and below
saturation, no vertical motion while pressed, and no CableTakeoff sag.

## 7. PX4 Integration Positioning

Mission package uses `px4_ros2_cpp` and custom mode executor wrappers.
It is not only consuming telemetry; it attempts to own/drive mode behavior through explicit mode registration and action requests (arm/disarm/takeoff/land patterns in BT nodes).

## 8. Catalog Operations

The operator surface is the III CLI:

```bash
iii mission status
iii mission list
iii mission list --all
iii mission show inspection-production
iii mission select inspection-production
iii mission select --default
```

Read commands return catalog IDs, hashes, classification, profile compatibility,
dependency identities, default/override state, and readiness without target
filesystem paths. Selection is a retained runtime mutation and is accepted only
when mission/custom-operation state is fresh and idle; real and opti-track
profiles additionally require fresh PX4 state, a disarmed landed vehicle, and a
maintenance-safe navigation mode.

`iii system boot` performs a simulation-only source/install preflight. It verifies
the installed local catalog and its source-state attestation, runs an incremental
`iii_drone_mission` build when drift is detected, and refuses to launch if the
rebuilt catalog remains stale or invalid.

Qualified ARM64 builds replace the local catalog with the production-only
qualified reduction and remove all local/field variants. The shared GC/drone
release manifest and each component bundle manifest bind the same logical catalog
hash; the release manifest also binds the exact `catalog.json` and source-state
hashes. Field catalogs may include onboard-compatible experimental IDs only via
explicit materialization and retain the experimental warning in catalog and
runtime state.
