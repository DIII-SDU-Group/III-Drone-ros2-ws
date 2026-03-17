# Interfaces Reference

Custom interfaces are defined in `iii_drone_interfaces` and form the contract across core/mission/supervision/GC.

## 1. Action Interfaces

Maneuver and mode actions:
- `FlyToPosition`
- `FlyToObject`
- `Hover`
- `HoverByObject`
- `HoverOnCable`
- `CableLanding`
- `CableTakeoff`
- `ModeExecutorAction`

Supervisor actions:
- `SupervisorStart`
- `SupervisorStop`
- `SupervisorRestart`
- `SupervisorShutdown`

Characteristics:
- Maneuver actions generally return success plus feedback containing path/pose/target-distance style telemetry.
- Supervisor actions support selected-node operations and dependency bypass flags.

## 2. Message Interfaces

Core state/control abstraction messages:
- `State`, `Reference`, `ReferenceTrajectory`
- `Target`, `CombinedDroneAwareness`
- `Maneuver`, `ManeuverQueue`

Perception/environment messages:
- `SingleLine`, `ProjectionPlane`, `Powerline`
- `PLMapperCommand` (command payload message)

Payload/aux messages:
- `GripperStatus`, `ChargerStatus`, `ChargerOperatingMode`
- `StringStamped`, `TrajectoryMode`, `TrajectoryComputeTime`

Most central message semantically:
- `CombinedDroneAwareness` (system-level fused awareness consumed by mission/GC).

## 3. Service Interfaces

### 3.1 Configuration Services
- `DeclareParameters`, `UndeclareParameters`
- `GetParameterYaml`, `GetDeclaredParameters`
- `SaveParameters`, `LoadParameters`, `GetParameterFiles`
- `SetParameterFromGC`
- `GetCurrentParameterFile`, `SetCurrentParameterFileAsDefault`

### 3.2 Control/Mission Services
- `ComputeReferenceTrajectory`
- `GetReference`
- `PLMapperCommand`
- `SystemCommand`
- `UpdatePowerlineOverview`, `GetPowerlineOverview`
- `StoreCurrentState`, `LoadStoredState`
- `WriteBehaviorTreeModelXML`
- `RegisterOffboardMode`

### 3.3 Payload/Supervision Services
- `GripperCommand`
- `InitiateCharging`, `InterruptCharging`, `ProlongCharging`
- `GetManagedNodes`

## 4. Design Notes

- Interface package includes both low-level control primitives and higher-level orchestration APIs.
- Services/actions encode many operational constants/enums directly in interface definitions.
- Interface stability is critical: many packages are tightly coupled to these exact field names and enum values.

## 5. Integration Implication

For any future refactoring:
- Treat `iii_drone_interfaces` as a public API boundary.
- Breaking changes require coordinated updates across core, mission, supervision, and GC simultaneously.
