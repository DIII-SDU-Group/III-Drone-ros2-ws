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
4. `iii_drone_simulation/sensors_sim.launch.py` provides simulated sensor ingress through the supervised system graph.
5. `ros_gz_bridge` bridges simulated camera and depth point cloud topics.
6. `depth_cam_to_mmwave` converts incoming depth cloud to mmWave-like output topic (`/sensor/mmwave/pcl`).
7. `tf_sim.launch.py` publishes sim-specific static transforms and dynamic drone frame updates.

QGroundControl is outside III supervision. Connecting or disconnecting it affects PX4/operator telemetry, not III lifecycle bringup.

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
