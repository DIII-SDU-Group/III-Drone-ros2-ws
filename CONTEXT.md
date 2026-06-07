# III-Drone Operations

This context defines language for operator and agent-facing runtime control of the III-Drone system.

## Language

**Agent Operations Tooling**:
A tool layer that lets an agent execute, observe, diagnose, and report on III-Drone runtime workflows through canonical operator and supervision interfaces.
_Avoid_: Agent orchestrated testing

**Operations Controller**:
A runtime control component that executes bounded operator or agent commands outside full mission sequencing while preserving PX4/offboard safety and III supervision semantics.
_Avoid_: Bypass controller

**Direct Operation**:
An operator or agent command executed outside mission behavior-tree sequencing while still using the normal control, perception, payload, and supervision components.
_Avoid_: Bypass

**Custom Operation Mode**:
A standalone PX4 custom mode that hosts operator or agent-directed direct operations outside the mission executor.
_Avoid_: Offboard, mission mode

**Mission-Owned Mode**:
A PX4/custom mode owned by the mission executor for behavior-tree-driven autonomy.
_Avoid_: Offboard mode

**Reference Owner**:
The single runtime component currently allowed to publish control references toward PX4.
_Avoid_: Setpoint source

**Operations Interface**:
The shared command and inspection surface exposed by the Operations Controller for GUI and agent tooling.
_Avoid_: GUI API, MCP API

**Workflow Catalog**:
The finite set of operator and agent workflows that define v1 tooling completeness.
_Avoid_: Everything

**Maneuver Execution System**:
The lower-level control system that owns maneuver action execution, reference generation, scheduling, and maneuver status.
_Avoid_: Mission system

**Simulation Control**:
Agent-facing control of simulation state, viewpoints, and image snapshots for inspection workflows.
_Avoid_: Gazebo helper

**PX4 Command Operation**:
A direct operation equivalent of common QGroundControl commands such as arm, set mode, and land.
_Avoid_: Virtual joystick

**PX4 Command Telemetry**:
MAVLink/MAVSDK-derived PX4 status such as current mode, arm state, and flight state, independent of ROS topic availability.
_Avoid_: ROS vehicle status

**Cable-Aware Flight**:
A direct or mission operation that plans motion around known cable geometry to avoid collisions.
_Avoid_: Fly to position

**Maneuver Idle**:
The state where the maneuver controller has no current maneuver and no queued maneuver.
_Avoid_: Ready

**Current Maneuver Idle**:
The state where the maneuver controller has no currently executing maneuver, regardless of queued maneuvers.
_Avoid_: Queue empty

**Mission Execution**:
Autonomy driven by mission specifications, PX4 mission modes, and behavior trees through the mission executor.
_Avoid_: Full stack, normal path

**Mission Sequence**:
The top-level mission run owned by the mission executor's mode executor.
_Avoid_: Individual mode transition

## Relationships

- **Agent Operations Tooling** exposes **Direct Operation** and **Mission Execution** capabilities to agents.
- An **Operations Controller** owns **Direct Operation** commands.
- **Mission Execution** remains owned by the mission executor.
- **Direct Operation** may reuse the same maneuver, perception, payload, and logging components used by **Mission Execution**.
- **Mission Execution** and **Direct Operation** both use the **Maneuver Execution System** for maneuver action execution.
- The **Operations Controller** does not implement its own maneuver scheduler.
- The **Operations Controller** owns its own maneuver reference client instance.
- Mission and custom-operation activation clear queued maneuver work before taking control.
- Custom-operation activation and deactivation clear queued maneuver work.
- **Mission Sequence** activation and deactivation clear queued maneuver work.
- The **Operations Controller** accepts new **Direct Operation** maneuver goals only when the **Maneuver Execution System** is **Current Maneuver Idle**.
- Clearing queued maneuver work is an atomic service operation on the **Maneuver Execution System** and does not cancel the currently executing maneuver.
- The **Operations Controller** owns direct control only while **Custom Operation Mode** is active.
- A **Mission-Owned Mode** transfers control to **Mission Execution**.
- At most one **Reference Owner** may publish control references toward PX4 at a time.
- PX4 mode activation is the authority that determines the active **Reference Owner**.
- Leaving **Custom Operation Mode** interrupts active **Direct Operation** commands.
- Entering **Custom Operation Mode** while **Mission Execution** is active transfers control to the **Operations Controller**, which holds a fixed hover reference until given a **Direct Operation** command.
- The **Operations Interface** exposes primitive **Direct Operation** commands and runtime inspection for both GUI and agent tooling.
- The **Operations Interface** does not sequence mission-like workflows.
- The **Operations Interface** is ROS-native; GUI and MCP tooling are clients of that interface.
- The **Operations Interface** uses typed ROS actions and services from III-Drone-Interfaces rather than generic string commands.
- GUI and MCP tooling import the **Operations Interface** client wrapper from the mission package.
- The mission package exposes the client wrapper as `iii_drone_mission.operations_client`.
- MCP tooling for **Agent Operations Tooling** runs inside the ROS runtime environment for the first implementation.
- The v1 **Workflow Catalog** includes runtime bringup, custom operation, mission running, perception control, payload control, data inspection, diagnosis, GUI operations/diagnostics, MCP tooling, agent-only visual inspection, and all system maneuver actions.
- Agent-only visual inspection captures RViz, plots, or data images as files for agent analysis; this is not part of the GUI path.
- The v1 **Workflow Catalog** includes **Simulation Control** for Gazebo camera perspective and image snapshots.
- **Simulation Control** belongs in the simulation package and is composed into agent tooling separately from mission operations.
- The v1 **Workflow Catalog** includes **PX4 Command Operation** for arming, mode selection, and landing.
- Agent tooling does not use manual virtual joystick movement; position changes use direct operation maneuvers.
- **PX4 Command Operation** is separate from **Custom Operation Mode**.
- QGroundControl-equivalent command and telemetry tooling uses MAVLink/MAVSDK.
- **PX4 Command Telemetry** is available independently of ROS.
- Standard PX4 commands and status use MAVSDK; activation of px4_ros2 custom modes may use ROS/px4_ros2 when MAVSDK cannot select them reliably.
- **Cable-Aware Flight** requires active perception and known cable geometry.
- **Cable-Aware Flight** is a Core maneuver and trajectory-generator capability, not an Operations tooling feature.
- Long-running **Direct Operation** commands use ROS action feedback and cancellation for status and abort behavior.
- **Direct Operation** action names live under the Operations Controller namespace while reusing the same action types as the underlying system action.
- The **Operations Controller** accepts at most one active **Direct Operation** command at a time.
- Cancelling a **Direct Operation** cancels the forwarded maneuver action and returns **Custom Operation Mode** to fixed hover.

## Example dialogue

> **Dev:** "Should the agent fly to a test pose by launching a mission?"
> **Domain expert:** "No, that is a **Direct Operation**. The **Operations Controller** should hold offboard control and call the maneuver action directly."

> **Dev:** "Can the **Operations Controller** run inside mission-owned PX4 modes?"
> **Domain expert:** "No. It only owns control in **Custom Operation Mode**; activating a **Mission-Owned Mode** gives control to **Mission Execution**."

> **Dev:** "Can mission and direct operation both publish PX4 setpoints while the operator decides?"
> **Domain expert:** "No. There is exactly one **Reference Owner** at a time."

## Flagged ambiguities

- "bypass" was used to mean executing outside mission behavior-tree sequencing, not bypassing control safety; resolved term: **Direct Operation**.
- "offboard" was considered as the direct-operation mode, then rejected in favor of a standalone **Custom Operation Mode**.
