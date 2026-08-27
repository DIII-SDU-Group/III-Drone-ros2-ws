# Simulation And PX4 Integration

## 1. Simulation Package Role

`iii_drone_simulation` contains:
- launch-time simulation sensor and tf integration
- Gazebo/PX4 asset install tooling
- depth-camera to mmWave pointcloud transformation node

## 2. Runtime Simulation Flow

In simulation mode (`SIMULATION=true`):
1. The simulation helper starts Gazebo/PX4 SITL outside III supervision.
2. PX4 SITL is treated like the physical PX4 flight controller becoming available.
3. The III daemon starts `micro_ros_agent` as a daemon-managed service during system bringup.
4. `iii_drone_simulation/sim_assets.launch.py` provides simulated Gazebo asset ingress through the supervised system graph.
5. `ros_gz_bridge` bridges simulated camera and depth point cloud topics.
6. `depth_cam_to_mmwave` converts incoming depth cloud to mmWave-like output topic (`/sensor/mmwave/points`).
7. `tf_sim.launch.py` publishes sim-specific static transforms and dynamic drone frame updates.

QGroundControl runs only as the pinned host-native application owned by `iii qgc`.
The devcontainer uses host networking, so PX4 SITL emits MAVLink to the host UDP
14550 endpoint used by the same QGroundControl binary as real operation. The
simulation launcher owns only PX4/Gazebo and never starts, stops, or embeds QGC.
Connecting or disconnecting QGC affects PX4/operator telemetry, not III lifecycle
bringup.

Start the two independent surfaces explicitly:

```bash
iii qgc start --dry-run
iii qgc start --operation-id <retained-operation-id> --confirm
tools/simulation/launch_simulation_tools.sh --headless --no-attach
tools/simulation/launch_simulation_tools.sh --status
```

The status output reports whether host UDP 14550 has a listener, but it never
starts or stops that listener. `iii qgc stop` and the simulation helper's
`--stop` remain independent. Host-network transport is part of the devcontainer
contract; Docker bridge/NAT port inference is not supported for this flow.

QGC forwards a second loopback-only MAVLink stream to UDP 14551. The login-scoped
`iii-gc-px4-parameters.service` uses that stream to mirror complete, disarmed PX4
inventories. It debounces observed parameter events for two seconds, reconciles
the full set every 60 seconds while connected and disarmed, and reconciles once
more at a clean session end. It never requests a bulk transfer or writes while
armed. Captures from direct QGC edits are therefore attributed only to a MAVLink
observation; the companion does not invent an operator or transaction identity.

Both real and simulation use the versioned `iii.px4-parameter-manifest/v1`
contract. The release owns complete profile-specific manifests and binds their
content identities to the exact PX4 firmware commit. Inspect and manage them with
the read/plan/confirm sequence below:

```bash
iii px4 params pull --profile sim --json
iii px4 params plan --profile sim --snapshot <snapshot-id> --key <parameter> --json
iii px4 params apply --plan-id <plan-id> --key <parameter> --dry-run --json
iii px4 params apply --plan-id <plan-id> --key <parameter> \
  --operation-id <retained-operation-id> --confirm --json
iii px4 params verify --plan-id <plan-id> --json
```

Every write begins from a fresh complete backup, requires exact per-key
confirmation, verifies readback, and attempts byte-equivalent parameter recovery
on failure. Pull, activation validation, and ordinary field deployment are
read-only. Real activation sends the complete disarmed inventory as authenticated,
release-bound receiver evidence; required drift rejects activation without a
`PARAM_SET`.

Named snapshots use `capture`, `list`, `show`, `diff`, `export`, and `import`.
`promote` accepts only reviewed non-calibration keys and writes the corresponding
manifest on a normal feature branch; it does not commit or push.

## 3. PX4 SITL Asset Injection

Script:
- `src/III-Drone-Simulation/scripts/install_gazebo_simulation_assets.sh`

It copies into PX4 tree:
- models
- worlds
- world models
- airframes

Then updates PX4 CMakeLists for airframes via helper script.

## 4. Included Simulation Assets

`Gazebo-simulation-assets` includes:
- drone model (`d4s_dc_drone`)
- pylon/world model assets (`hcaa_pylon_setup`)
- world file (`hca_full_pylon_setup.sdf`)
- custom posix airframe definitions

## 5. PX4 Coupling Details

Workspace includes local `PX4-Autopilot/` repo and package-level references to DIII fork branches/tags.

Mission/control integration points with PX4 include:
- `px4_msgs` subscriptions/publications
- daemon-managed micro-ROS agent bridging
- offboard mode registration via service APIs
- mode executor behavior inside mission package

The III daemon monitors FMU topic heartbeats exposed through the bridge. PX4 SITL/Gazebo can be started before or after `iii system boot`; PX4-dependent nodes remain inactive until the bridge is ready.

## 6. Patch Artifact

`patches/PX4-Autopilot.patch` contains (currently commented in installer) changes to GZBridge timing limits, suggesting previous need to handle world creation/clock startup latency.

## 7. Compatibility Observations

Repository docs/scripts reference multiple simulation naming variants (`gazebo-classic` and `gz`/Garden style), indicating transition history in simulation stack that should be standardized for current operational baseline.
