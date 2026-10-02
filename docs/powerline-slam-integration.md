# Powerline SLAM integration readiness

This workspace is prepared for integrating the powerline SLAM estimator
(`powerline_perception` in the `powerline_slam` repository) into the live III
simulation. Only the preparation lives here: the sensor layout the estimator was
developed on, its radar simulator configuration, recording and analysis tools,
an estimator build for this Python/ROS distribution, and generators for the
estimator's static inputs. The integration itself (estimator node, runtime
wiring, acceptance criteria) is defined by a research work order in the
powerline_slam workflow; the decisions it has to make are listed at the end.

All work is on the `powerline-slam` branch of the workspace and of
III-Drone-Core, III-Drone-Configuration, III-Drone-Interfaces,
III-Drone-Runtime, III-Drone-Simulation and Gazebo-simulation-assets. The
production simulation (`d4s_dc_drone`) is unchanged and stays the default.

## Sensor layout `d4s_dc_drone_powerline_eval`

The evaluation drone carries the dual-radar layout of the powerline_slam
r22–r26 development recordings (layout `U0_F50_C20_R0`), built from III's
current production drone model:

| Sensor | Topics | Frame | Mount in `drone` (x, y, z m; rotation) | Rate |
|---|---|---|---|---|
| Radar-U | `/sensor/mmwave/points`, `/sensor/mmwave/points_full` | `mmwave` | 0.025, −0.24, 0.295; boresight up, the existing III mount (yaw π, pitch −π/2) | 30 Hz |
| Radar-F | `/sensor/mmwave_forward/points`, `/sensor/mmwave_forward/points_full` | `mmwave_forward` | 0.105, −0.24, 0.285; pitch −40° (boresight 50° forward of up) | 30 Hz, triggered 5.10112 ms after Radar-U |
| Cable camera | `/sensor/cable_camera/image_raw`, `/sensor/cable_camera/camera_info` | `cable_camera` | 0, −0.215, 0.3; pitch −70° (optical axis 20° from up, toward the nose) | 10 Hz, 640×480, horizontal FOV 80° |

Both radars use the simulator-v2 IWR6843AOP model (`AOP_FAST_POINT`) with
separate profiles, seeds (1 and 2) and the hardware-triggered RF coexistence
schedule. Radar-F has its own evaluator truth
(`/simulation/ground_truth/mmwave_forward/scan_v2`, `.../conductor_labels`).
The camera publishes an ideal pinhole `camera_info` (fx = fy = 381.361,
cx = 319.5, cy = 239.5, no distortion) with each image stamp.

`Gazebo-simulation-assets/scripts/create_powerline_eval_drone_variant.py`
generates the model and PX4 airframe 99997 from the production model and
airframe; `--check` fails when they differ from a fresh generation, and
`test_launch_smoke.py` checks the SDF mounts against the configured static
transforms.

### Selecting the layout

The layout is selected in two places that must agree:

- the PX4 model: `tools/simulation/launch_simulation_tools.sh --sim-model
  gz_d4s_dc_drone_powerline_eval`, `scripts/workspace/iii_dev.sh sim
  start|restart --sim-model gz_d4s_dc_drone_powerline_eval` (also `stack
  start`), or the MCP `simulation` tool's `sim_model` argument;
- the configuration constant `/tf/sim/sensor_layout:
  d4s_dc_drone_powerline_eval`, which switches `tf_sim.launch.py` to the 20°
  camera mount and adds the `drone → mmwave_forward` transform, and
  `sim_assets.launch.py` to bridge Radar-F.

The default for both is the production layout.

### Radar assets and the sensor plugin

`models/d4s_dc_drone_powerline_eval/radar/` holds `RADAR_U.yaml`,
`RADAR_F.yaml`, their chirp profiles and `RF_COEXISTENCE_SCHEDULE.json`;
`world_models/hcaa_pylon_setup/radar/scene_scatterers_r22_v1.json` holds the
scene scatterers. `PROVENANCE.json` records every file's source path and
SHA-256 in the powerline_slam repository and what changed (only the scatterer
path, now a `model://` URI the plugin resolves; it also accepts absolute paths
and paths relative to the configuration file).

Equivalence with the development configuration was checked by rendering 40
scans at six static poses (mid-span, lateral, oblique, tilted, at and near a
pylon) with the powerline_slam plugin build and its dev13 configuration, and
with this workspace's plugin and assets:

