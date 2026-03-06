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

Core path inside mission executor:
1. Read mission specification YAML.
2. Build `TreeProvider` and behavior tree factory context.
3. Configure maneuver reference client (consumes control references/odometry).
4. Build/register `ModeProvider` and `GenericModeExecutor`.
5. Register external/offboard mode with PX4 side (`RegisterOffboardMode` service path).
6. Run behavior trees associated with active mode.

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
- ordered mode entries (`key`, display name, BT file, activation constraints, next mode)

This is the bridge between operational mode sequencing and concrete BT XML files.

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
