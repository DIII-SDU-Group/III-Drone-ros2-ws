# Simulated Charger/Gripper Implementation Plan

## Purpose

The simulated charger/gripper should model the real integrated gripper and energy harvesting unit closely enough that mission logic, ground control, and operator workflows can be tested without special-case simulation behavior.

The real unit is mounted on top of the drone. It does not close immediately when commanded to close. Instead, a close command arms the mechanism, and the gripper only closes after the powerline conductor pushes into the bottom of the cable guard. Once closed around the conductor, the unit mechanically supports the drone, harvests energy from the conductor, reports charging power, and reports the current drone battery voltage. An open command should immediately open the gripper, release the conductor, and stop charging.

The current simulation shortcut is too weak: in simulation mode, the charger/gripper node maps the latest command directly to gripper status. A close command immediately reports closed, independent of cable contact, without mechanical support, charging behavior, or PX4 battery integration.

## Existing Interfaces

The simulated implementation must preserve the public ROS interface used by the real node.

Real node:

- Source: `src/III-Drone-Core/scripts/payload/charger_gripper_node/charger_gripper_node.py`
- Namespace: `/payload/charger_gripper`
- Service: `/payload/charger_gripper/gripper_command`
- Status topics:
  - `/payload/charger_gripper/gripper_status`
  - `/payload/charger_gripper/battery_voltage`
  - `/payload/charger_gripper/charging_power`
  - `/payload/charger_gripper/charger_status`
  - `/payload/charger_gripper/charger_operating_mode`

Interface definitions:

- `src/III-Drone-Interfaces/srv/GripperCommand.srv`
- `src/III-Drone-Interfaces/msg/GripperStatus.msg`
- `src/III-Drone-Interfaces/msg/ChargerStatus.msg`
- `src/III-Drone-Interfaces/msg/ChargerOperatingMode.msg`

Command semantics:

- `GRIPPER_COMMAND_OPEN = 0`
- `GRIPPER_COMMAND_CLOSE = 1`

Gripper status semantics:

- `GRIPPER_STATUS_OPEN = 0`
- `GRIPPER_STATUS_CLOSED = 1`

Charger status semantics:

- `CHARGER_STATUS_DISABLED = 0`
- `CHARGER_STATUS_CHARGING = 1`
- `CHARGER_STATUS_FULLY_CHARGED = 2`

The simulation must keep these topics, message types, service names, and command/status constants unchanged.

## Target Behavior

### Open Command

When the node receives `GRIPPER_COMMAND_OPEN`:

- The command service returns success immediately.
- The simulated gripper transitions to open immediately.
- Any active Gazebo cable latch is released.
- `gripper_status` publishes `GRIPPER_STATUS_OPEN`.
- `charging_power` drops to `0.0`.
- `charger_status` publishes `CHARGER_STATUS_DISABLED`.
- PX4 battery charging input stops.

### Close Command

When the node receives `GRIPPER_COMMAND_CLOSE`:

- The command service returns success if the command is valid.
- The gripper does not immediately report closed.
- The gripper transitions to an armed-to-close state.
- `gripper_status` continues to publish `GRIPPER_STATUS_OPEN`.
- The gripper closes only when a conductor enters the configured bottom trigger region in the cable guard.

### Cable Triggered Close

When the gripper is armed-to-close and a conductor is detected in the trigger region:

- The simulated gripper transitions to closed/latched.
- `gripper_status` publishes `GRIPPER_STATUS_CLOSED`.
- A Gazebo constraint is created so the drone is supported by the conductor.
- Charging becomes eligible.

### Charging

When the gripper is latched:

- The node publishes nonzero charging power.
- `charger_status` publishes `CHARGER_STATUS_CHARGING`.
- PX4 simulated battery state is charged using the same power value.
- `/payload/charger_gripper/battery_voltage` follows PX4 battery voltage.

When the battery reaches the configured full threshold:

- `charger_status` publishes `CHARGER_STATUS_FULLY_CHARGED`.
- Charging power drops to `0.0` or a configured trickle power.
- PX4 battery state is clamped at full or continues with trickle behavior depending on configuration.