- Radar-F: bitwise identical scans at all six poses (`points` and
  `points_full`, 12/12);
- Radar-U with the dev13 mount: bitwise identical (24/24 with Radar-F);
- Radar-U with the III mount: point counts differ by up to ±5 %, because the
  III mount turns the radar 180° about its boresight (decision: keep the III
  simulation mount and map to the hardware mount later).

## Live corridor flights

`scripts/workspace/powerline_slam_flights.py` flies the waypoint legs of the
powerline_slam r21 dual-radar stimulus (`powerline_slam_corridor_flights.json`,
11 legs per direction, `a_to_b` and `b_to_a`) through the III runtime, as
`perception_dataset_flights.py` does, and records an exact 28-topic contract:
estimator runtime inputs (`/clock`, camera image and info, both radars'
`points_full`, PX4 `sensor_combined`, `vehicle_odometry`,
`vehicle_local_position`, `vehicle_status_v1`, `timesync_status`), III context
(`points`, `pl_mapper`, `/tf`, `/tf_static`) and evaluator truth. Every flight
directory holds the bag, `mission_phase_evidence.json` (each leg's flown
command and its source-time interval on the simulation clock),
`flight_plan.json` (the live Gazebo-to-ROS mapping), `trajectory.json` and
`verification.json`. Catalog targets below 1.2 m live height are raised to it;
the evidence keeps both the flown and the catalog command.

`scripts/workspace/run_isolated_powerline_slam_flights.sh` runs the flights
isolated from other simulations on the host: its own ROS domain, Gazebo
partition, configuration root (seeded with the evaluation layout), simulation
seed, tmux sessions and transient supervision daemon, all of which it stops
after the run unless `--keep-running` is given. Run it in a container
disconnected from its network:

```bash
III_POWERLINE_RUN_ID=powerline_slam_live02 scripts/workspace/run_isolated_powerline_slam_flights.sh --flights a_to_b b_to_a
```

DDS discovery: the evaluation layout runs about 37 DDS participants.
Localhost-only discovery (`ROS_LOCALHOST_ONLY=1` / `LOCALHOST`) only reaches
participants with IDs below 32, so in the first acceptance run the Radar-F
bridges (participants 32–35) and the recorder never matched and four contract
topics stayed empty. The launcher therefore uses `SUBNET` discovery and refuses
to start unless the network namespace has nothing but the loopback interface;
`III_POWERLINE_DISCOVERY_RANGE=LOCALHOST` restores localhost-only discovery.

`scripts/workspace/analyze_powerline_slam_flight.py BAG` reports the topic
inventory, rates, real-time factor, the PX4 time contract, sensor stamps, the
Radar-F trigger offset, radar points per scan and PX4 heading against truth.

### Acceptance flights (`powerline_slam_live02`, seed 20261002)

| | `a_to_b` | `b_to_a` |
|---|---|---|
| Duration / messages | 165.5 s / 190 590 | 180.1 s / 205 089 |
| Contract topics with messages | 27 of 28 (+ `timesync_status` empty by contract) | 27 of 28 (+ `timesync_status` empty by contract) |
| Real-time factor | 0.956 | 0.945 |
| Radar-U / Radar-F scans | 4743 / 4742 | 5103 / 5103 |
| Camera images | 1572 | 1700 |
| Radar points per scan, p50 (max) U / F | 9 (21) / 7 (27) | 9 (20) / 7 (24) |
| Bag verification, analysis checks | passed | passed |

### Time contract (measured)

PX4 v1.16.1 runs with `UXRCE_DDS_SYNCT=0`, so its uXRCE-DDS timestamps are in
the Gazebo simulation-time domain. The values below compare stamps with the
latest `/clock` at bag receipt, so they are receipt-order diagnostics, not
source-time truth:

- PX4 `timestamp` − `/clock`: p50 −4 ms (one 4 ms physics step), p99 16–20 ms,
  max 40 ms; `timestamp` − `timestamp_sample`: 0–8 ms;
  `/fmu/out/timesync_status` carries no messages.
- Radar header stamps are the scan's simulation time (receipt lag p50 0 ms) at
  a constant 33.3 ms period; Radar-F − Radar-U stamp: 4–8 ms, mean 5.05–5.14 ms
  (the 5.10112 ms schedule on the 4 ms physics step).
