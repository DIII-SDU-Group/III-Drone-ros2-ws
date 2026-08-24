# Cable Landing Sharpening Findings

This file captures the four current findings we want to work from for improving cable landing reliability. It intentionally excludes the separate contact-point/frame-origin finding for now.

## Incomplete

## Step-by-step Fix Trial Plan

Apply these one at a time, sorted by implementation complexity from lowest to highest. After each step, run the same rendered cable landing scenario from the same start positions and record whether the miss rate, retry count, yaw behavior, and final gripper contact improve.

### Step 1. Lower close-range perception cutoff

Goal:

- Keep close conductor points available to `pl_mapper` during final cable landing.

Implementation:

- Change `/perception/pl_mapper/min_point_dist` to approximately `0.10 m` in sim tracked defaults.
- Apply the same intended value to real tracked defaults if acceptable for the real mmWave sensor.
- Review `/perception/pl_mapper/strict_min_point_dist`; keep it valid relative to the manifest constraint and decide whether strict FOV should also allow close cable points.

Validation:

- Start from a known good cable landing scenario.
- Confirm logs/plots show the target conductor remains freshly measured when close to the gripper/mmWave frame.
- Confirm lowering the cutoff does not introduce obvious near-field clutter or self-reflection false positives.

### Step 2. Fix Hough best-distance update

Goal:

- Remove order-dependent Hough line selection that can corrupt powerline yaw.

Implementation:

- In `HoughTransformer::getBestLineIndex()`, update `best_dist` whenever `best_idx` is updated.
- Add or run a deterministic regression check with multiple candidate Hough lines.

Validation:

- Confirm selected candidate is the closest-to-center candidate according to the implemented metric.
- Confirm powerline yaw is stable across repeated runs with the same image/candidate ordering.

### Step 3. Shape final capture reference instead of dropping position/yaw

Goal:

- Keep active lateral correction during final cable capture.

Implementation:

- Replace the current `truncateReferenceWithinSafetyZone()` behavior that returns `NaN` position/yaw near the cable.
- When inside the capture transition radius, project `target_world - current_world` onto:
  - conductor axis;
  - horizontal axis perpendicular to conductor;
  - vertical axis.
- Suppress or clamp the conductor-axis component.
- Preserve perpendicular-horizontal and vertical correction.
- Reconstruct a normal world-frame setpoint and publish that downstream.
- Keep controlled upward velocity, but combine it with the shaped position target.

Validation:

- Plot conductor position in the gripper yz-plane during final approach.
- Confirm the conductor converges toward the V-gate center instead of drifting laterally once inside the old truncate radius.
- Confirm the drone does not chase along-cable displacement near contact.

### Step 4. Shape cable landing target before MPC

Goal:

- Prevent the cable landing MPC from spending authority on irrelevant/noisy along-cable target error.

Implementation:

- Add a helper near `CableLandingManeuverServer::getUpdatedTargetReference()`.
- Decompose the target error using the matched conductor direction.
- Preserve current/start along-cable coordinate, or clamp along-cable movement to a small limit.
- Keep perpendicular-horizontal and vertical target components.
- Feed the reconstructed world-frame target into the existing `trajectory_mode_t::cable_landing` MPC path.
- Do not modify the generated MATLAB MPC solver in this step.

Validation:

- Add debug telemetry for along-cable, perpendicular-horizontal, and vertical target error before and after shaping.
- Confirm the MPC reference no longer moves meaningfully along the conductor in response to perception noise.
- Confirm cable landing accuracy improves or remains stable across varied start positions.

### Step 5. Add focused debug telemetry and plots

Goal:

- Make each trial diagnosable without ad hoc CLI or code.

Implementation:

- Log or publish:
  - selected Hough candidate distance and yaw;
  - target conductor freshness/source;
  - raw and shaped target error decomposition;
  - final capture gripper-frame conductor position;
  - latch/closed state at each retry.
- Extend the existing rosbag/plot workflow to compare attempts before and after each step.

