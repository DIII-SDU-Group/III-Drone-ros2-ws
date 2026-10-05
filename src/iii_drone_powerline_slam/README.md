# iii_drone_powerline_slam

Lifecycle-managed powerline SLAM perception backend for the simulation profile.
`powerline_slam_node` runs the pinned `powerline_slam` r26 development estimator
on III-native sensor streams. It publishes a passive `iii_drone_interfaces/Powerline`
output under its own namespace. The node runs **beside** the legacy
`hough_transformer → pl_dir_computer ↔ pl_mapper` stack and never replaces or
modifies it. Supervision instantiates one of the two stacks per system launch,
selected by `/perception/processing_stack` (see
`docs/powerline-slam-integration.md`). No mission, maneuver or overview consumer
reads this output.

| Item | Value |
|---|---|
| Node | `/perception/powerline_slam/powerline_slam` (rclpy lifecycle node) |
| Supervision entity | `powerline_slam`, SIM profile only, sensor layout `d4s_dc_drone_powerline_eval`, active-depends on `tf` and `sim_assets` |
| Estimator | the `powerline_slam` checkout pinned by `deps/powerline-slam.json`: `corridor_simulation/tools/iii_r1_pipeline.IncrementalPipeline`, the same code path as the offline III replay |
| Contracts | `III_POWERLINE_SLAM_CONTRACTS_v1` (input, clock, processing, selector, output and lifecycle) and its amendments A1–A4, in the powerline_slam provenance root `corridor_simulation/provenance/WO-2026-10-05-001/` |

## Environment

The executable is a shell wrapper. It sources
`.cache/powerline_slam_estimator/env.sh`, which provides the estimator's Python
3.12 environment: the v13-derived runtime, the GTSAM 4.2 fixed-lag native
modules, CPU torch and the pinned checkout. It then starts
`python -m iii_drone_powerline_slam.node`. Prepare the environment with:

1. On the host, `scripts/workspace/sync_powerline_slam_checkout.sh
   --powerline-source <powerline_slam checkout>`. This checks out the pinned
   commit, stages the git-ignored GTSAM fixed-lag sources and materializes the
   v13-derived runtime, verified against its binding.
2. In the devcontainer, `scripts/workspace/setup_powerline_slam_estimator_env.sh
   --powerline-root .cache/powerline_slam_checkout --v13-runtime`. This builds
   the native modules and writes `env.sh` and `NATIVE_BUILD.json`.

The wrapper exits with code 78 when the environment is missing.

## Parameters

| Parameter | Type | Meaning |
|---|---|---|
| `/perception/powerline_slam/runtime_config` | string, constant, default `none` | Path of the runtime configuration JSON (`iii.powerline-slam-runtime-config/v1`). With `none`, configure fails. |

The runtime configuration names:

- the calibration set;
- the map/mission priors;
- the ground-segment gyro covariance;
- the locked estimator configuration;
- the pylon checkpoint;
- the estimator epoch and the torch thread count.

Its optional `node` section holds development settings:

- `prefetch_workers`: camera detector / pylon-mask worker processes, default 0;
- `doppler_workers`: Radar-F window-solve worker processes, default 0;
- `output_dir`: where `flush_and_finalize` writes the replay report layout;
- `ledger`: whether the per-frame parity ledger is kept;
- `stall_timeout_s`: the stream-absence timeout, default 2 s;
- `flush_timeout_s`.

## Interfaces

Subscriptions (active state only):

- the six runtime inputs of `III_NATIVE_INPUT_CONTRACT_v1`:
  - Radar-U and Radar-F `points_full`;
  - `/sensor/cable_camera/image_raw`;
  - `/fmu/out/sensor_combined`, `/fmu/out/vehicle_odometry` and
    `/fmu/out/vehicle_local_position`;
- `/sensor/cable_camera/camera_info`, a contract check against the
  calibration set, never an estimator input;
- `/fmu/out/timesync_status`: any message fails the node closed, because the
  lockstep identity clock requires `UXRCE_DDS_SYNCT=0`.

No `/simulation/…`, `/tf` or other `/perception/…` topic is read.

## Processes

The ROS process only moves data: raw subscriptions, per-topic numbering and an
unbounded, order-preserving forwarder. The estimator host process (started at
configure) runs the pinned checkout's `iii_r1_pipeline.IncrementalPipeline` and
`iii_r1_frames.FrameConverter`, and returns frames, runtime records and replies
for publication. The estimator never shares the ROS interpreter. When it ran in
a thread of the ROS process it held the interpreter lock, and the executor lost
most best-effort PX4 samples at sensor rates. The host exits at cleanup or
shutdown, and also when the node process disappears (end of file on its input
pipe).

Publications (lifecycle publishers, under the node namespace):

| Topic | Type | Content |
|---|---|---|
| `powerline` | `iii_drone_interfaces/Powerline` | Per processed frame, stamped with the frame's source time. Contains confirmed conductors only, with stable ids, in the `drone` frame. When the estimator is fail-closed, the message has no lines. |
| `diagnostics` | `iii_drone_interfaces/StringStamped` | Per frame, a compact JSON with: local health/authority, corridor/global state, pylon anchors, local-frame generation, continuity guard, bridge, accounting and the fail-closed reason. Once per second, a `runtime` record with: counters, inbox/backlog high-water marks, latency percentiles and prefetch statistics. |
| `state` | `iii_drone_interfaces/StringStamped` | `Idle`, `Waiting`, `Running` or `FailClosed`. |

Service `flush_and_finalize` (`std_srvs/Trigger`, development/evaluation) does
the following:

1. closes the input;
2. processes every buffered event;
3. runs the single post-traversal finalization;
4. writes the replay report layout and the streams to `output_dir`.

## Lifecycle

- **configure**:
  - starts the estimator host, which activates the estimator runtime (verified
    against its manifest and native build record) and validates the runtime
    configuration and every path it names;
  - creates the publishers and the flush service;
  - does no sensor processing.
- **activate**:
  - starts a fresh processing epoch in the host (a new pipeline, its worker
    pools started);
  - creates the subscriptions.
- **deactivate**:
  - destroys the subscriptions;
  - ends the epoch;
  - counts and discards unforwarded and unprocessed events;
  - releases the pipeline.

  A later activate never reuses state. If the host dies, the node exits
  (code 71) and supervision respawns a clean one.
- **cleanup / shutdown**: the host exits, then the publishers and the service
  are released.

Processing is causal in source time. The host feeds messages to the pipeline in
arrival order. Source-time watermarks, not arrival order or call boundaries,
decide when a camera/Radar-U event is processed. Receipt time is used only for
the latency diagnostics and for stream-absence detection (amendment A4, measured
on the arrival clock of the ingested input).

## Tests

`colcon test --packages-select iii_drone_powerline_slam` runs the message and
lifecycle unit tests. The lifecycle tests run a real estimator host process
with a stand-in pipeline and need no estimator environment. The estimator-level
checks are in the powerline_slam provenance of WO-2026-10-05-001:

- controlled-replay determinism;
- online/offline parity on the immutable `powerline_slam_live11` bags.
