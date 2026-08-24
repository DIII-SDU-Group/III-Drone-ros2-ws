# Perception Dataset Flight Suite

`scripts/workspace/perception_dataset_flights.py` records deterministic Gazebo/PX4 flights for offline cable-perception development. Each bag uses the script's exact 28-topic contract, including `/fmu/out/vehicle_imu` and `/fmu/out/sensor_combined`: camera info and trajectory setpoints are intentionally excluded, and PX4 vehicle status uses `/fmu/out/vehicle_status_v1`.

## Run

Use the canonical devcontainer environment and keep the existing simulator/system available after collection:

```bash
source /home/iii/ws/setup/setup_dev.bash
cd /home/iii/ws
export PYTHONPATH=/home/iii/ws/tools/III-Drone-MCP:${PYTHONPATH}

# Inspect the immutable topic/flight catalog without ROS activity.
python3 scripts/workspace/perception_dataset_flights.py --list

# Record all B01-B18 flights once.
python3 scripts/workspace/perception_dataset_flights.py \
  --run-id perception_dataset_YYYYMMDD \
  --keep-running

# Record or retry a subset. A passing scenario is skipped when resuming.
python3 scripts/workspace/perception_dataset_flights.py \
  --run-id perception_dataset_YYYYMMDD \
  --scenario B03 --scenario B16 --resume --keep-running

# Recheck bags, coherence reports, plots, and contact sheets without flying.
python3 scripts/workspace/perception_dataset_flights.py \
  --verify-run datasets/perception_pipeline/perception_dataset_YYYYMMDD
```

The runner uses the canonical III system, arms/takes off through PX4 when needed, activates Custom Operation, resets the PL mapper for every bag, prepositions before recording, and applies a slow, nominal, or fast motion profile. Recording begins before pre-roll and ends after post-roll. Every flight goal is awaited. Failure recovery stops the bag, cancels active operations, and requests PX4 hold. Original motion parameters are restored at suite exit.

## Artifact layout

The default root is `datasets/perception_pipeline/<run-id>/`:

```text
manifest.json
manifest.csv
aggregate_verification.json
contact_sheets/
  all_topdown.png
  all_side.png
  all_observability.png
B16_full_partial_full/
  bag/metadata.yaml
  bag/*.mcap
  scenario_manifest.json
  ground_truth.json
  resolved_waypoints.json
  trajectory.json
  verification.json
  plots/
    trajectory_topdown.png
    trajectory_side.png
    trajectory_conductor_clearance.png
    observability_timeline.png
```

Each `ground_truth.json` contains the complete four-conductor polylines and pylon geometry mapped from immutable Gazebo truth into the live ROS `world` frame, the mapping inputs, source path/checksum, frame conventions, camera/mmWave FOV/rate/noise descriptions, and the scenario's planned waypoints. `trajectory.json` stores sampled `world -> drone` poses plus corridor membership, nearest-conductor clearance, upward-camera observability, and current PL mapper line count. Conductors below the upward-pointing camera are classified as not visible.

`verification.json` requires exact bag topics with nonzero messages, finite and dense trajectory samples, target achievement, at least 0.5 m conductor clearance, motion/hover intent, the scenario corridor policy, plot integrity, and ordered measured observability targets. B16 is one uninterrupted bag with an early full view, a middle partial top-pair view, and a later full view. The run manifest records the visual-review verdict and notes for every top-down, side, and observability plot.

## Focused test

Only the workspace-owned dataset tooling is exercised:

```bash
source /home/iii/ws/setup/setup_dev.bash
cd /home/iii/ws
python3 -m unittest -v scripts/workspace/test_perception_dataset_flights.py
```
