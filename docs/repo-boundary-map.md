# Repository Boundary Map

This document records the current repository boundaries for the III-Drone stack.
Branch, gitlink, and release policy is owned by
[dependency governance](dependency-governance.md).

## 1. Guiding Principle

Use a hybrid model:
- Keep reusable, independently testable software in dedicated repos.
- Keep system-integration glue in the top-level workspace repo.

Decision rule:
- If a component has standalone lifecycle/value outside this exact workspace, keep it separate.
- If a component only exists to compose this stack for your robots/team workflows, keep it in workspace.

## 2. Recommended Target Structure

### 2.1 Separate Repositories

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

### 2.2 Workspace-Owned Integration

1. `setup/*` and top-level `scripts/*`
- Environment/bootstrap/deployment glue for this specific integrated system.

2. Runtime operator-view and deployment glue
- Files that encode tmux session layout, environment assumptions, and deployment workflow belong with the workspace-level integration layer.

3. Simulation install glue and local asset plumbing
- Keep `III-Drone-Simulation` as a repo if desired, but workspace should own the authoritative integration scripts/profiles that tie it to PX4 and your current workflow.

4. Deployment profile definitions
- Any files that encode local robot/developer workflow assumptions should be workspace-owned.

### 2.3 Ground Control And CLI

`III-Drone-GC` and `III-Drone-CLI` are editable III submodules. Their code,
package tests, `develop`, and `main` branches stay in their owning repositories;
the workspace owns integration, release composition, and exact gitlink/lock pins.

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

The governed branch flow is explicit:
- Editable III feature branches merge into their repository `develop` branches.
- `promote/develop-to-main/*` branches carry verified develop-derived changes
  into `main`; their workspace-only delta from `develop` is limited to III
  gitlinks and `deps/submodule-lock.txt`.
- Only the workspace has `release`, and only protected workspace `main` may
  promote into it. Editable submodules remain on `main`.
- Qualified robot deployments use immutable workspace `vX.Y.Z` tags reachable
  from `release`, freezing every dependency ref.

## 5. Current Governance

1. The workspace is the canonical integration and qualified-release repository.
2. Every editable III repository owns its code and package checks through `main`.
3. Gitlinks and `deps/submodule-lock.txt` freeze exact integration commits.
4. Forks and third-party repositories remain outside editable-III automation.
5. Structural repository moves require a separate architecture decision.

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
