# Perception Dataset Flight Suite Backlog

## Context

Build and execute a resumable simulation flight suite that records exactly the agreed 26 ROS topics for offline perception development. Each named scenario folder must contain one rosbag, trajectory samples, ground-truth conductor geometry and sensor descriptions, verification results, and plots showing the drone path relative to every conductor. The run-level manifest must summarize scenario intent, motion profile, bag integrity, corridor coverage, visibility transitions, clearance, and artifact paths.

The suite will live in workspace-owned `scripts/workspace/` and reuse `iii_drone_mcp.agent_tools.DroneAgentTools` for canonical simulation/system lifecycle, configuration, Custom Operation flight, PL mapper control, rosbag recording, TF sampling, and safety recovery. Generated datasets default to `datasets/perception_pipeline/<run-id>/` and are not source-controlled.

Constraints and decisions:

- Record exactly the 26-topic list agreed in conversation; `/sensor/cable_camera/camera_info` and `/fmu/in/trajectory_setpoint` are excluded and vehicle status is `/fmu/out/vehicle_status_v1`.
- Execute one repetition of all 18 scenario types for this requested run; the script supports additional repetitions and resumption.
- Most scenarios remain laterally and longitudinally inside the conductor corridor, generally below the lowest conductor. High-altitude paths use known fixture geometry and enforce minimum conductor clearance.
- Scenario B16 is a single full → partial → full observability bag using `mid_corridor_taken_off_conductors_visible` → `high_corridor_in_flight_top_two_conductors_visible` → the low fixture.
- Rosbag contents are verified from `metadata.yaml`; every requested topic must be present and no unrequested topic may appear.
- Plot verification is both programmatic and visual: assert nonempty samples, expected displacement/hover, finite coordinates, conductor overlays, clearance, corridor coverage, and required visibility transitions; then inspect contact sheets containing every generated plot.
- No existing dirty worktree changes may be overwritten.

## Incomplete

## In-Progress

## Completed

### P2.T1: Inspect every plot and iterate until satisfied

Description:
Create contact sheets for all top-down, side, and observability plots; visually inspect every scenario, cross-check anomalies against trajectory/visibility metrics, rerun or adjust scenarios when plots do not match intent, and record final visual-review decisions in the run manifest.

Acceptance:
- [x] Every generated plot is visually reviewed.
- [x] Paths are coherent relative to conductor geometry and scenario intent.
- [x] Most flight samples are inside the corridor.
- [x] At least one bag contains verified full → partial → full observability.
- [x] Final manifest records reviewer verdict and notes for all scenarios.

Tests and implementation notes:
- Visually inspected all 54 plot tiles through the regenerated top-down, side, and observability contact sheets.
- Reran B01–B05 after detecting stale pre-mapping overlays; their final plots now use live ROS-world conductor geometry.
- All 18 scenario summaries contain a passing visual-review verdict and scenario-specific notes.
- Every scenario requiring majority corridor coverage passed that policy; B16 measured `full → partial → full` in one uninterrupted bag.
- Final aggregate verifier reports `success: true` for all 18 scenarios and all three contact sheets.

### P2.T0: Execute complete dataset flight suite

Description:
Run all 18 scenarios once against the active canonical simulation. Retry recoverable failures, retain only coherent completed attempts as canonical scenario outputs, and leave detailed failure artifacts for rejected attempts.

Acceptance:
- [ ] 18 named scenario folders contain valid exact-topic rosbags.
- [ ] All bags have ground truth, trajectory data, plots, and verification JSON.
- [ ] Run manifest reports success for all scenarios.

Tests:
- Suite final verifier exits zero.
- `ros2 bag info`/metadata validation passes for every bag.

Implementation notes:
- Recorded and verified all 18 canonical scenario folders.
- Aggregate verifier passes with exact 26-topic bags, ground truth, trajectories, and plots for every scenario.
- B16 contains measured full → partial → full observability in one bag.

### P1.T0: Add focused III tooling tests and documentation

Added `scripts/workspace/test_perception_dataset_flights.py` and `docs/perception-dataset-flight-suite.md`, linked from the documentation index. Coverage includes the exact topic/exclusion contract, all catalog invariants, majority-corridor policy, B16 ordering, manifest/ground-truth serialization, and positive/negative bag metadata validation.

Verification:
- Devcontainer `python3 -m unittest -v scripts/workspace/test_perception_dataset_flights.py`: 6/6 passed.

### P0.T2: Implement plots and automated coherence validation

Implemented conductor-overlay top-down/side/clearance plots, calibrated-target/model/mapper observability timeline, trajectory persistence, semantic coherence metrics, ordered-state validation, exact-bag verification, aggregate run verification, and three run-level contact sheets.

Verification:
- Live B01 plot run passed aggregate verification with all three required plot classes and exact bag topics.
- Visually inspected B01 top-down, side, and timeline contact sheets: stationary path is inside the corridor, below all four conductor curves, mapper line count remains four, and clearance remains ~1.65 m.
- Helper checks passed for exact-topic metadata and full → partial → full ordering.

### P0.T1: Implement canonical execution, recording, and recovery

Implemented canonical readiness checks, PX4 arm/takeoff, Custom Operation activation, live fixture resolution, constraint-safe motion profile application/restoration, mapper reset, pre-positioning, exact-topic rosbag lifecycle, continuous TF/geometry sampling, awaited maneuver goals, and hold/cancel/bag-stop recovery. Resume/filter/repetition paths share the same execution loop.

Verification:
- Live B01 completed in `datasets/perception_pipeline/validation_static_20260812e`.
- Its 24.41 s bag contains exactly 26 topics, no zero-message topics, and 26,449 total messages (218.5 MB).
- Its trajectory contains 88 samples, and all original motion parameters were restored successfully.
- A rejected below-minimum-altitude attempt and a live-configuration transition inconsistency were diagnosed; target altitude flooring and constraint-safe parameter ordering now prevent recurrence.

### P0.T0: Implement flight catalog and artifact schema

Implemented `scripts/workspace/perception_dataset_flights.py` with a validated exact 26-topic contract, deterministic B01–B18 catalog, slow/nominal/fast profiles, scenario selection/repetition/resume CLI surface, named artifact directories, JSON/CSV manifests, and complete conductor/pylon/sensor ground truth copied into each scenario. B16 explicitly records full → partial → full waypoints in one scenario.

Verification:
- `python3 -m py_compile scripts/workspace/perception_dataset_flights.py` passed.
- Host and devcontainer `--list` reported exactly 26 topics and all 18 scenarios.
- A dry run produced 18 scenario folders and 38 schema files (two per scenario plus run JSON/CSV).
