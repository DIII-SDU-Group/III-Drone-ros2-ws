# Simulated Mmwave Conductor Sensor Plan

## Goal

Replace the current depth-camera-based mmwave proxy with a simulated sensor that emits sparse conductor detections closer to the real radar behavior.

Target behavior:
- approximately one stable point per visible conductor
- no output when the sensor measurement plane does not overlap a finite conductor span
- noise aligned with the conductor span, plus smaller noise in the other axes
- sparse `PointCloud2` output on `/sensor/mmwave/points` so downstream perception does not need to change

## Proposed Approach

Implement a new Gazebo-side simulated mmwave sensor attached to the drone.

Preferred first implementation:
- a Gazebo model/system plugin attached to the drone model
- functionally acts as a sensor
- simpler than introducing a brand-new low-level Gazebo sensor type

At runtime the plugin should:
1. Load a set of explicit conductor spans.
2. Transform those spans into the sensor frame.
3. Intersect each finite conductor span with a thin slab around the sensor `xy` plane.
4. Reject conductors with no overlap on the actual span.
5. Emit at most one point per conductor.
6. Add noise in a conductor-aligned basis.
7. Publish sparse `sensor_msgs/msg/PointCloud2` detections.

## Why This Replaces The Current Sim Path

The current simulated mmwave path in `src/III-Drone-Simulation/src/depth_cam_to_mmwave.cpp` is a depth-camera proxy:
- it clusters a dense depth point cloud
- collapses each cluster to a centroid
- adds ad hoc XYZ noise
- manually remaps axes instead of using the real transform

That is the wrong abstraction level for the real radar, which already publishes sparse XYZ detections.

The new path should aim directly at sparse conductor detections, not at a radar-like surface reconstruction.

## Measurement Model

For each conductor:
1. Transform the conductor geometry into the sensor frame.
2. Apply range and field-of-view gating.
3. Intersect the finite span with a thin slab around the local `xy` plane.
4. If there is no overlap, publish nothing for that conductor.
5. Choose one representative point from the overlap region.
6. Add noise.

Recommended detail:
- use a thin slab such as `abs(z_sensor) < epsilon`, not an exact zero-thickness plane
- exact plane intersection is more numerically fragile

Recommended noise basis:
- `t`: tangent along conductor span
- `n1`: lateral cross-span direction
- `n2`: normal or out-of-plane direction

Recommended noise model:
- larger noise along span than across span
- smaller noise out of plane
- optional range-dependent scaling
- optional dropout probability
- optional sparse clutter later

Initial parameter set to expose:
- `update_rate_hz`
- `max_range_m`
- `view_cone_slope`
- `plane_half_thickness_m`
- `sigma_along_m`
- `sigma_cross_m`
- `sigma_normal_m`
- `dropout_probability`
- `conductor_asset_path`

## Implementation Breakdown

### 1. Conductor Asset Extraction

Create an offline extractor that turns the world mesh into explicit conductor geometry.

New script:
- `src/III-Drone-Simulation/scripts/extract_conductors_from_dae.py`

New checked-in asset:
- `src/III-Drone-Simulation/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/conductors.yaml`

Recommended asset format:

```yaml
frame_id: world
conductors:
  - id: phase_1
    samples:
      - [x, y, z]
      - [x, y, z]
  - id: phase_2
    samples:
      - [x, y, z]
      - [x, y, z]
  - id: phase_3
    samples:
      - [x, y, z]
      - [x, y, z]
```

Recommendation:
- start with sampled polylines, not analytic conductor equations
- easier to inspect, trim, transform, and intersect robustly

### 2. Gazebo Plugin

Create a new Gazebo plugin that publishes the simulated mmwave detections.

New source:
- `src/III-Drone-Simulation/src/mmwave_conductor_sensor_plugin.cpp`

Possible helper headers:
- `src/III-Drone-Simulation/include/iii_drone_simulation/mmwave_conductor_sensor_plugin.hpp`
- `src/III-Drone-Simulation/include/iii_drone_simulation/conductor_model.hpp`

Responsibilities:
- load the conductor asset once
- resolve the sensor pose each update
- transform conductor samples into sensor frame
- find conductor-plane overlap
- generate sparse detections
- publish `/sensor/mmwave/points`