## Architecture

The implementation should be split into three parts:

1. A ROS-facing simulated charger/gripper node.
2. A Gazebo-side cable interaction and latch component.
3. A PX4 battery simulator charge input path.

This split keeps mission and ground-control code unchanged while isolating Gazebo physics and PX4 internals behind narrow simulation-specific interfaces.

## Component 1: Simulated Charger/Gripper ROS Node

### Responsibility

The ROS node owns the high-level charger/gripper state machine and publishes the real charger/gripper interface.

It should either:

- Replace the simulation branch inside `charger_gripper_node.py` with a proper simulation backend, or
- Add a separate simulation executable with the same namespace and public interface.

Recommended first implementation: add a separate simulation executable in `III-Drone-Simulation` or `III-Drone-Core` and launch that for the simulation profile. This avoids adding Gazebo and PX4 simulation assumptions to the real hardware node.

### Public ROS Interface

The simulated node must provide:

- Service server: `/payload/charger_gripper/gripper_command`
- Publishers:
  - `/payload/charger_gripper/gripper_status`
  - `/payload/charger_gripper/battery_voltage`
  - `/payload/charger_gripper/charging_power`
  - `/payload/charger_gripper/charger_status`
  - `/payload/charger_gripper/charger_operating_mode`

It should also subscribe to PX4 battery status:

- Preferred topic: `/fmu/out/battery_status`
- Message type: `px4_msgs/msg/BatteryStatus`

If the exact battery topic differs in the local bridge configuration, make it configurable:

- `/sim/charger_gripper/px4_battery_status_topic`

### Internal Simulation Interface

The simulated node needs a private interface to the Gazebo gripper/cable component.

Recommended topics/services:

- `/sim/charger_gripper/latch_state`
  - Published by Gazebo-side latch component.
  - Contains whether a conductor is in capture range, whether trigger is active, and latched conductor ID.

- `/sim/charger_gripper/latch_command`
  - Published or called by the ROS node.
  - Commands Gazebo side to attach or detach.

Possible message fields for a custom internal message:

```text
builtin_interfaces/Time stamp
bool conductor_in_capture_region
bool conductor_in_trigger_region
bool latched
string conductor_id
geometry_msgs/Point closest_point_world
geometry_msgs/Point closest_point_gripper
float32 trigger_depth_m
```

For the first implementation, this can be two services instead of a custom message:

- `AttachGripperToConductor`
- `DetachGripperFromConductor`

However, a status topic is still useful for debugging and for the state machine to know when cable conditions are satisfied.

### State Machine

States:

- `OPEN`
- `ARMED_TO_CLOSE`
- `LATCH_REQUESTED`
- `LATCHED`
- `CHARGING`
- `FULLY_CHARGED`
- `FAULT`

State behavior:

- `OPEN`
  - `gripper_status = OPEN`
  - `charger_status = DISABLED`
  - `charging_power = 0`
  - no Gazebo latch

- `ARMED_TO_CLOSE`
  - entered by close command
  - `gripper_status = OPEN`
  - waits for `conductor_in_trigger_region == true`
  - if trigger condition persists for configured debounce duration, transitions to `LATCH_REQUESTED`

- `LATCH_REQUESTED`
  - sends attach request to Gazebo component
  - if attach succeeds, transitions to `LATCHED`
  - if attach times out or fails, transitions back to `ARMED_TO_CLOSE` or `FAULT` depending on configured policy

- `LATCHED`
  - `gripper_status = CLOSED`
  - mechanical support active
  - if battery below full threshold, transitions to `CHARGING`
  - if battery already full, transitions to `FULLY_CHARGED`

- `CHARGING`
  - `gripper_status = CLOSED`
  - `charger_status = CHARGING`
  - publishes charging power
  - sends charging power to PX4 battery input
  - transitions to `FULLY_CHARGED` when PX4 battery voltage or remaining percentage reaches threshold