Validation:

- For each failed or retried landing, the recorded data should show whether the issue came from perception freshness, yaw selection, target shaping, final lateral correction, or gripper latch/contact.

### 1. Final approach gives up lateral correction too early

Current behavior:

- `CableLandingManeuverServer::computeReference()` computes a target reference from the detected cable and sends it through the cable-landing trajectory generator.
- The result is then passed through `truncateReferenceWithinSafetyZone()`.
- Once `(state.position() - target_reference.position()).norm()` is within `/control/maneuver_controller/cable_landing_reference_truncate_radius`, currently `0.25 m`, the returned reference has `NaN` position and yaw but keeps velocity, acceleration, and yaw-rate fields.
- Practically, this means the final close-range phase stops actively commanding lateral position/yaw toward the cable target and mostly continues with the vertical/upward behavior.

Why this can cause misses:

- The gripper V-gate is much tighter than the `0.25 m` truncate radius. Current V-gate config uses `/control/maneuver_controller/cable_landing_gripper_v_gate_half_width_at_reference_z = 0.08`.
- A lateral error that is acceptable at the truncation radius can still be too large for the conductor to enter the gripper correctly.
- If the drone enters the truncate radius with residual lateral error, there is no explicit close-range correction loop to drive the cable into the center of the V-gate.

Code anchors:

- `src/III-Drone-Core/src/control/maneuver/cable_landing_maneuver_server.cpp`
- `CableLandingManeuverServer::computeReference()`
- `CableLandingManeuverServer::truncateReferenceWithinSafetyZone()`
- `/control/maneuver_controller/cable_landing_reference_truncate_radius`
- `/control/maneuver_controller/cable_landing_gripper_v_gate_*`

Suggested direction:

- Replace the final `NaN` truncation behavior with a close-range gripper-frame servo.
- In the close phase, ignore or de-weight along-cable error, keep correcting lateral gripper-frame error, and command a controlled upward velocity.
- Add explicit telemetry for final-phase lateral error, vertical error, yaw error, target freshness, and latch state.

Selected fix:

- Do not simply keep ordinary full 3D position control enabled unchanged all the way through the maneuver.
- The final reference is still published as a world-frame setpoint. The distinction between along-cable and perpendicular correction is made before publishing by projecting the world-frame error onto a cable-relative basis, shaping that error, and then reconstructing a world-frame target.
- In practice:
  - derive `cable_axis_world` from the matched conductor direction;
  - derive `perpendicular_horizontal_world` as the horizontal axis orthogonal to `cable_axis_world`;
  - decompose `error_world = target_world - current_world` into cable-axis, perpendicular-horizontal, and vertical components;
  - clamp or suppress the cable-axis component;
  - keep the perpendicular-horizontal and vertical components;
  - reconstruct `shaped_target_world = current_world + shaped_error_world`.
- Do keep position correction active all the way through in the axes that matter for capture:
  - horizontal axis perpendicular to the conductor;
  - vertical closure relative to the conductor/gripper;
  - yaw-axis alignment using the undirected conductor direction.
- Suppress or clamp along-cable position correction in the final capture phase. Along-cable error does not determine whether the conductor enters the gripper, and chasing it can inject unnecessary motion.
- Replace `truncateReferenceWithinSafetyZone()` with a capture-phase reference shaper instead of returning `NaN` position/yaw. The shaped reference should continue to publish a valid position reference for the perpendicular and vertical components while allowing the along-cable component to remain current/start/clamped.
- Keep the controlled upward velocity behavior, but combine it with active lateral correction rather than switching to velocity-only behavior near the cable.
- Add a parameterized transition distance for entering this capture-phase controller. The existing truncate radius can be reused initially, but its meaning should become "start gripper-frame capture servo" rather than "drop position/yaw reference."
- This is the fix to implement for item 1 before considering larger MPC changes.

Acceptance criteria:

