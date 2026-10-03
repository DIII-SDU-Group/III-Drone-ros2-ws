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

HIL keeps the PX4–Pi Ethernet link on `10.41.10.0/24`. The workstation reaches
the Pi through a direct link or a routed LAN. The operator target defaults to
`iii.local`; the workstation launcher resolves it to a reachable IPv4 address
and selects the matching local route for PX4 transport. Use `--host HOST` on
HIL commands to select another Pi for that invocation. Provision and deploy
the Pi first.

Install this checkout's [native dev ground-computer profile](ground-computer-installation.md)
first. Once the mission candidate has passed source review, use the installed
host CLI for provisioning and deployment, then start the split-host profile:

```bash
python3 scripts/install_gc.py --profile dev
source setup/setup_hil.bash
iii host provision --host iii.local --profile hil
iii deploy dev --host iii.local --build --restart
./iii-dev exec bash -lc 'source /opt/ros/jazzy/setup.bash; source /home/iii/ws/install/local_setup.bash; colcon build --base-paths src --packages-select iii_drone_simulation --symlink-install'
./iii-dev hil start
./iii-dev hil status
./iii-dev hil stop
```

When changing an already running HIL checkout to a new middleware profile,
stop that owned HIL instance before provisioning and building so its old
adapter executable and DDS participants cannot be reused.

The installed CLI records this checkout's source identity; runtime commands
refuse a mismatched Pi CLI source. Preview a deployment with
`iii deploy dev --host iii.local --build --dry-run`.
Only deploy a reviewed mission candidate; `--build` includes dirty III source
components in the workstation cross-build and synchronizes the resulting
install tree to the Pi.

`hil start` preserves a healthy running mission. `hil restart` explicitly
shuts down Pi-managed ROS processes before resetting workstation simulation
time, then boots and starts the Pi graph. Virtual SIM/HIL runtime lifecycle
commands do not require armed or airborne telemetry. Workstation HIL stop
still verifies process ownership and stopped endpoints before shutting down
the Pi runtime.
The lower-level `tools/simulation/launch_hil_workstation.sh` remains the
workstation process owner used by this coordinator. Run these commands from
the host checkout; the launcher forwards its resolved environment into the
matching devcontainer.

For a different Pi, pass the same target explicitly, for example
`./iii-dev hil start --host alternate.local` and
`./iii-dev hil status --host alternate.local`. The argument takes precedence
over inherited HIL endpoint, SSH, and Runtime API defaults for that command.
Startup also registers and selects that same runtime in the HIL ground-control
proxy after its identity check; it fails if the proxy is connected to a
different runtime.
The Pi's `10.42.0.15/24` address remains a static direct-link fallback for
provisioning and recovery; it is not the operator target default.

`./iii-dev hil start` and `./iii-dev hil restart` are rendered by default.
Use `--headless` only when an operator intentionally does not need the Gazebo
viewer:

```bash
./iii-dev hil start             # rendered Gazebo, Pi runtime, HIL GC, QGroundControl, and GC browser window
./iii-dev hil start --headless  # same HIL profile without desktop windows
./iii-dev hil restart           # explicit safe simulation epoch reset
./iii-dev hil status            # concise ready/running/degraded/stopped state
./iii-dev hil status --json     # same aggregate state for automation
./iii-dev hil logs --follow     # captured coordinator and GC diagnostics
```

The Gazebo partition is an internal discovery namespace, computed from the
checkout and PX4 instance. It prevents this HIL viewer and its adapters from
joining another checkout's Gazebo world. Operators do not pass a partition to
`iii-dev`. A repeated rendered `hil start` keeps a healthy PX4/Gazebo mission
running and only recreates a missing or dead **owned** Gazebo GUI pane.
Headless start never creates that pane or opens QGroundControl or a browser.
After final HIL health passes, a rendered start/restart ensures the pinned host
QGroundControl service is started and opens the HIL GC URL in a new window of
the default browser if no ground-control window is already open. Repeating
`hil start` preserves the existing operator windows. QGroundControl is host-owned and `hil stop` does not stop
it; closing its window is an independent operator action. `hil stop` closes
the owned simulation session, including its viewer.

