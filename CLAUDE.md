# CLAUDE.md - III-Drone ROS2 Workspace Guide

This file is a concise router for working in this repository. It mirrors the
workspace policy in `AGENTS.md` (the Codex counterpart) without Codex-specific
orchestration; when shared policy changes, update both files. Operational detail
lives in the maintained docs, not here:

- Operator and engineering manual: [`docs/README.md`](docs/README.md)
- Bounded-context language: [`CONTEXT-MAP.md`](CONTEXT-MAP.md), which points to
  per-context `CONTEXT.md` files (see [`docs/agents/domain.md`](docs/agents/domain.md))
- Executable documentation must follow the
  [automation-ready authoring contract](docs/automation-ready-authoring-contract.md)
- Safe edit/test/branch/PR workflow for editable repositories:
  [`docs/agents/editable-repositories.md`](docs/agents/editable-repositories.md)
- Runtime tooling (III-Drone MCP server): [`tools.md`](tools.md)

## Issues and triage

- Issues and PRDs live in GitHub Issues for `DIII-SDU-Group/III-Drone-ros2-ws`.
  See [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md).
- Triage labels: `needs-triage`, `needs-info`, `ready-for-agent`,
  `ready-for-human`, `wontfix`. See [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md).

## 1) Repository purpose

`III-Drone-ros2-ws` is the workspace-level integration repository for the
III-Drone stack. It composes multiple sub-repositories (mostly under `src/`)
plus `PX4-Autopilot/`, tooling, and environment/bootstrap glue.

Treat this repo as the source of truth for:
- integration workflows
- environment setup
- dependency pinning/governance
- runtime bringup conventions

## 2) Canonical runtime model

Bringup flow:
1. Source an environment profile from `setup/` (usually `setup/setup_dev.bash`).
2. Start runtime via the III CLI and tmux layout:
   - `iii system boot`
   - `iii system attach`
3. Use supervision/configuration services for lifecycle/state management.

Do not assume a direct `ros2 launch ...` alone matches operational behavior.

For runtime work (simulation, system lifecycle, PX4 commands, mission
workflows, topics, rosbags, logs, Gazebo observation, configuration), prefer the
III-Drone MCP tools (`tools/III-Drone-MCP`) over ad hoc `docker exec` commands
when they are available. The project `.mcp.json` registers them as `iii_drone`
(tools appear as `mcp__iii_drone__<tool>`) via
`scripts/workspace/iii_drone_mcp_bridge.sh`, which needs a running devcontainer.
`tools.md` has the tool map and the list of shell
patterns that remain acceptable (builds, tests, code inspection, one-off
diagnosis where no MCP tool exists).

## 3) Environment and build baseline

Default dev path (inside the devcontainer):
- workspace path: `/home/iii/ws`
- ROS distro: Jazzy (devcontainer config and Dockerfile args)

### 3.1 Devcontainer build/test execution

Run build and test commands inside the active devcontainer rather than on the
host. Do not hardcode a container id; discover it from the workspace path label:

```bash
docker ps \
  --filter "label=devcontainer.local_folder=$(pwd)" \
  --format '{{.ID}}\t{{.Names}}'
```

Config: `.devcontainer/devcontainer.json`. In-container workspace root: `/home/iii/ws`.

Build:
```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec --user iii "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon build --base-paths src --packages-select <pkg> --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
'
```

Test:
```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec --user iii "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon test --base-paths src --packages-select <pkg> --ctest-args --output-on-failure
  colcon test-result --verbose
'
```

Notes:
- **Only run tests for III packages.** Never run test commands for non-III
  third-party packages.
- Always pass `--base-paths src` to `colcon` in the devcontainer to avoid
  package discovery in unrelated workspace directories.
- Source `/opt/ros/jazzy/setup.bash` before `colcon test`, otherwise
  Python-based `ament` test helpers may be missing.

