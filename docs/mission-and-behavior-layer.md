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
- Exposes `write_behavior_tree_model_xml` service.

2. `powerline_overview_provider` (lifecycle node)
- Subscribes to mapped powerline data.
- Serves/updates stored powerline overview.

## 3. Internal Mission Flow

Runtime flow implemented by `MissionExecutorNode` + `MissionExecutor`:
1. `mission_executor` lifecycle node configures `Configurator` and reads parameter `mission_specification_file`.
2. `MissionSpecification` parses that YAML into:
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

Primary file:
- `mission_specification/mission_specification.yaml`

Defines:
- `executor_owned_mode`
- mode entries (`key`, display name, BT file, activation constraints, next mode)

This is the bridge between operational mode sequencing and concrete BT XML files.

Important behavior:
- `executor_owned_mode` must match an existing `entries[].key`.
- `next_mode` values are mode-entry keys (not display names).
- `behavior_tree_xml_file` is expanded with shell-style expansion (`wordexp`), so environment variables like `$BEHAVIOR_TREES_DIR/...` are valid.

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

## 7. PX4 Integration Positioning

Mission package uses `px4_ros2_cpp` and custom mode executor wrappers.
It is not only consuming telemetry; it attempts to own/drive mode behavior through explicit mode registration and action requests (arm/disarm/takeoff/land patterns in BT nodes).