Normal lifecycle output reports named stages and a stable
`runtime_logs/hil/latest.log` pointer. Raw coordinator, launcher, and ground
control output is captured in a timestamped file; add `--verbose` to a
start/restart/stop command to mirror it to the terminal. On failure, inspect
`./iii-dev hil status` before retrying because a failed transition may leave
verified components running.

Each checkout and PX4 instance gets a distinct Gazebo partition. The launcher
reports it as `hil_gz_partition` and verifies the Gazebo server's recorded
process identity as part of readiness. A new start refuses a partition that
already contains an unowned world. Stop closes both the owned PX4 and Gazebo
processes, so restart begins a fresh simulation epoch. Explicit
`III_HIL_GZ_PARTITION` overrides apply to the launcher and workstation fixture
and probe tools. HIL also checks and installs the current project simulation
assets through the normal asset installer before launching.

HIL ground control runs on the workstation at `http://127.0.0.1:5174`, with
proxy port `8781` and Compose project `iii-ground-control-hil`. Its identity
checks require the Pi HIL profile. The ordinary simulation/field GC and
`iii-dev stack` retain their existing settings. Override the HIL GC settings
with `III_HIL_GC_ENV_FILE`; the default is `setup/ground-control.hil.env`.
On the Mission page, refresh installed missions, select a compatible catalog
ID, and apply it with no active mission or custom operation. Virtual HIL
catalog selection does not depend on armed/airborne telemetry.
`inspection-production` is the canonical HIL default.

Arm, take off, and activate the selected mission through the Runtime API
commands exposed by GC and the CLI. HIL uses the same PX4 SITL battery model
as SIM: the Pi's XRCE agent publishes `/fmu/out/battery_status` to the
workstation charger adapter, and the adapter sends charging power back on
`/fmu/in/sim_battery_charge`. The adapter publishes the PX4 voltage on
`/payload/charger_gripper/battery_voltage` for the mission's low-voltage
condition. The manual recharge and charging-interrupt commands remain
available for operator-directed transitions.
After Land, allow PX4 to finish its normal auto-disarm: HIL uses a 60-second
delay after touchdown to accommodate cable transitions, and its Runtime API
allows 180 seconds for descent and disarm confirmation.

Workstation adapters publish simulated camera, mmWave, charger/gripper, clock,
and static sensor transforms into DDS domain 42. The Pi publishes the sole
dynamic `world -> drone` transform from the same PX4 odometry used by its
controller, and owns perception, control, mission execution, and the SITL XRCE
agent. This keeps the control state and sensor transforms in one pose frame as
the PX4 estimate drifts relative to Gazebo ground truth during a long run.
Source `setup/setup_hil.bash` for workstation ROS tools as well: it selects
Fast DDS with UDP transport and domain 42, matching the Pi's XRCE agent and
managed ROS graph. Re-run `iii host provision --host iii.local --profile hil`
before the next HIL start to replace the Pi's older Cyclone DDS systemd
overrides; `iii deploy dev` alone does not update those host overrides.

Use `iii px4 inspect --host iii.local` to inspect the Pi-side Ethernet link and
listeners. PX4 firmware and parameter changes remain explicit manual developer
work; this deployment path never writes PX4 firmware or arms the vehicle.

The split-host HIL profile keeps workstation SITL separate from the connected
physical PX4. SITL uses Pi XRCE UDP `8890`, client key `2`, system ID `8`, and
MAVLink UDP `14544`. The physical PX4 HIL baseline retains XRCE UDP `8889`,
client key `1`, and MAVLink UDP `14542`; the split-host profile does not start
its XRCE agent. The HIL Runtime API and HIL command clients listen on `14544`,
so physical traffic cannot become HIL vehicle telemetry. Provision the updated
runtime configuration before starting HIL after an endpoint change.

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