- Camera image stamps are the render time (receipt lag p50 12 ms, period
  100 ms). `camera_info` carries the same stamps (every stamp matched in the
  span both topics cover: 1572/1572 and 1677/1677), but the plugin emits it
  after the frame's ground-truth render, so it arrives about 1 s late (p50
  1.0–1.1 s, max 4.2 s at real-time factor 0.95). A live consumer should take
  the intrinsics from the calibration file (or latch them) instead of pairing
  every image with its `camera_info`.

### Frames

The Gazebo world is ENU and equals powerline_slam's `WO002_SIM_DESIGN_ENU`.
III's `world` is north-west-up at the PX4 local origin: x = y_gz + dx,
y = −x_gz + dy, z = z_gz + dz, with the flight's offset in `flight_plan.json`.
A PX4 NED heading ψ is the III yaw −ψ.

## Estimator environment

`scripts/workspace/setup_powerline_slam_estimator_env.sh --powerline-root
CHECKOUT` builds what the estimator needs for Python 3.12 / ROS Jazzy under the
Git-ignored `.cache/powerline_slam_estimator`:

- GTSAM 4.2.1 with powerline_slam's fixed-lag modifications. The
  `patches/gtsam-4.2-fixed-lag-unused-keys.patch` in powerline_slam carries only
  the backport; the covariance-capture API that the estimator backend requires
  exists only in five vendored source files under
  `powerline_perception/build/gtsam-4.2/source`. The script verifies those
  files against `powerline-fixed-lag-artifacts.sha256` and installs them over
  the upstream tag;
- a virtual environment on the ROS Python packages, populated by the system pip
  (the image has no `ensurepip`), with user-site packages excluded, plus CPU
  torch;
- the estimator's native modules (`_slam_native`, `_bootstrap_native`), built
  into the checkout next to its Python 3.10 builds, and an import check.

Built and checked in the worktree devcontainer (Python 3.12.3, ROS Jazzy)
against a copy of powerline_slam at `c0e368f`: GTSAM 4.2.1 imports with all
five capture methods, `_slam_native`
(`native-cpp17-gtsam-4.2-correlated-transverse-local-line`) and
`_bootstrap_native` load for CPython 3.12, with CPU torch 2.14.1. The native
modules subclass GTSAM's Python types, so they are built with the pybind11
bundled in GTSAM's wrapper (2.10): the system pybind11 (2.11) uses other
internals on Python 3.12 and the import fails.

The estimator's test suite (`powerline_perception/tests`, run without ROS's
`launch_testing` pytest plugins): 951 passed, 70 skipped. Every failure (135)
and the one collection error come from what the copy deliberately leaves out:
133 read sealed `results/` artifacts, `corridor_simulation/provenance` files
or the candidate-v13 identity closure; 1 materializes the frozen v13 runtime
from the repository's Git tag; 2 backend tests assert that GTSAM loads from
powerline_slam's own `build/gtsam-4.2` path, and their functional assertions
pass here.

The frozen candidate-v13 runtime of powerline_slam is bound by hash to its
Python 3.10 native modules, so this environment is for development builds of
the estimator, not for frozen-candidate evidence.

## Estimator inputs

`scripts/workspace/powerline_slam_runtime_inputs.py` builds the estimator's
static and per-flight inputs; outputs are checked with powerline_slam's own
loaders:

| Subcommand | Output |
|---|---|
| `calibration --output DIR --doppler-source SET [--compare SET]` | The runtime calibration set (radar, camera extrinsics and intrinsics, Doppler calibrations) from the sim parameter set and the variant model. Against the dev13 set (`runtime_calibration_U0_F50_C20_R0`) it differs only in frame names (`mmwave`/`mmwave_forward` instead of `radar_up`/`radar_forward`) and in Radar-U's 180° boresight roll; camera intrinsics and extrinsics and Radar-F are identical. |
| `mission-priors --evidence FILE --output FILE [--to-world FLIGHT_PLAN]` | Commanded-pose priors (sidecar schema 4) from a flight's `mission_phase_evidence.json`: no prior on the first leg, each later leg from its begin + 5 s, 0.3 m / 0.03 rad. |
| `map-prior --source FILE --flight-plan FILE --output FILE` | A `WO002_SIM_DESIGN_ENU` map prior in III `world`, covariances included. |
| `imu-covariance --flight DIR [--flight DIR ...] --output FILE` | The gyro covariance from `sensor_combined` in each flight's four guarded hover holds, with powerline_slam's stationarity checks (run where ROS is sourced). |

