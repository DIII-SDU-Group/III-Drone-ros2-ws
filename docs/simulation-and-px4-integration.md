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

QGroundControl is an ordinary host-native PX4 client. The simulation launcher
owns PX4/Gazebo only; connecting or disconnecting QGroundControl affects
operator telemetry, not III lifecycle bringup.

HIL is a maintained, non-flight acceptance profile. Keep the aircraft
disarmed and remove the propulsion battery. OptiTrack is a real-aircraft
profile and is prepared separately from HIL.

HIL uses two independent Ethernet links: PX4 is connected to the Pi on
`10.41.10.0/24`, while the workstation connects directly to the Pi. The
workstation resolves the Pi as `iii.local` by default; use
`III_HIL_PI_ADDRESS` as a direct fallback when mDNS is unavailable. The
canonical workstation control surface is
`tools/simulation/launch_hil_workstation.sh` (`start`, `status`, and `stop`).
Provision and deploy the Pi first, then start the workstation HIL processes.

Start the two independent surfaces explicitly:

```bash
iii host provision --host iii.local --profile hil
iii deploy dev --host iii.local --build --restart
tools/simulation/launch_hil_workstation.sh start
tools/simulation/launch_hil_workstation.sh status
```

Use `iii px4 inspect --host iii.local` to inspect the Pi-side Ethernet link and
listeners. PX4 firmware and parameter changes remain explicit manual developer
work; this deployment path never writes PX4 firmware or arms the vehicle.

When the physical PX4 is connected on the Pi Ethernet link, do not start the
workstation SITL launcher. It would otherwise compete for the same Pi XRCE and
MAVLink endpoints. The launcher detects a live `10.41.10.2` peer and refuses
to start by default. Set `III_HIL_ALLOW_SITL_WITH_PHYSICAL_PX4=1` only for a
deliberate split-host experiment where that coexistence has been designed and
verified.

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