Full build:
```bash
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

Workspace defaults are in `defaults.yaml` (`src` base path, skip `example_*`).

## 4) Dependency governance (strict)

Submodule refs are lock-governed:
- lock file: `deps/submodule-lock.txt`
- verify: `./scripts/git/verify_submodule_lock.sh`
- update lock intentionally: `./scripts/git/update_submodule_lock.sh`

Rules:
- Do not change submodule commits casually.
- If submodule refs are intentionally changed, update and verify the lock file
  in the same change.
- Document why each submodule bump is needed.

See [`docs/dependency-governance.md`](docs/dependency-governance.md).

## 5) Submodule edit policy

Workspace-owned integration areas are safe default targets:
- `setup/`
- `scripts/`
- top-level docs and workflow files

### 5.1 Main III codebase (editable when the task needs it)

- `src/III-Drone-Core`: main control/perception runtime code
- `src/III-Drone-Configuration`: configuration server/client and parameter model
- `src/III-Drone-Contracts`: ROS-free Pydantic API contracts for the operator/runtime boundary
- `src/III-Drone-Interfaces`: ROS message/service/action contracts
- `src/III-Drone-Mission`: mission and behavior execution layer
- `src/III-Drone-Simulation`: simulation integration and asset glue
- `src/III-Drone-Supervision`: supervision and lifecycle orchestration
- `src/III-Drone-Runtime`: runtime-host control plane (`iii-runtime-api`, daemon client, host adapters)
- `src/III-Drone-GC`: ground control/operator tooling
- `tools/III-Drone-CLI`: main CLI used for canonical bringup

### 5.2 Forked open-source libraries (ask before editing)

Editing may be needed, but **pause and ask for explicit verification first**:
- `src/BehaviorTree.CPP`
- `src/BehaviorTree.ROS2`
- `src/px4-ros2-interface-lib`
- `src/iwr6843aop-ROS2-pkg`

### 5.3 Third-party dependencies (do not edit by default)

Do not edit unless there is a strong technical reason, and then ask first:
- `PX4-Autopilot`
- `src/Micro-XRCE-DDS-Agent`
- `src/dynamic_message_introspection`
- `src/micro-ROS-Agent`
- `src/micro_ros_msgs`
- `src/px4_msgs`

Recursive/nested third-party submodules (e.g. under `PX4-Autopilot` and
`src/III-Drone-Simulation`) are also no-touch by default.

`build/`, `install/`, and `log/` are generated artifacts: never hand-edit.

## 6) Configuration and runtime assumptions

Runtime expects environment variables and config layout from `setup/paths.bash`,
especially `CONFIG_BASE_DIR` and `NODE_MANAGEMENT_CONFIG_DIR`.

Mission specifications and behavior trees are installed, content-addressed
`iii_drone_mission` catalog assets. Runtime APIs use catalog IDs only and must
not fall back to source paths or mission-asset environment variables.

Bringup often depends on installed config content under `.config/iii_drone`. If
config-dependent behavior fails, verify setup/install scripts were run.

**HIL** always runs without the drone propulsion battery. Do not request physical
verification for HIL startup, restart, or virtual mission runs. Verify in
software that the Pi uses the `hil` runtime profile and that PX4 is the
workstation-owned SITL instance. This rule does not apply to real or OptiTrack
operations.

## 7) Work protocol

When implementing changes:
1. Read relevant local docs first:
   - `README.md`
   - `docs/README.md`
   - `docs/runtime-launch-and-node-graph.md`
   - `docs/build-and-environments.md`
   - `docs/dependency-governance.md`
2. Prefer minimal diffs and keep behavior consistent with CLI-first bringup.
3. Validate with the smallest meaningful command set for the touched area.
4. Report observed inconsistencies instead of silently "fixing" architecture.
5. Run focused tests after each task, and the full applicable regression once at
   the end of a larger phase of work.

### 7.1 Long-running work

- Launch one coherent full job for long-running work; do not replace it with a
  chain of short ad-hoc jobs unless a documented safety gate or recovery action
  requires a new attempt.
- For Pi work, **never issue an operating-system shutdown**. Reboot is
  permitted; runtime stop/restart is permitted when required for a controlled
  deployment or test transition.

### 7.2 Deployment

- Deployment is a direct developer workflow: use `iii deploy dev` for SSH/rsync
  synchronization, a workstation-side ARM64 cross-build (including the Pi's
  Micro XRCE-DDS agent), and runtime restart. `--build` must never invoke colcon
  or a compiler on the Pi.
- Do not introduce release signing, receiver protocols, trust stores, immutable
  slots, field qualification gates, forced-command SSH, or deployment operation
  nonces.
- A dirty editable workspace is expected during field and HIL development.
  Preserve unrelated dirty files.
- Preserve the physical safety boundary: deployment never arms the vehicle or
  writes PX4 firmware/parameters.
- `iii deploy dev` and `iii host provision` always restart the system daemon and
  the Runtime API and are refused unless the aircraft is provably disarmed and
  landed (`--force` only when that state cannot be read). PX4 parameters change
  only through the explicit `iii px4 param-baseline --profile <profile>`; see
  [`docs/px4-parameter-baselines.md`](docs/px4-parameter-baselines.md).

## 8) Validation checklist

```bash
# Dependency integrity
./scripts/git/verify_submodule_lock.sh

# Build (full)
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

# Build (targeted)
colcon build --packages-select <pkg_name> --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug
```

If runtime-related:
```bash
source setup/setup_dev.bash
iii system boot
```

## 9) Known project risks

- launch-path inconsistencies between some launch files and supervision-managed flows
- tight cross-package coupling via shared interfaces/config
- fragility when env/config setup is incomplete

Preserve stability and avoid broad refactors unless explicitly requested.