Map prior and mission priors of one estimator run must share a map frame:
either both in `WO002_SIM_DESIGN_ENU` (the replay convention) or both in
`world` (`--to-world`).

## Findings that affect the integration

1. **PX4 heading bias in III's simulation.** PX4 v1.16 reads Gazebo's
   magnetometer, whose field at the world's location (Odense) has a
   declination of −3.1°, while PX4's world magnetic model expects +4.30° (PX4's
   `gz_bridge` notes a frame bug in the Gazebo magnetometer). PX4's heading
   estimate therefore differs from truth by 0.03–0.14 rad in flight (a_to_b mean
   0.070 rad, b_to_a 0.042 rad) and by about 0.085 rad in a standalone hover
   (PX4 and Gazebo only). The powerline_slam development PX4 build simulated
   the magnetometer inside PX4 and tracked commanded headings within 0.0033 rad
   in its pose-tracking dry runs. The bias affects the production layout too.
   The variant could reproduce the development conditions with
   `param set-default SENS_EN_MAGSIM 1` in airframe 99997 and no Gazebo
   magnetometer in its model; whether the evaluation should instead keep a
   realistic heading error is a research decision.
2. **Hover motion.** The gyro covariance from the live02 hover holds is
   1.4e-4, 2.4e-4 and 2.4e-7 rad²/s² (x, y, z) against about 1.2e-6, 1.1e-6
   and 5.8e-8 in the r18 development inputs. The gyro noise model is the same,
   so this is vehicle motion under III's flight stack.
3. **IMU accelerometer noise.** The variant inherits III's current
   accelerometer noise (white 0.0064–0.0069 m/s², bias 0.0006) instead of the
   development model's (0.00186, bias 0.006).
4. **Radar-U mount.** The III simulation mount is rotated 180° about the
   boresight relative to the development mount; the calibration generator
   follows the III mount.
5. **`camera_info` latency** (see the time contract).
6. **The powerline_slam replay pipeline does not accept III bags as is.** It
   requires a clock-contract proof from its instrumented PX4 build, checks the
   recorded `model.sdf` and the frozen 11-phase mission, reads radar topics by
   their powerline names (`radar_up`, `radar_forward`), and writes to frozen
   result roots. Accepting III recordings needs either an approved PX4
   instrumentation or a work-order change of the estimator's input contract.

## Decisions for the integration work order

Recommendations, for the research work order to confirm or replace:

- **Package home:** a separate ROS 2 package and repository
  (e.g. `III-Drone-Powerline-SLAM`) wrapping `powerline_perception`, so the
  estimator's evidence and versioning stay apart from III-Drone-Core.
- **Node:** a lifecycle node managed by III supervision, parameterized through
  III-Drone-Configuration (topics, frames, calibration and prior paths) and
  enabled only in the sim profile with the evaluation layout at first.
- **Inputs:** both radars' `points_full`, the camera image with intrinsics
  from the calibration set, `sensor_combined`, `vehicle_odometry`, `/clock`;
  priors and calibration from `powerline_slam_runtime_inputs.py`.
- **Time:** accept the measured `UXRCE_DDS_SYNCT=0` contract (PX4 time is
  simulation time within one physics step) or instrument PX4 for the
  powerline clock-contract proof; editing PX4 needs approval.
- **Output:** a new topic next to `/perception/pl_mapper/powerline` instead of
  replacing it, with no consumer switched until the estimator is evaluated
  live.
- **Heading:** decide on finding 1 before evaluating against truth.
- **Hardware:** map Radar-U and Radar-F to the physical mounts later.

## Reproducing

```bash
# Inside the devcontainer, disconnected from its network:
III_POWERLINE_RUN_ID=powerline_slam_live03 scripts/workspace/run_isolated_powerline_slam_flights.sh
python3 scripts/workspace/analyze_powerline_slam_flight.py datasets/powerline_slam/powerline_slam_live03/a_to_b/bag
# With network access, against a powerline_slam checkout:
scripts/workspace/setup_powerline_slam_estimator_env.sh --powerline-root PATH
```