- `FULLY_CHARGED`
  - `gripper_status = CLOSED`
  - `charger_status = FULLY_CHARGED`
  - charging power is `0` or trickle power
  - remains latched until open command

- `FAULT`
  - optional first implementation can avoid this state and log errors instead
  - useful later for stale PX4 battery status, attach failure, invalid conductor, or unexpected detach

Open command behavior from any state:

- issue detach command
- clear close/attach pending state
- transition to `OPEN`
- publish open/disabled/zero-power immediately

### Battery Voltage Publishing

The simulated node should not maintain an independent fake battery voltage if PX4 battery status is available.

Preferred behavior:

- Subscribe to PX4 battery status.
- Use `BatteryStatus.voltage_v` as the source of truth.
- Publish that value on `/payload/charger_gripper/battery_voltage`.

Fallback behavior:

- If no PX4 battery status has been received within a configured timeout:
  - publish the last known voltage if available
  - otherwise publish a configured initial voltage
  - set an internal warning/stale flag
  - optionally keep charging disabled until PX4 battery is available

Config:

- `/sim/charger_gripper/px4_battery_status_timeout_s`
- `/sim/charger_gripper/fallback_battery_voltage_v`
- `/sim/charger_gripper/require_px4_battery_for_charging`

### Charging Power Publishing

Charging power should be computed by the simulated charger/gripper node and reused consistently:

- publish to `/payload/charger_gripper/charging_power`
- send to PX4 battery simulator charge input

Model:

```text
target_power_w = nominal_charging_power_w if latched and not full else 0
power_w = ramp(previous_power_w, target_power_w, ramp_rate, dt)
power_w += gaussian_noise
power_w = clamp(power_w, 0, max_charging_power_w)
```

Config:

- `/sim/charger_gripper/nominal_charging_power_w`
- `/sim/charger_gripper/max_charging_power_w`
- `/sim/charger_gripper/charging_power_noise_std_w`
- `/sim/charger_gripper/charging_ramp_time_s`
- `/sim/charger_gripper/trickle_power_w`

## Component 2: Gazebo Cable Interaction and Latch Model

### Responsibility

Gazebo-side code should answer two questions:

1. Is a conductor physically in the gripper guard trigger region?
2. If the gripper closes, how is the drone mechanically constrained to the conductor?

### Conductor Source

Use existing conductor metadata rather than trying to infer conductors from mesh triangles.

Source:

- `src/III-Drone-Simulation/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml`

This file already defines conductor spans. Those spans should be used as the truth model for geometric gripper interaction.

### Frame Source

The detector needs:

- world-frame conductor segments
- world pose of the gripper/cable guard
- gripper local frame axes

The gripper frame should match the frame already used by TF:

- likely `cable_gripper`
- configured in simulation TF launch and parameters

The Gazebo component can source gripper pose directly from Gazebo model/link pose rather than ROS TF. This is preferable inside Gazebo because it avoids TF timing issues.

Config:

- `/sim/charger_gripper/gazebo_model_name`
- `/sim/charger_gripper/gripper_link_name`
- `/sim/charger_gripper/gripper_frame_id`
- `/sim/charger_gripper/conductor_config_path`

### Geometric Trigger Model

Represent the gripper guard in gripper-local coordinates.

Use two volumes:

- Capture region: conductor is plausibly inside the guard mouth.
- Trigger region: conductor is pressing into the bottom of the guard and should close the gripper if armed.

Recommended first model:

- Capture region: axis-aligned box in gripper frame.
- Trigger region: thinner box or slab near the bottom of the capture region.

For each conductor segment:

1. Transform segment endpoints from world to gripper frame.
2. Compute closest point between segment and capture volume.
3. Check if the segment intersects the capture box.
4. Check if closest/intersection point lies inside trigger region.
5. Compute a trigger metric such as trigger depth.
6. Choose the best conductor by maximum trigger depth or minimum distance to gripper center.

Required trigger condition:

- gripper is armed-to-close
- conductor intersects capture region
- conductor intersects trigger region
- condition persists for `required_contact_duration_ms`

