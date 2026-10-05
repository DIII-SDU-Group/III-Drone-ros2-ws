# Powerline SLAM integration readiness

This workspace integrates the powerline SLAM estimator (`powerline_perception`
in the `powerline_slam` repository) with the III simulation. It holds:

- the sensor layout the estimator was developed on and its radar simulator
  configuration;
- recording and analysis tools;
- an estimator build for this Python/ROS distribution and generators for the
  estimator's static inputs;
- since WO-2026-10-05-001, a passive perception backend: the separate lifecycle
  node `/perception/powerline_slam/powerline_slam`, selectable at boot instead
  of the legacy perception stack (next section).

The acceptance criteria are defined by research work orders in the
powerline_slam workflow. The preparation's recommendations are listed under
"Decisions for the integration work order".

All work is on the `powerline-slam` branch of the workspace and of
III-Drone-Core, III-Drone-Configuration, III-Drone-Interfaces,
III-Drone-Simulation, Gazebo-simulation-assets and PX4-Autopilot
(III-Drone-Runtime's branch carries no change), merged with
`deployment-infrastructure-redesign` up to e09c12f (2026-10-05). The
production simulation (`d4s_dc_drone`) stays the default; it shares the
magnetometer fix (finding 1). The changes that are not specific to the
evaluation layout are listed at the end.

## Powerline SLAM perception backend (passive)

WO-2026-10-05-001 adds the estimator to the canonical III system as a
perception backend. Its output is passive: no mission, maneuver or overview
consumer reads it.

### Selector

The constant parameter `/perception/processing_stack` (`legacy` or
`powerline_slam`, default `legacy`) selects the perception stack. Supervision
reads it from the active parameter set before it builds the launch graph and
latches it for the booted session; there is no hot switching.

- `legacy`: the graph is unchanged (regression-locked against Supervision
  `186ac916`).
- `powerline_slam`:
  - replaces `hough_transformer`, `pl_dir_computer` and `pl_mapper` with the
    entity `powerline_slam`;
  - is legal only in the `sim` profile with `/tf/sim/sensor_layout:
    d4s_dc_drone_powerline_eval`; other combinations fail before anything
    launches;
  - the node active-depends on `tf` and `sim_assets`, and nothing depends on it.

`/perception/powerline_slam/runtime_config` names the node's runtime
configuration (`iii.powerline-slam-runtime-config/v1`). It lists:

- the calibration set;
- the map and mission priors;
- the gyro covariance;
- the estimator contracts;
- the pylon checkpoint;
- an optional `node` section: worker processes, output directory, ledger and
  timeouts.

The tracked defaults keep `legacy` and `none`. An integration test selects a
living parameter set of this clone instead. For example, the snapshot holds a
copy of the active set with the stack, the layout and the configuration path
changed:

```bash
printf 'version: 1\nactive_parameter_set: snapshots/powerline_slam_integration_a_to_b.yaml\n' > .config/iii_drone/profiles/sim.yaml
iii system boot
iii system start --select-nodes powerline_slam --include-dependencies   # tf, sim_assets and powerline_slam only
```

### Node

Package `src/iii_drone_powerline_slam` (see its README) has two processes:

- **ROS process:** raw subscriptions to the six runtime inputs, `camera_info`
  (a calibration check) and `timesync_status` (must stay silent), and an
  unbounded, order-preserving forwarder.
- **Estimator host process:** runs the pinned powerline_slam checkout's
  incremental pipeline, the same code as the offline III replay.

Keeping the estimator out of the ROS interpreter is required. When it ran in a
thread of the ROS process it held the interpreter lock, and about 90 % of the
best-effort PX4 samples were lost in a 1.0x bag playback.

Outputs, under `/perception/powerline_slam`:

| Topic | Content |
|---|---|
| `powerline` | `iii_drone_interfaces/Powerline`: confirmed conductors in `drone`, source-time stamps, generation/epoch-scoped stable ids; no lines while fail-closed |
| `diagnostics` | `StringStamped` JSON per frame (v13 health, runner/global state, anchors, local-frame generation, continuity guard, bridge, fail-closed reason), and a runtime record once per second (counters, queues, latency) |
| `state` | `Idle`, `Waiting`, `Running` or `FailClosed` |

Service `flush_and_finalize` closes the input and runs the post-traversal
finalization. It is for development and evaluation only.

### Estimator environment and pin

`deps/powerline-slam.json` pins the powerline_slam commit and its v13-derived
runtime. Set the environment up in two steps:

1. On the host, run `scripts/workspace/sync_powerline_slam_checkout.sh`. It
   checks out the pin, stages the Git-ignored GTSAM fixed-lag sources and
   materializes the runtime against its binding.
2. In the devcontainer, run
   `scripts/workspace/setup_powerline_slam_estimator_env.sh --v13-runtime`. It
   builds the native modules.

The package is excluded from the ARM64 cross-build, and Supervision's dependency
on it is limited to the sim profile.

### Evidence (WO-2026-10-05-001)

The evidence is in the powerline_slam provenance root
`corridor_simulation/provenance/WO-2026-10-05-001/`:

- the III-native contracts and their amendments A1–A4;
- the offline replay of the immutable `powerline_slam_live11` flights
  (Backlog 03);
- the node, adapter and selector evidence (Backlogs 04–06);
- the bag-playback online/offline parity (Backlog 07);
- the regression and resource summary (Backlog 08).

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

The variant keeps the production model's Gazebo magnetometer and inherits its
airframe's magnetometer parameters (finding 1).

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
`perception_dataset_flights.py` does, and records an exact 29-topic contract:
estimator runtime inputs (`/clock`, camera image and info, both radars'
`points_full`, PX4 `sensor_combined`, `vehicle_odometry`,
`vehicle_local_position`, `vehicle_status_v1`, `timesync_status`), III context
(`points`, `pl_mapper`, `/tf`, `/tf_static`), evaluator truth, and Gazebo's own
IMU samples (`/simulation/gazebo/imu`, bridged in the evaluation layout) as
clock evidence. Every flight directory holds the bag,
`mission_phase_evidence.json` (each leg's flown command and its source-time
interval on the simulation clock), `flight_plan.json` (the live Gazebo-to-ROS
mapping), `trajectory.json` and `verification.json`. Catalog targets below
1.2 m live height are raised to it; the evidence keeps both the flown and the
catalog command.

Before the first takeoff a run records `ground_imu/`: 60 s of IMU, arming
state, Gazebo IMU and truth, disarmed on the ground, from which the gyro noise
covariance is computed. A flight's bag stops only when the camera truth, which
the sensor plugin renders behind the simulation, covers the last leg (or after
60 s). A fly command that finds the maneuver controller not ready is reissued
up to twice, 5 s apart; the leg starts with the accepted command and
`verification.json` lists each leg's attempts.

Each flight flies in its own process, appending to the run directory. In two
single-process runs (`live03`, `live05`) the III tools' ROS node stopped
receiving any service response for minutes during the second flight, while
PX4, the maneuver controller and the supervision daemon were fine; a retried
fly command (and a timed-out shutdown) now also replaces that node, recorded as
`tools_rebuilds` in `verification.json` and `run_manifest.json`.

Two recorders take a flight: one for the three camera-resolution image topics
(`image_raw` and the two instance masks, 0.6–0.9 MB per message, 99 % of the
flight's bytes at 21 MB/s) and one for the other 26 streams. After the flight
their bags are merged in receipt order into the flight's `bag`, whose
per-topic message counts must equal the parts', and `verification.json`
records each recorder's own count of transport losses. III's ROS graph is
UDP-only (`FASTDDS_BUILTIN_TRANSPORTS=UDPv4`, `setup/ros_setup.bash`) and the
host caps socket receive buffers at `net.core.rmem_max` (212 KB here), so one
recorder taking everything overflowed its socket during the image bursts
(about 50 dropped datagrams per second) and lost 100–300 samples of the other
streams per flight, while a second recorder subscribed only to `/clock` and
odometry received every sample. A deeper subscription history does not help:
the samples never reach the recorder's history. Since 2026-10-05 this host's
default socket receive buffer is 8 MiB (`net.core.rmem_default`, with
`net.core.rmem_max` 64 MiB, in `/etc/sysctl.d/60-iii-dds-udp-buffers.conf`),
and in `powerline_slam_live10` one recorder taking all 29 topics lost nothing
either (below). The split stays, so recordings do not depend on the host
setting.

`scripts/workspace/run_isolated_powerline_slam_flights.sh` runs the flights
isolated from other simulations on the host: its own ROS domain, Gazebo
partition, configuration root (seeded with the evaluation layout), simulation
seed, tmux sessions and transient supervision daemon, all of which it stops
after the run unless `--keep-running` is given. Run it in a container
disconnected from its network:

```bash
III_POWERLINE_RUN_ID=powerline_slam_live10 scripts/workspace/run_isolated_powerline_slam_flights.sh --flights a_to_b b_to_a
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
Radar-F trigger offset, radar points per scan, PX4 heading against truth and
samples missing from the periodic streams (`/clock`, `sensor_combined`, Gazebo
IMU, truth state).

### Acceptance flights (seed 20261002)

With the current variant and tooling (`powerline_slam_live11`, fenced to 12
cores while a HIL soak shared the host):

| | `a_to_b` | `b_to_a` |
|---|---|---|
| Duration / messages | 169.5 s / 227 615 | 183.9 s / 245 128 |
| Contract topics with messages | 28 of 29 (+ `timesync_status` empty by contract) | 28 of 29 (+ `timesync_status` empty by contract) |
| Real-time factor | 0.933 | 0.916 |
| Radar-U / Radar-F scans | 4730 / 4653 (equal in their common span) | 5042 / 5044 |
| Camera images; during the legs with `camera_info` and truth | 1566; 1493 of 1493 | 1645; 1597 of 1597 |
| Radar points per scan, p50 (max) U / F | 9 (19) / 7 (29) | 9 (19) / 7 (23) |
| PX4 heading − truth, mean (range) | −0.002 rad (−0.006…0.004) | −0.002 rad (−0.007…0.003) |
| PX4 IMU timestamps equal to a Gazebo IMU stamp | 15 510 of 15 510 | 16 805 of 16 805 |
| Samples missing: `/clock`, Gazebo IMU, truth state; `sensor_combined` | 0, 0, 0; 0 | 0, 0, 0; 3 |
| Transport losses reported by the streams / images recorder | 0 / 0 | 0 / 0 |
| Gyro variance in the hover holds (x, y, z; rad²/s²) | 5.0e-5, 2.2e-5, 1.8e-7 | 1.2e-5, 3.2e-5, 1.7e-7 |
| Leg command attempts | 1 per leg | 1 per leg |
| Bag verification, analysis checks | passed | passed |

The ground segment recorded 60.0 s (41 145 messages) disarmed, without
transport losses, and passed. Missing `sensor_combined` samples are intervals
PX4's publisher skipped (the recorders report no transport loss). Radar-F has
fewer scans in `a_to_b` only because the recorder subscribed to it 2.5 s
after Radar-U; in their common span both have 4652 scans and no gap.

`powerline_slam_live09` was the acceptance run before the resting spawn
(heading 0.000…0.040 rad in `a_to_b`, from the start of the flight).

Earlier runs: `powerline_slam_live02` flew both directions with Gazebo's
magnetometer before its fix; `live03` and `live03b` with PX4's simulated
magnetometer; `live04` was the previous acceptance run. In `live03` and
`live05` the second flight stopped when the runner's ROS node stopped
receiving service responses, before flights got processes of their own
(`live06` on). `live07` verified the magnetometer fix, `live07d` recorded the
hold setpoints, and `live08` the terminal-tracking change, with a deeper
recorder history that lost more samples instead of fewer. Until `live08` one
recorder lost 115–314 messages on the transport layer per flight; radar and
camera header stamps showed no gaps.

`powerline_slam_live10` verified the host's larger UDP buffers: the run, fenced
to 12 cores while a HIL soak shared the host and GPU, recorded with an extra
recorder taking all 29 topics with default QoS over UDP, as the flights'
single recorder did until `live08` (`live07`: 162 and 178 messages lost). Both
flights passed; that recorder's sockets had 8 MiB buffers, it reported no
loss, the kernel counted no receive-buffer error during the run, and over the
flights' source-time span every stamped topic holds exactly the same samples
in it as in the flights' split bags. The flights' streams recorder reported
one lost message per flight, which is no gap in any stamped stream (it falls
at the start of the recording or on `/tf`).

### Time contract (measured)

PX4 v1.16.1 runs with `UXRCE_DDS_SYNCT=0`, so its uXRCE-DDS timestamps are in
the Gazebo simulation-time domain. The values below compare stamps with the
latest `/clock` at bag receipt, so they are receipt-order diagnostics, not
source-time truth (`live09`):

- PX4 `timestamp` − `/clock`: p50 −8 to −4 ms (within two 4 ms physics
  steps), p99 0 ms, range −20…8 ms; `timestamp` − `timestamp_sample`:
  0–16 ms; `/fmu/out/timesync_status` carries no messages.
- Source-side pairing: every PX4 `sensor_combined` timestamp within the
  Gazebo IMU span equals the stamp of a Gazebo IMU sample on
  `/simulation/gazebo/imu` (`live09`: 15 564 and 17 107; 99.96–99.98 % in
  earlier runs, whose recorder lost Gazebo samples), so PX4's clock is the
  simulation clock at the source.
- Radar header stamps are the scan's simulation time (receipt lag p50 0 ms) at
  a constant 33.3 ms period; Radar-F − Radar-U stamp: 4–8 ms, mean 5.05–5.15 ms
  (the 5.10112 ms schedule on the 4 ms physics step).
- Camera image stamps are the render time (receipt lag p50 12 ms, period
  100 ms); the image reaches ROS through the Gazebo bridge. The sensor plugin
  emits `camera_info` with the image's stamp when the frame arrives (receipt
  lag p50 8 ms, max 28 ms). Until 2026-10-02 it did so only after the frame's
  ground-truth render, 1–5 s late. The camera truth (conductor and pylon masks
  and frame truth) arrives with a receipt lag of p50 12 ms but up to 1.9–2.7 s
  when the render falls behind, which the flight runner's drain covers. A live
  consumer should still take the intrinsics from the calibration set (or latch
  them) rather than pair every image with its `camera_info`.

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
| `imu-covariance --ground GROUND_IMU_DIR --output FILE` | The gyro covariance from `sensor_combined` in a run's disarmed ground segment, with powerline_slam's stationarity checks: sensor noise only (`live04`: 5.0e-8, 3.4e-8 and 2.2e-8 rad²/s²). Run where ROS is sourced. |
| `imu-covariance --flight DIR [--flight DIR ...] --output FILE` | The same from each flight's four guarded hover holds (the powerline_slam r18 rule). Under III's flight stack the holds measure vehicle motion as well (finding 2). |

Map prior and mission priors of one estimator run must share a map frame:
either both in `WO002_SIM_DESIGN_ENU` (the replay convention) or both in
`world` (`--to-world`).

## Findings that affect the integration

1. **PX4 heading (fixed, both layouts).** With Gazebo's magnetometer PX4's
   heading differed from truth by 0.03–0.14 rad in flight (live02 mean 0.070
   and 0.042 rad). Four causes: gz-sim 8.15's Magnetometer system (default
   `use_earth_frame_ned` true) rotates the field's NED components, laid on the
   ENU axes, into the body frame, a reflected field that PX4's `gz_bridge`
   mapping made look right only for a level vehicle (declination mirrored to
   −3.11°, the vertical component leaking into the heading when the vehicle
   tilts); the true field's declination at the world's location is +3.12°
   (gz-sim's tables) where PX4's world magnetic model gives +4.30°; ekf2
   overwrote a configured `EKF2_MAG_DECL` with the model's value; and with
   automatic (3D) fusion the EKF learned earth-field and bias states a
   hovering vehicle cannot observe, which left the heading 0.02 rad off. Fixed
   together: the world sets `use_earth_frame_ned` false, `gz_bridge` maps the
   true body field (FLU) to FRD, ekf2 keeps `EKF2_MAG_DECL` when the model's
   declination is not in use (PX4-Autopilot `powerline-slam`), and both D4S
   airframes set `EKF2_DECL_TYPE` 2, `EKF2_MAG_DECL` 3.12 and `EKF2_MAG_TYPE` 1
   (heading fusion; the simulated field has no bias). PX4's heading now stays
   within 0.009 rad of truth in the hover holds and within 0.013 rad in the
   legs after the first (`live08`, `live09`), with flight means of −0.005 to
   +0.006 rad (`live07`–`live09`). The evaluation layout's interim fix, PX4's
   simulated magnetometer (`SENS_EN_MAGSIM`), is gone.

   Until 2026-10-05 a start-up transient remained, from PX4's EKF
   initialisation rather than the magnetometer: PX4 spawns the vehicle at the
   world origin, where `hca_full_pylon_setup`'s terrain lies 3.0 cm lower, so
   the vehicle dropped onto it while PX4 started (its first IMU samples read
   free fall and a 10 g landing). Within 0.1 s of the EKF's tilt alignment
   the EKF held a horizontal accelerometer bias the simulated IMU does not
   have (0.09–0.16 m/s², against Gazebo's 0.0006; the vertical bias at the
   0.8 m/s² `EKF2_ABL_LIM`), which on the ground is indistinguishable from
   tilt: PX4's attitude was 0.01–0.016 rad off truth, and heading fusion with
   the field's 70° inclination turned that into a heading error that depends
   on the heading, up to 0.04 rad at the start of `live09`'s first flight,
   until about three minutes into it. In this world the D4S airframes now
   spawn the vehicle at its resting height (`PX4_GZ_MODEL_POSE` 0,0,−0.028
   unless set). In standalone boots without rendering the EKF then starts
   with no bias (≤ 0.004 m/s², against 0.09–0.11 with the drop) and within
   0.0012 rad of the true attitude (against 0.009–0.011 rad, heading 0.026),
   and a take-off, hover and landing stays within 0.0015 rad (heading
   0.003 rad). In the corridor flights of `live11` the EKF's bias was 0.0001
   m/s² after alignment and 0.005 m/s² at take-off (0.157 and 0.151 in
   `live09`), and PX4's heading stayed within 0.007 rad of truth throughout
   both flights, its first leg included (at most 0.0044 rad, against 0.027
   rad in `live09`).
2. **Hover motion (fixed in III Core).** In the hover holds of the earlier
   live flights the gyro variance (1.3–1.9e-4, 1.2–2.7e-4 and 0.2–1.8e-6
   rad²/s², x, y, z) equalled the variance of the true angular velocity, so it
   was vehicle motion: about ten times PX4's own position hold of the same
   model (1.2–1.9e-5 in roll and pitch). III's terminal position tracking
   integrated every millimetre of PX4's hold wander (5–19 mm std, excursions
   to 45 mm) and corrected it with rest-to-rest segments whose feedforward
   (up to 0.045–0.07 m/s²) the maneuver controller sends at 5 Hz and PX4
   applies as steps. It now integrates only the error beyond 2 cm and starts a
   correction once it reaches 1 cm (both capped at half the arrival
   tolerance, 0.10–0.15 m in III's parameter sets). In `live08` and `live09`
   5 of 16 holds made a 1 cm correction and the holds move 1.6–3.0e-5,
   1.2–4.0e-5 and 1.5–1.7e-7 rad²/s² (x, y, z), up to twice PX4's own hold.
   The r18 development inputs had about 1.2e-6, 1.1e-6 and 5.8e-8; the
   remaining factor of ten is PX4 v1.16 and this simulation, so the gyro noise
   covariance still comes from the disarmed ground segment
   (`imu-covariance --ground`): 2.2–5.0e-8 rad²/s², the gyro model's noise.
3. **IMU accelerometer noise.** The variant inherits III's current
   accelerometer noise (white 0.0064–0.0069 m/s², bias 0.0006; PX4's upstream
   x500 values) instead of the development model's (0.00186, bias 0.006). A
   standalone hover with the development values moved the same.
4. **Radar-U mount.** The III simulation mount is rotated 180° about the
   boresight relative to the development mount; the calibration generator
   follows the III mount.
5. **`camera_info` latency and truth at the end of a flight (fixed).**
   `camera_info` now arrives with the image, and the flight runner waits for
   the camera truth of the last leg; in `live04` every image during the legs
   has both.
6. **The powerline_slam replay pipeline does not accept III bags as is.** It
   requires a clock-contract proof from its instrumented PX4 build, checks the
   recorded `model.sdf` and the frozen 11-phase mission, reads radar topics by
   their powerline names (`radar_up`, `radar_forward`), and writes to frozen
   result roots. Accepting III recordings needs either an approved PX4
   instrumentation or a work-order change of the estimator's input contract.
   PX4 needs no instrumentation to pair its clock with the simulation's: every
   flight now records Gazebo's IMU samples, and every PX4 `sensor_combined`
   timestamp in their span equals one of their stamps (`live09`; 99.8–99.98 %
   in earlier recordings and two standalone hovers, where the recorder lost
   Gazebo samples). The next section
   proposes the input contract for the work order.

## Proposed estimator input contract for III recordings

For the research work order to adopt or amend. It replaces the frozen
powerline_slam qualification checks that III recordings cannot satisfy;
converting III bags into powerline_slam's recording format is not an option,
because it would fabricate the clock-contract proof and provenance.

- **Recording:** one exact-topic bag per flight from `powerline_slam_flights.py`
  (run manifest schema `iii.powerline-slam-flight-run/v1`) and the run's
  `ground_imu` segment, with each run's `run_manifest.json`.
- **Topics, by configuration rather than by name in code:**

  | Estimator input | III topic | Frame |
  |---|---|---|
  | Radar-U scans | `/sensor/mmwave/points_full` | `mmwave` |
  | Radar-F scans | `/sensor/mmwave_forward/points_full` | `mmwave_forward` |
  | Camera | `/sensor/cable_camera/image_raw`, intrinsics from the calibration set | `cable_camera` |
  | IMU | `/fmu/out/sensor_combined` | PX4 FRD, drone FLU after conversion |
  | Odometry | `/fmu/out/vehicle_odometry` | NED / FRD |
  | Clock | `/clock` | simulation time |

- **Clock contract:** PX4 runs in lockstep with Gazebo with
  `UXRCE_DDS_SYNCT=0`. Per bag, `/fmu/out/timesync_status` is empty and at
  least 99 % of `sensor_combined` timestamps equal a Gazebo IMU sample stamp on
  `/simulation/gazebo/imu`. Both stamps come from their sources, so this pairs
  PX4 time with simulation time without instrumenting PX4.
  `analyze_powerline_slam_flight.py` checks both.
- **Mission evidence:** `mission_phase_evidence.json` (schema
  `iii.powerline-slam-mission-phase-evidence/v1`) and `flight_plan.json`,
  instead of the frozen r21 11-phase mission. Priors come from
  `powerline_slam_runtime_inputs.py mission-priors` (and `map-prior` for the III
  world frame).
- **Model evidence:** the variant's `model.sdf`, airframe 99997 and
  `radar/PROVENANCE.json` by SHA-256 at a Gazebo-simulation-assets commit,
  instead of powerline_slam's recorded `model.sdf` check.
- **Calibration and IMU noise:** `powerline_slam_runtime_inputs.py
  calibration` (III mounts, Radar-U on the III simulation mount) and
  `imu-covariance --ground`.
- **Results and estimator:** new development result roots; the estimator from
  the Python 3.12 development build (`setup_powerline_slam_estimator_env.sh`),
  not the frozen candidate-v13 runtime.

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
- **Time:** adopt the clock contract above, which pairs PX4 time with
  simulation time through Gazebo's IMU samples without changing PX4;
  instrumenting PX4 for the powerline clock-contract proof stays a fallback
  and needs approval.
- **Output:** a new topic next to `/perception/pl_mapper/powerline` instead of
  replacing it, with no consumer switched until the estimator is evaluated
  live.
- **Heading:** fixed for both layouts (finding 1); PX4's heading stays within
  0.007 rad of truth throughout the flights (`live11`), well inside the
  0.03 rad mission-prior sigma.
- **Hardware:** map Radar-U and Radar-F to the physical mounts later.

## Changes beyond the evaluation layout

These fix III behaviour outside the evaluation layout. All but the terminal
tracking change were merged into `deployment-infrastructure-redesign` on
2026-10-05 (superproject `d6747aa`, `7c351f9`, `ede109b` and `65d2a0d`); the
terminal tracking change stays on `powerline-slam`:

- **Simulated magnetometer** (finding 1), one set: PX4-Autopilot `eb14199e4e`
  (ekf2 keeps `EKF2_MAG_DECL`), `3d55fd979d` (`gz_bridge` reads the true field)
  and the airframe commit; Gazebo-simulation-assets' general commit (world flag
  and the production airframe) and III-Drone-Simulation's general commit (bump
  and smoke test). The `gz_bridge` mapping needs worlds with
  `use_earth_frame_ned` false, and the assets airframe must match its PX4 ROMFS
  copy. The ekf2 change only matters when `EKF2_DECL_TYPE` saves without using
  the model's declination, so the default (3) on real vehicles is unaffected.
- **Resting spawn** (finding 1), one set: PX4-Autopilot `9bba79da09`
  (airframe 99999's ROMFS copy), Gazebo-simulation-assets `ea7b7c5`
  (production and paper airframes) and III-Drone-Simulation `a28fb7b` (bump
  and smoke test). The spawn pose applies only in `hca_full_pylon_setup` and
  yields to an explicit `PX4_GZ_MODEL_POSE`.
- **Terminal tracking** (finding 2), III-Drone-Core: changes how closely every
  terminal hold holds its target in real flight (up to 2 cm plus a 1 cm step,
  well inside the 0.10–0.15 m arrival tolerances), verified only in
  simulation.
- **Simulation tools**: selecting the PX4 model (`--sim-model`) and rebuilding
  PX4 for a stale airframe without tripping on unset variables.
- **III-Drone-Core**: the stale `hough-accel-ip` submodule entry is removed.

## Follow-ups outside the SLAM integration

- **Field recordings:** inspection bags on the vehicle do not record the SLAM
  inputs (IMU, forward radar, camera). Which of them to add is open for the
  hardware phase: the recorder costs about 0.4 ms per message on the Pi, and
  upstream drops high-rate streams for that reason.
- **Large messages over UDP:** with III's UDP-only graph and the kernel's
  default 212 KB socket buffer, every subscriber of the raw camera image
  (0.9 MB) overflowed its socket during each frame's burst and relied on
  retransmission (the recorder dropped about 50 datagrams per second). Fast
  DDS 2.14 keeps the kernel default (`receiveBufferSize` 0), so the default,
  not only the maximum, has to grow. This workstation now has 8 MiB
  (`live10`: no drops); other hosts that carry III's raw images over UDP, such
  as the HIL Pi, still have the kernel default.

## Reproducing

```bash
# Inside the devcontainer, disconnected from its network:
III_POWERLINE_RUN_ID=powerline_slam_live10 scripts/workspace/run_isolated_powerline_slam_flights.sh
R=datasets/powerline_slam/powerline_slam_live10
python3 scripts/workspace/analyze_powerline_slam_flight.py $R/a_to_b/bag --mission-evidence $R/a_to_b/mission_phase_evidence.json
python3 scripts/workspace/powerline_slam_runtime_inputs.py imu-covariance --ground $R/ground_imu --output imu_gyro_covariance.json
# With network access, against a powerline_slam checkout:
scripts/workspace/setup_powerline_slam_estimator_env.sh --powerline-root PATH
```
