# Perception dataset ground truth

The simulation dataset recorder uses `scripts/workspace/perception_dataset_flights.py` and records an exact topic allowlist. Ground truth is published directly from Gazebo entity state and the same canonical conductor geometry consumed by the synthetic sensor plugin. It does not depend on PX4, EKF, odometry estimates, or perception outputs.

## Physical conductor identity

`Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml` is the canonical geometry and ID source. Its deterministic mapping is:

| Mask value | Physical ID |
|---:|---|
| 0 | background / no physical conductor |
| 1 | `conductor_1` |
| 2 | `conductor_2` |
| 3 | `conductor_3` |
| 4 | `conductor_4` |

`/simulation/ground_truth/conductors/geometry` (`iii_drone_interfaces/msg/StaticConductorGeometry`) records every exact world-frame centerline sample and physical radius. No estimator-specific slope, sag, local-Z, or bundle-frame quantity is stored. `/simulation/ground_truth/conductor_id_map` is a compact JSON compatibility mapping.

## Drone state

`/simulation/ground_truth/drone/state` uses `iii_drone_interfaces/msg/SimulatorDroneState` at 100 Hz:

- `header.stamp`: simulator time;
- `header.frame_id`: `world`, the Gazebo world / ROS ENU frame;
- `source_model_name` and `source_link_name`: exact Gazebo entities (`base_link` for the drone state);
- `pose_world`: position and orientation in `world`;
- quaternion fields use ROS order `x, y, z, w`;
- `twist_world.linear` and `twist_world.angular`: both expressed in `world`.

The compatibility topic `/simulation/ground_truth/drone/odometry` remains recorded, but the typed state topic above is the authoritative dataset contract.

## Radar truth

`/sensor/mmwave/points_full` contains measured XYZ, radial velocity, SNR, and noise. Every scan has one same-timestamp `/simulation/ground_truth/mmwave/scan` (`RadarScanGroundTruth`). `scan_sequence` is monotonic within the simulator process and each `points[i].source_point_index == i`, preserving exact `PointCloud2` ordering.

Each `RadarPointSource` contains:

- `source_class`: `VALID_PHYSICAL_CONDUCTOR=1`, `PHANTOM=2`, or `CLUTTER_NO_PHYSICAL_SOURCE=3`;
- `physical_conductor_id`: canonical ID for class 1, deliberately empty for explicitly classified nonphysical returns;
- `ideal_generating_point_{world,sensor}`: the exact point selected by the radar generator before along/cross/normal measurement noise;
- `nearest_physical_point_{world,sensor}`: the geometrically nearest canonical centerline point;
- `generating_point_equals_nearest_point`: states whether those concepts coincide for this simulation model.

The current generator produces physical conductor returns only. The nonphysical classes are reserved and enforced by validation so future phantom/clutter generation cannot silently lose provenance. `/simulation/ground_truth/mmwave/conductor_labels` remains as a compact point-aligned `PointCloud2` compatibility topic.

## Camera truth

Each `/sensor/cable_camera/image_raw` frame triggers, using the exact same simulator timestamp:

- `/simulation/ground_truth/cable_camera/conductor_instance_mask` (`sensor_msgs/msg/Image`, `mono16`, same dimensions);
- `/simulation/ground_truth/cable_camera/frame` (`CameraFrameGroundTruth`).

The frame message lists every physical conductor, not only visible ones. State is `OUTSIDE_FOV`, `NO_VISIBLE_PIXELS`, or `VISIBLE`, with mask value, visible pixel count, and an in-bounds bounding box when visible. The instance mask analytically rasterizes the exact canonical centerlines with physical radius and z-buffers conductors against each other. It does **not** currently test occlusion by non-conductor rendered meshes such as pylons or the vehicle; this is the remaining distinction from a true renderer instance pass.

## Recording and validation

Start the existing isolated cohort recorder:

```bash
docker exec -u iii <devcontainer> bash -lc '
  cd /home/iii/ws
  ./scripts/workspace/run_isolated_perception_dataset.sh
'
```

For a selected flight, append `--scenario B01`. The recorder starts PL mapper and records its exact 37-topic contract.

Inspect a completed scenario bag:

```bash
docker exec -u iii <devcontainer> bash -lc '
  source /opt/ros/jazzy/setup.bash
  source /home/iii/ws/install/setup.bash
  python3 /home/iii/ws/scripts/workspace/inspect_perception_ground_truth.py \
    /home/iii/ws/datasets/perception_pipeline/<run>/<scenario>/bag
'
```

The inspector reports all topic names, types, and counts; drone-GT rate and numeric validity; radar scan/truth alignment, point counts, source classes and per-ID counts; camera image/mask/frame alignment and per-ID visible-frame counts; and canonical ID consistency. It exits nonzero on an invariant failure.