Config:

- `/sim/charger_gripper/capture_min_x_m`
- `/sim/charger_gripper/capture_max_x_m`
- `/sim/charger_gripper/capture_min_y_m`
- `/sim/charger_gripper/capture_max_y_m`
- `/sim/charger_gripper/capture_min_z_m`
- `/sim/charger_gripper/capture_max_z_m`
- `/sim/charger_gripper/trigger_min_x_m`
- `/sim/charger_gripper/trigger_max_x_m`
- `/sim/charger_gripper/trigger_min_y_m`
- `/sim/charger_gripper/trigger_max_y_m`
- `/sim/charger_gripper/trigger_min_z_m`
- `/sim/charger_gripper/trigger_max_z_m`
- `/sim/charger_gripper/required_contact_duration_ms`

### Latch Joint

When the ROS node requests latch, Gazebo should create a constraint at the selected conductor point.

Recommended first implementation:

- Spawn or maintain an invisible static latch anchor model at the closest conductor point.
- Create a joint between the gripper link and latch anchor.
- Use a fixed joint initially.
- If simulation becomes unstable, replace with a stiff 6-DOF joint with compliance/damping.

Joint behavior:

- Latched state should support the full drone weight.
- The drone should not fall through or detach while closed.
- Open command should remove the joint immediately.

Config:

- `/sim/charger_gripper/latch_joint_type`
- `/sim/charger_gripper/latch_joint_stiffness`
- `/sim/charger_gripper/latch_joint_damping`
- `/sim/charger_gripper/latch_anchor_model_name`
- `/sim/charger_gripper/latch_timeout_s`

### Detach Behavior

On detach:

- Remove/destroy the joint.
- Clear selected conductor ID.
- Clear latch anchor.
- Publish latch state as not latched.

Detach must be idempotent:

- If already detached, command should still succeed.

### Failure Conditions

Potential failures:

- Attach requested without a selected conductor.
- Selected conductor no longer intersects capture/trigger region.
- Gazebo cannot create joint.
- Joint disappears unexpectedly.

First implementation:

- Log clearly.
- Return attach failure.
- ROS node returns to `ARMED_TO_CLOSE` unless open command is received.

Later implementation:

- Add `FAULT` state and explicit diagnostics.

## Component 3: PX4 Battery Integration

### Requirement

Charging must increase the simulated PX4 battery charge. It is not enough for the charger/gripper node to publish fake charging power and fake battery voltage. PX4 itself must see increasing battery state through its normal battery pipeline.

### Existing PX4 Battery Simulation

PX4 has an internal SITL battery simulator:

- `PX4-Autopilot/src/modules/simulation/battery_simulator/BatterySimulator.cpp`
- `PX4-Autopilot/src/modules/simulation/battery_simulator/battery_simulator_params.c`

Relevant existing parameters:

- `SIM_BAT_ENABLE`
- `SIM_BAT_DRAIN`
- `SIM_BAT_MIN_PCT`

The existing simulator decreases `_battery_percentage` over time, computes voltage, and publishes `battery_status`.

### Preferred Integration

Extend PX4 battery simulator with an external charge input.

Add a new uORB message, for example:

```text
# msg/SimBatteryCharge.msg
uint64 timestamp
bool charging_enabled
float32 charging_power_w
```

Add matching `px4_msgs/msg/SimBatteryCharge.msg` and bridge it through uXRCE-DDS.

The simulated charger/gripper node publishes `SimBatteryCharge`:

- `charging_enabled = true` while `CHARGING`
- `charging_power_w = computed charging power`
- disabled/zero otherwise

Modify `BatterySimulator.cpp`:

- subscribe to `sim_battery_charge`
- track last charge input timestamp
- if input is fresh, include charge energy in battery percentage integration
- if stale, treat charge power as zero

Integration model:

