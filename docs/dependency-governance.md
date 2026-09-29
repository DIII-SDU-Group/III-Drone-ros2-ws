# Dependency Notes

The workspace retains `deps/submodule-lock.txt` as a useful reproducibility
reference.  It is not a deployment authorization gate: an attending developer
may deploy the editable workspace, including local changes, directly to the Pi.

Use ordinary Git review and tests when sharing changes.  Field deployment does
not require protected branches, release tags, signatures, or qualification
evidence.

## Submodule branches

A pinned submodule commit must stay reachable after temporary branches are
deleted. The pull-request check
`scripts/ci/verify_iii_submodule_branch_policy_ci.sh` enforces this for each
changed pin:

- **III repositories** (`src/III-*`, `tools/III-*`): the pinned commit must be
  on a branch in the workspace branch stack between the pull request's base
  and feature branches. Use the same branch name as the workspace branch.
- **Governed forks** (`PX4-Autopilot`, `src/px4-ros2-interface-lib`,
  `src/BehaviorTree.CPP`, `src/BehaviorTree.ROS2`,
  `src/iwr6843aop-ROS2-pkg`): the same rule applies. A fork may also pin an
  unmodified upstream commit that is on the fork's default branch.
- **Nested forks** such as `src/III-Drone-Simulation/Gazebo-simulation-assets`
  follow the same naming, although the check covers only top-level pins.

When a fork carries III changes, push them to a fork branch named after the
workspace branch, for example `deployment-infrastructure-redesign`. Editing
fork content still requires explicit approval, as `CLAUDE.md` and `AGENTS.md`
describe.