Build changes:
- extend `src/III-Drone-Simulation/CMakeLists.txt`
- extend `src/III-Drone-Simulation/package.xml`
- add required Gazebo Harmonic dependencies such as `gz-sim`, `gz-plugin`, and `gz-math`

### 3. Drone SDF Wiring

Attach the plugin to the simulated drone model.

Target file:
- `src/III-Drone-Simulation/Gazebo-simulation-assets/models/d4s_dc_drone/model.sdf`

The plugin should:
- use the drone-attached mmwave mounting pose
- publish in the same mmwave frame already used by the rest of the stack

### 4. Launch Wiring

Retire the current depth-camera mmwave proxy path.

Target file:
- `src/III-Drone-Simulation/launch/sim_assets.launch.py`

Changes:
- remove `depth_cam_to_mmwave`
- remove the depth point cloud bridge if nothing else needs it
- keep publishing on `/sensor/mmwave/points`

This preserves compatibility with downstream consumers such as `pl_mapper`.

### 5. Configuration

Add sim-specific mmwave parameters to the configuration system.

Likely files:
- `src/III-Drone-Configuration/config/parameters/parameter_manifest.yaml`
- `src/III-Drone-Configuration/config/parameter_sets/sim/tracked/default.yaml`

Keep the output topic and frame consistent with the existing real and simulated perception stack.

## Delivery Order

Recommended implementation order:
1. Build the offline conductor extractor.
2. Generate and inspect `conductors.yaml`.
3. Implement and test conductor-plane intersection logic outside Gazebo.
4. Implement the Gazebo plugin.
5. Attach it to the drone SDF.
6. Remove the old `depth_cam_to_mmwave` path.
7. Tune against recorded real radar behavior.

## Validation Plan

### Static Validation

With the drone stationary and conductors centered in view:
- expect approximately one stable point per visible conductor
- expect no detections when the sensor plane does not overlap the finite spans

### Motion Validation

Sweep the drone laterally and vertically through the corridor:
- points should appear and disappear according to span overlap and FOV logic
- detections should remain sparse and stable

### System Validation

Keep the downstream interface unchanged:
- `/sensor/mmwave/points`
- `sensor_msgs/msg/PointCloud2`

Then verify that:
- the perception stack behaves without new sim-only hacks
- mapped conductor behavior is closer to real logs

## Feasibility Findings From The Current World Assets

The current world model is usable for this plan, but not directly as clean Gazebo conductor entities.

### SDF-Level Structure

The pylon setup in `src/III-Drone-Simulation/Gazebo-simulation-assets/world_models/hcaa_pylon_setup/model.sdf` is a single SDF model with:
- one visual mesh
- one collision mesh

The world in `src/III-Drone-Simulation/Gazebo-simulation-assets/worlds/hca_full_pylon_setup.sdf` includes that model once at a fixed pose.

This means Gazebo itself does not expose separate conductor links or objects that can simply be queried as named entities.

### Mesh-Level Structure

The underlying Collada assets do contain a distinct conductor assembly:
- `V1_world_slimmer.dae` contains a `cableAssem` node
- `V1_world_collisions_a.dae` contains a `cableAssem-mesh` geometry

So the conductors are separable from the rest of the scene at the mesh-asset level.

### Extraction Findings

Inspection of `cableAssem` shows:
- it is one connected mesh, not separate named conductor objects
- its dominant span is about 34.5 meters
- cross-sections separate into about three stable lateral bands
- two of those bands fit sagging conductor behavior cleanly
- the third band appears contaminated by extra end geometry and likely needs trimming during extraction

Conclusion:
- direct runtime lookup of conductors from Gazebo is not the right approach
- offline extraction into explicit conductor spans is feasible and preferred

## Main Risks

- the extracted conductor mesh may need manual trimming in the first version
- exact zero-thickness plane intersection will be unstable, so use a slab
- if line-of-sight occlusion becomes important later, that should be added as a second-stage feature

## Recommendation

Proceed with:
1. offline conductor extraction from `cableAssem`
2. checked-in conductor span asset
3. drone-mounted Gazebo mmwave plugin that emits sparse finite-span conductor detections

Do not continue investing in the depth-camera clustering proxy if the goal is to match the real radar behavior.