```text
dt_s = (now - last_integration) / 1e6
discharge_energy_wh = configured_discharge_power_w * dt_s / 3600
charge_energy_wh = charging_power_w * charging_efficiency * dt_s / 3600
net_energy_wh = charge_energy_wh - discharge_energy_wh
battery_percentage += net_energy_wh / battery_capacity_wh
battery_percentage = clamp(battery_percentage, sim_bat_min_pct, 1.0)
```

PX4 should continue to publish normal `battery_status`. The charger/gripper node then reads PX4 battery status and republishes voltage on its own interface.

### PX4 Parameters to Add

Add to `battery_simulator_params.c`:

- `SIM_BAT_CAP_WH`
  - battery capacity used for charge/discharge integration
- `SIM_BAT_CHG_EFF`
  - charging efficiency, default around `0.85` to `1.0`
- `SIM_BAT_CHG_MAX_W`
  - max accepted charging power
- `SIM_BAT_CHG_TIMEOUT`
  - timeout after which external charge input is considered stale

Potentially replace `SIM_BAT_DRAIN` with a clearer energy model later, but avoid broad PX4 refactors in the first implementation.

### Alternative Short-Term Integration

A temporary proof-of-concept could use PX4 parameters to manipulate battery percentage indirectly, for example by changing `SIM_BAT_MIN_PCT`. This is not recommended for the final implementation because:

- it does not model energy
- it is hard to reason about
- it can mask real battery threshold behavior
- it overloads a parameter intended as a minimum clamp

Use this only if the project needs a quick demonstration before modifying PX4.

## Data Flow

Normal unlatched flight:

```text
PX4 battery_simulator -> battery_status -> ROS bridge -> sim_charger_gripper_node
sim_charger_gripper_node -> /payload/charger_gripper/battery_voltage
sim_charger_gripper_node -> charging_power = 0
sim_charger_gripper_node -> charger_status = DISABLED
```

Close command before cable contact:

```text
mission/gc -> /payload/charger_gripper/gripper_command(close)
sim_charger_gripper_node -> state = ARMED_TO_CLOSE
sim_charger_gripper_node -> gripper_status = OPEN
Gazebo latch detector -> conductor_in_trigger_region false/true
```

Cable-triggered latch:

```text
Gazebo latch detector -> conductor_in_trigger_region true
sim_charger_gripper_node -> attach request
Gazebo latch component -> create joint
Gazebo latch component -> latched true
sim_charger_gripper_node -> gripper_status = CLOSED
```

Charging:

```text
sim_charger_gripper_node -> charging_power topic
sim_charger_gripper_node -> px4 sim battery charge input
PX4 battery_simulator -> integrates charge
PX4 battery_simulator -> battery_status voltage/remaining increases
sim_charger_gripper_node -> republishes PX4 voltage
```

Open/release:

```text
mission/gc -> gripper_command(open)
sim_charger_gripper_node -> detach request
Gazebo latch component -> remove joint
sim_charger_gripper_node -> gripper_status = OPEN
sim_charger_gripper_node -> charge input = disabled/0 W
```

## Configuration Additions

Add simulation-specific parameters under `/sim/charger_gripper`.

Core:

```yaml
/sim/charger_gripper/enabled: true
/sim/charger_gripper/status_publish_rate_hz: 50.0
/sim/charger_gripper/px4_battery_status_topic: /fmu/out/battery_status
/sim/charger_gripper/px4_battery_status_timeout_s: 1.0
/sim/charger_gripper/require_px4_battery_for_charging: true
/sim/charger_gripper/fallback_battery_voltage_v: 24.0
```

Geometry:

```yaml
/sim/charger_gripper/gripper_frame_id: cable_gripper
/sim/charger_gripper/gazebo_model_name: d4s_dc_drone
/sim/charger_gripper/gripper_link_name: cable_gripper
/sim/charger_gripper/conductor_config_path: <installed conductors.yaml>
/sim/charger_gripper/capture_min_x_m: -0.10
/sim/charger_gripper/capture_max_x_m: 0.10
/sim/charger_gripper/capture_min_y_m: -0.15
/sim/charger_gripper/capture_max_y_m: 0.15
/sim/charger_gripper/capture_min_z_m: -0.20
/sim/charger_gripper/capture_max_z_m: 0.05
/sim/charger_gripper/trigger_min_x_m: -0.08
/sim/charger_gripper/trigger_max_x_m: 0.08
/sim/charger_gripper/trigger_min_y_m: -0.12
/sim/charger_gripper/trigger_max_y_m: 0.12
/sim/charger_gripper/trigger_min_z_m: -0.20
/sim/charger_gripper/trigger_max_z_m: -0.12
/sim/charger_gripper/required_contact_duration_ms: 100
```