- Inside the current truncate radius, the cable landing controller still actively reduces gripper-frame lateral error.
- A rosbag/plot can show conductor position in gripper yz-plane converging toward the V-gate center during the final phase.
- Final approach does not rely on retry luck when entering the last 25 cm with small but nonzero lateral error.

### 2. Perception can lose or bias the cable exactly when close

Current behavior:

- `pl_mapper` consumes mmWave point clouds only while running.
- Each point is converted into a temporary `SingleLine` and filtered by `SingleLine::IsInFOV()`.
- In sim config, `/perception/pl_mapper/min_point_dist` is currently `0.99` and `/perception/pl_mapper/strict_min_point_dist` is `1.0`.
- During cable landing, the target conductor is intentionally brought close to the gripper/mmWave frame. Near the final phase, this can put relevant points near or inside the minimum distance cutoff.
- `Powerline::Predict()` continues to predict existing line positions from drone motion, and inter-line positions can overwrite non-FOV line positions when `/perception/pl_mapper/overwrite_non_FOV_line_positions_from_inter_pos` is true.

Why this can cause misses:

- The controller needs the most accurate cable estimate during the final close-range phase.
- If fresh close cable points are filtered out by min-distance/FOV logic, the target can be predicted rather than measured exactly when precision matters most.
- Predicted/inter-line-updated positions can be good enough for approach, but biased by odometry, direction estimate, frame offsets, or stale geometry during contact.
- The line can still appear valid and visible enough for control while the actual conductor in the gripper frame is offset.

Code anchors:

- `src/III-Drone-Core/src/perception/pl_mapper_node/pl_mapper_node.cpp`
- `PowerlineMapperNode::mmWaveCallback()`
- `src/III-Drone-Core/src/perception/single_line.cpp`
- `SingleLine::IsInFOV()`
- `SingleLine::IsInFOVStrict()`
- `src/III-Drone-Core/src/perception/powerline.cpp`
- `Powerline::Predict()`
- `Powerline::UpdateNonFOVLines()`
- `/perception/pl_mapper/min_point_dist`
- `/perception/pl_mapper/strict_min_point_dist`
- `/perception/pl_mapper/use_inter_line_positions`
- `/perception/pl_mapper/overwrite_non_FOV_line_positions_from_inter_pos`

Suggested direction:

- Reduce the near-distance cutoff so the target conductor can remain measured closer to the mmWave sensor during cable landing.
- Publish target freshness/age and whether each line was updated by direct measurement, prediction, or inter-line inference.
- Consider freezing line identity while still allowing close-range target position updates for the matched target conductor.

Selected fix:

- Start with a direct parameter change rather than a new landing-specific perception mode.
- Reduce `/perception/pl_mapper/min_point_dist` from the current sim value of `0.99 m` to approximately `0.10 m`.
- Apply the same intent to the tracked real default if the sensor behavior supports it; current real default is `0.5 m`, which can still reject useful close-range conductor points.
- Keep `/perception/pl_mapper/strict_min_point_dist` consistent with the manifest constraint that it must be greater than or equal to `/perception/pl_mapper/min_point_dist`. It can remain more conservative if strict FOV is being used for line visibility/liveness, but it should be reviewed because `SingleLine::IsInFOVStrict()` also uses this near-distance gate.
- This change is low risk architecturally because `SingleLine::IsInFOV()` applies the cutoff directly as `position.norm() >= min_point_dist` in the mmWave frame; lowering it does not require mapper algorithm changes.
- Validation should confirm that close conductor points are not filtered during the final cable landing phase and that lowering the cutoff does not introduce obvious near-field clutter or self-reflection false positives.

Acceptance criteria:

- During the last cable-landing phase, target cable estimate remains based on fresh measurements whenever the conductor is physically visible to the sensor.
- Logs/diagnostic topic distinguish measured target updates from predicted/inter-line updates.
- Miss analysis can tell whether a failure came from stale perception, biased perception, or control error.

### 3. Hough line selection has a likely bug

Current behavior:

- `HoughTransformer::getBestLineIndex()` loops over Hough lines and computes distance from each candidate image line to the image center.
- It initializes `best_dist` to a large value and updates `best_idx` if `dist < best_dist`.
- The current implementation does not update `best_dist` inside that branch.

Why this can cause misses:

- Without updating `best_dist`, the selected line can become the last candidate that beats the initial large value, not the closest-to-center candidate.
- That can corrupt estimated powerline yaw.
- Powerline yaw feeds `pl_dir_computer`, `pl_mapper` projection plane, target matching, gripper alignment yaw, and cable-axis safety checks.
- Even if the system often works, this makes direction estimate selection fragile and image-order dependent.

Code anchors:

- `src/III-Drone-Core/src/perception/hough_transformer.cpp`
- `HoughTransformer::GetHoughLines()`
- `HoughTransformer::ComputeAngle()`
- `HoughTransformer::getBestLineIndex()`

Suggested direction:

- Fix `getBestLineIndex()` so `best_dist = dist` is updated with `best_idx`.
- Add a small unit test or deterministic image/candidate test for line selection.
- Longer term: replace single-line selection with robust aggregation such as weighted median orientation or RANSAC over multiple Hough candidates.

Selected fix:

- Implement the minimal Hough selection bug fix first.
- In `HoughTransformer::getBestLineIndex()`, when `dist < best_dist`, update both:
  - `best_idx = i`;
  - `best_dist = dist`.
- Add or run a deterministic regression check proving that, given multiple candidate Hough lines, the selected candidate is the one with the smallest center-distance metric.
- Do not change the broader Hough strategy in this sweep unless the minimal fix still leaves unstable yaw selection.

Acceptance criteria:

- Given multiple Hough candidates, the selected candidate is the one closest to the image center under the implemented metric.
- Powerline yaw no longer depends on candidate ordering except through the intended metric.
- A regression test catches the missing `best_dist` update.

### 4. Cable landing MPC likely chases dimensions that do not matter enough

Current behavior:

- Cable landing computes a world-frame target reference with position and yaw.
- The trajectory generator uses `trajectory_mode_t::cable_landing` and cable-landing MPC parameters.
- The cable landing safety checks now de-emphasize along-cable error in some places, but the target reference itself is still a full 3D position/yaw objective.
- Along-cable position is not critical for successful gripper capture; perpendicular-to-cable lateral error, vertical closure, and yaw-axis alignment are critical.
- The current generated MATLAB MPC interface exposes diagonal weight vectors only: `weights.y[6]` for `[x, y, z, vx, vy, vz]`, `weights.u[3]` for acceleration effort, and `weights.du[3]` for jerk/input-rate effort.
- The III wrapper currently fills those vectors from scalar config parameters such as `/control/trajectory_generator/cable_landing_MPC_wx`, `wy`, `wz`, `wvx`, `wvy`, and `wvz`.

Why this can cause misses:

- If the MPC spends effort reducing along-cable error or following noisy along-cable target motion, that authority is not being used for the lateral/vertical contact problem.
- Perception noise along the conductor should not cause meaningful drone motion.
- The final success condition is physical capture by the gripper, not exact world-position convergence.
- A full-position objective can introduce unnecessary lateral/yaw motion close to the cable if the detected line pose shifts.

Code anchors:

- `src/III-Drone-Core/src/control/maneuver/cable_landing_maneuver_server.cpp`
- `CableLandingManeuverServer::computeReference()`
- `CableLandingManeuverServer::getUpdatedTargetReference()`
- `src/III-Drone-Core/src/control/trajectory_generator.cpp`
- `TrajectoryGenerator::setupMPC()`
- `TrajectoryGenerator::stepMPC()`
- `trajectory_mode_t::cable_landing`
- `/control/trajectory_generator/cable_landing_MPC_*`
- `src/III-Drone-Core/src/extern/matlab_MPC_5Hz_hp10/mpcmoveCodeGeneration_types.h`
- `pos_MPC::struct7_T`

Feasibility note:

- A true powerline-orientation-aware quadratic cost in world coordinates needs a rotated/dense position weight matrix, for example high weight on the horizontal axis perpendicular to the conductor and low weight along the conductor.
- The current generated solver path does not expose a dense `Q` matrix or cross terms. It exposes only diagonal output weights, so dynamic dense weight construction is not directly available without changing/regenerating the MPC interface.
- A world-axis-only approximation is possible by recomputing effective `wx/wy` from conductor yaw, but it drops the cross term of the rotated quadratic. It is only exact when the conductor axes are aligned with the world axes, so it is not the preferred fix.
- The most feasible near-term approach is to precondition the target before it enters MPC: decompose cable landing error into along-cable, horizontal-perpendicular-to-cable, and vertical components, then suppress or clamp the along-cable component. This keeps the existing MPC unchanged while preventing it from chasing irrelevant along-cable motion.
- The stronger medium-term approach is a cable-frame MPC wrapper: transform state, velocity, target, and bounds into a local frame whose x-axis is the conductor direction, run the existing diagonal-weight MPC with low local-x weight and high local-y/z weights, then transform the planned trajectory/reference back to world. This gives the desired rotated weighting without requiring a dense solver, but it must be validated carefully because acceleration and velocity limits are also currently axis-specific.
- The full solver-level approach is to regenerate or replace the MPC so it accepts a parameterized dense output weighting matrix, or accepts an online frame transform as part of the generated model. That is feasible but much heavier than the cable landing fixes we need first.

Suggested direction:

- Make cable landing control explicitly operate in a cable/gripper-relative frame for the final phase.
- Ignore or strongly de-weight along-cable error.
- Prioritize gripper-frame lateral error, vertical closure rate, and yaw-axis alignment.
- Start with target preconditioning in `CableLandingManeuverServer::getUpdatedTargetReference()` or a small helper close to it: preserve the current/start along-cable coordinate while still commanding the detected conductor's perpendicular and vertical contact geometry.
- If target preconditioning is insufficient, add the cable-frame MPC wrapper as a dedicated `trajectory_mode_t::cable_landing` path instead of trying to mutate world-axis weights every tick.
- Consider a two-phase controller:
  - acquisition phase: get under the target and align yaw;
  - capture phase: slow upward motion with continuous lateral gripper-frame correction.
- Add MPC/reference telemetry that reports error decomposition as along-cable, perpendicular-to-cable, vertical, yaw-axis, and yaw-rate.

Suggested near-term fix:

- Implement this outside the generated MPC implementation.
- In the cable landing maneuver layer, shape the target before it is passed to the trajectory generator/MPC.
- Add a helper near `CableLandingManeuverServer::getUpdatedTargetReference()` that decomposes the target error using the matched conductor direction:
  - `along`: axis parallel to the conductor;
  - `perpendicular_horizontal`: horizontal axis orthogonal to the conductor;
  - `vertical`: world/ROS vertical axis.
- Suppress or clamp the `along` component so cable landing does not chase noisy displacement along the conductor.
- Preserve active correction in `perpendicular_horizontal` and `vertical`, because those are the dimensions that determine whether the conductor enters the gripper.
- Keep yaw alignment using the existing undirected cable-axis convention, where gripper x-axis may align with either positive or negative conductor direction.
- Feed the resulting shaped world-frame target into the existing `trajectory_mode_t::cable_landing` MPC path.
- Do not modify the generated MATLAB MPC solver for this first fix.
- Do not add dense weight matrices for this first fix.
- Do not dynamically rewrite `wx/wy/wz` as a substitute for rotated weights; that is only an approximation and loses the cross term.

Acceptance criteria:

- Cable landing control objective explicitly separates along-cable error from perpendicular and vertical error.
- Close-range cable landing does not command meaningful along-cable chasing in response to noisy target estimates.
- Logs/plots show perpendicular gripper-frame error decreasing monotonically or with bounded transient during the capture phase.

## In Progress

None.

## Complete

None.
