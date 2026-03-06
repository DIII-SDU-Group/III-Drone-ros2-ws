# Repository Boundary Map

This document proposes how to structure repository boundaries for the III-Drone stack to reduce submodule complexity while preserving modularity and research velocity.

## 1. Guiding Principle

Use a hybrid model:
- Keep reusable, independently testable software in dedicated repos.
- Keep system-integration glue in the top-level workspace repo.

Decision rule:
- If a component has standalone lifecycle/value outside this exact workspace, keep it separate.
- If a component only exists to compose this stack for your robots/team workflows, keep it in workspace.

## 2. Recommended Target Structure

### 2.1 Keep As Separate Repos (Submodules or Manifest-managed externals)

1. `src/III-Drone-Interfaces`
- Contract/API package; should remain independent and versioned deliberately.

2. `src/III-Drone-Core`
- Large C++ subsystem with clear internal testability and potential reuse.

3. `src/III-Drone-Configuration`
- Can remain separate because it is shared by many runtime components.

4. `src/III-Drone-Mission`
- Distinct autonomy layer with behavior-tree and PX4 mode logic.

5. `src/III-Drone-Supervision`
- Distinct orchestration/lifecycle subsystem and strong standalone identity.

6. External/vendor deps (keep separate, upstream-driven)
- `BehaviorTree.CPP`, `BehaviorTree.ROS2`, `dynamic_message_introspection`, `px4_msgs`, `px4-ros2-interface-lib`, `micro-ROS-Agent`, `micro_ros_msgs`, `Micro-XRCE-DDS-Agent`.

7. `PX4-Autopilot`
- Keep separate repository.

### 2.2 Move Into Workspace Repo (or treat as workspace-owned, not independent products)

1. `tools/tmuxinator/*`
- Pure integration/ops glue for your team workflows.

2. `setup/*` and top-level `scripts/*`
- Environment/bootstrap/deployment glue for this specific integrated system.

3. Simulation install glue and local asset plumbing
- Keep `III-Drone-Simulation` as a repo if desired, but workspace should own the authoritative integration scripts/profiles that tie it to PX4 and your current workflow.

4. Deployment profile definitions
- Any files that encode local robot/developer workflow assumptions should be workspace-owned.

### 2.3 Optional Decision (Choose One and Stay Consistent)

`III-Drone-GC` and `III-Drone-CLI` can go either way:

Option A: Keep separate repos
- Good if they may be reused across projects.
- Better ownership/versioning boundaries.

Option B: Move into workspace
- Good if they are tightly coupled to this exact stack and unlikely to be reused.
- Simplifies day-to-day branch/PR flow.

Recommendation for now:
- Keep both separate until coupling becomes clearly one-project-only.

## 3. Submodule Complexity Reduction Plan

You can reduce pain without collapsing repos:

1. Pin stable refs by release train
- For each workspace release tag, pin each dependency to tested commit/tag.

2. Add ref-lock file and verifier script
- Example: `workspace-deps.lock` listing expected commit for each external.
- CI check fails if actual refs drift unexpectedly.

3. Standardize update workflow
- `update deps` script + PR template section for dependency bump rationale.

4. Consider replacing git submodules with ROS-friendly manifest flow later
- `vcstool` (`.repos` file) often gives better ergonomics for ROS teams.
- Keep this as phase 2/3, not immediate migration.

## 4. Branching/Release Coupling Across Repos

Given your new `staging` branch in workspace, mirror this pattern conceptually:
- Workspace `staging` references dependency commits that are also from tested non-main branches/tags.
- Workspace `main` references only stabilized dependency commits.
- Robot deployments use workspace tags that freeze all dependency refs.

## 5. Suggested Immediate Actions (Low Disruption)

1. Keep current repo split for core/mission/config/interfaces/supervision.
2. Treat workspace as the canonical integration repo.
3. Add dependency lock + CI validation in workspace.
4. Define policy for GC/CLI after 1-2 months of observed coupling.
5. Migrate only obviously workspace-local glue if needed; avoid major structural churn during active research cycles.

## 6. Anti-Patterns To Avoid

1. Moving everything into one repo while architecture is still evolving rapidly.
2. Keeping many small repos with no clear ownership or release policy.
3. Deploying from floating branches instead of immutable tags.
4. Allowing workspace submodule refs to drift without explicit change tracking.

## 7. Final Recommendation

For your context (research-heavy, robot testing cadence, single original author transitioning to team use):
- Keep core separation.
- Strengthen integration governance in this workspace.
- Reduce friction with process/tooling first, structural consolidation second.