Latch:

```yaml
/sim/charger_gripper/latch_joint_type: fixed
/sim/charger_gripper/latch_joint_stiffness: 100000.0
/sim/charger_gripper/latch_joint_damping: 1000.0
/sim/charger_gripper/latch_timeout_s: 1.0
/sim/charger_gripper/latch_anchor_model_name: charger_gripper_latch_anchor
```

Charging:

```yaml
/sim/charger_gripper/nominal_charging_power_w: 150.0
/sim/charger_gripper/max_charging_power_w: 250.0
/sim/charger_gripper/charging_power_noise_std_w: 5.0
/sim/charger_gripper/charging_ramp_time_s: 1.0
/sim/charger_gripper/trickle_power_w: 0.0
/sim/charger_gripper/fully_charged_voltage_v: 25.2
/sim/charger_gripper/fully_charged_remaining_pct: 0.98
```

PX4 battery simulator:

```yaml
SIM_BAT_CAP_WH: 130.0
SIM_BAT_CHG_EFF: 0.9
SIM_BAT_CHG_MAX_W: 250.0
SIM_BAT_CHG_TIMEOUT: 1.0
```

The exact numeric defaults must be calibrated against the real drone battery and charger.

## Repository-Level Changes

### `III-Drone-Simulation`

Add:

- simulated charger/gripper ROS node or launch wrapper
- Gazebo latch/interaction plugin
- tests for conductor intersection geometry
- launch integration in sim profile

Likely files:

- `src/III-Drone-Simulation/iii_drone_simulation/sim_charger_gripper_node.py`
- `src/III-Drone-Simulation/src/gazebo/sim_charger_gripper_system.cpp`
- `src/III-Drone-Simulation/launch/...`
- `src/III-Drone-Simulation/test/...`

### `III-Drone-Core`

Prefer not to modify the real hardware node unless there is a clear benefit.

Possible change:

- Factor common message publishing/state constants into a reusable helper if duplication becomes significant.

### `III-Drone-Configuration`

Add parameters:

- `/sim/charger_gripper/...`

Update:

- parameter schema/manifest
- sim tracked default parameter set
- tests for new parameter schema entries

### `III-Drone-Interfaces`

May need internal sim messages/services:

- `SimChargerGripperLatchState.msg`
- `SimChargerGripperLatchCommand.srv`

If keeping internal Gazebo/ROS communication in standard messages/services, this may not be needed.

### `PX4-Autopilot`

Modify:

- battery simulator module
- add uORB message
- bridge configuration if required
- generated message plumbing

### `px4_msgs`

Add matching ROS 2 message for new uORB charge input if using uXRCE-DDS.

## Implementation Phases

### Phase 1: ROS Simulation State Machine

Implement the simulated charger/gripper node without Gazebo latch physics.

Deliverables:

- close command arms state but does not report closed
- open command reports open immediately
- status topics publish continuously
- PX4 battery status is subscribed and republished as charger/gripper battery voltage
- charging remains disabled unless a temporary debug latch flag is set

Tests:

- service call close leaves `gripper_status = OPEN`
- service call open leaves `gripper_status = OPEN`
- battery voltage topic follows injected/subscribed PX4 battery status

### Phase 2: Conductor Geometry Trigger

Add deterministic conductor trigger detection.

Deliverables:

- load `conductors.yaml`
- transform conductor spans into gripper frame
- detect capture/trigger region intersection
- publish debug latch state
- close command plus trigger condition transitions to closed

Tests:

- no trigger outside corridor
- trigger when gripper bottom region overlaps conductor
- no close if conductor is only nearby but not in trigger volume
- close only after required debounce duration

### Phase 3: Gazebo Mechanical Latch

Add attach/detach behavior.

Deliverables:

- Gazebo component creates latch anchor
- Gazebo component creates joint on latch
- open command removes joint
- drone can hang from conductor

Tests:

- latched drone remains supported with low thrust
- open releases drone
- repeated open is safe
- repeated close while latched is safe

### Phase 4: Charging Telemetry

Add charger power model.

Deliverables:

- charging power ramps up when latched
- charger status transitions through disabled/charging/fully charged
- operating mode publishes a sensible simulated value

Tests:

- unlatched charging power is zero
- latched charging power is above mission threshold
- fully charged condition stops or reduces charging power

### Phase 5: PX4 Battery Charge Input

Extend PX4 battery simulator.

Deliverables:

- new uORB/ROS message for simulated battery charge
- PX4 battery simulator consumes charge power
- PX4 battery voltage/remaining increase while charging
- charger/gripper node republishes PX4 voltage

Tests:

- with charging disabled, PX4 battery drains normally
- with charging enabled, PX4 battery drain slows or reverses
- at sufficient charge power, PX4 battery voltage increases
- stale charge input is ignored

### Phase 6: Integration and Mission Validation

Run the full system through mission-relevant scenarios.

Scenarios:

- boot sim profile
- approach conductor
- command close early
- verify gripper remains open before cable trigger
- make contact with bottom guard
- verify latch and closed status
- verify drone hangs from conductor
- verify charging starts
- verify PX4 battery increases
- command open
- verify release and charging stops

## Risks and Mitigations

### Gazebo Joint Stability

Risk:

- A fixed joint from a dynamic drone to a static anchor can cause solver instability.

Mitigation:

- Start with fixed joint for simplicity.
- If unstable, switch to a damped 6-DOF joint or compliant joint.
- Keep latch anchor at the closest conductor point and avoid moving it after latch.

### PX4 Message Plumbing Cost

Risk:

- Adding a new uORB and `px4_msgs` message touches multiple repos and build steps.

Mitigation:

- Implement ROS-only and Gazebo latch phases first.
- Add PX4 battery input as a separate focused change.
- Keep the charge input message minimal.

### Battery Model Accuracy

Risk:

- Voltage/SOC behavior may not match real battery under load.

Mitigation:

- First model only needs monotonic charge/drain and threshold behavior.
- Add capacity/efficiency parameters.
- Calibrate from logs later.

### Frame Alignment

Risk:

- Gripper-local trigger volume can be wrong if `cable_gripper` frame does not match visual geometry.

Mitigation:

- Add RViz/Gazebo debug markers for capture and trigger volumes.
- Make all bounds configurable.
- Validate with visual gripper model and conductor positions.

### Real Node Divergence

Risk:

- Separate simulated node can drift from real node behavior.

Mitigation:

- Preserve exact public interface.
- Add interface-level tests.
- Keep real hardware code untouched unless shared code becomes clearly beneficial.

## Debug and Visualization

Add debug outputs:

- Marker for capture volume
- Marker for trigger volume
- Marker for closest conductor point
- Latched conductor ID
- Current gripper state
- Current PX4 battery voltage
- Current charging power

Recommended topics:

- `/sim/charger_gripper/debug_markers`
- `/sim/charger_gripper/state`

The debug topics should be optional and controlled by config.

## Acceptance Criteria

The implementation is complete when:

- Close command does not report closed until cable trigger occurs.
- Open command immediately reports open and releases the drone.
- The drone can be mechanically supported by the conductor while latched.
- Charging power is nonzero only while latched and charging.
- Charger/gripper battery voltage matches PX4 battery voltage.
- PX4 battery voltage or remaining percentage increases when latched and charging.
- Existing mission and GC code do not require interface changes.
- The sim profile launches the simulated charger/gripper implementation by default.

