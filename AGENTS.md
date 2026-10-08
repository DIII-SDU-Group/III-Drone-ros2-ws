# AGENTS.md - III-Drone ROS2 Workspace Guide

This file defines how coding agents should work in this repository.

Supporting agent-document ownership is indexed in
[`docs/agents/README.md`](docs/agents/README.md); this file remains the concise
workspace authority and does not duplicate the operating manuals.
[`CLAUDE.md`](CLAUDE.md) is the Claude Code counterpart: it carries the same
workspace policy without Codex-specific orchestration. Keep shared policy in
sync between the two files.
The safe standalone/editable-submodule workflow is
[`docs/agents/editable-repositories.md`](docs/agents/editable-repositories.md).

It is a concise router. The maintained operator and engineering manual begins at
[`docs/README.md`](docs/README.md), bounded-context language is indexed by
[`CONTEXT-MAP.md`](CONTEXT-MAP.md), and executable documentation must follow the
[`automation-ready authoring contract`](docs/automation-ready-authoring-contract.md).
Those sources, not repeated prose here, own operational detail.

## Agent skills

### Issue tracker

Issues and PRDs live in GitHub Issues for `DIII-SDU-Group/III-Drone-ros2-ws`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default triage label vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This repo uses a multi-context domain-doc layout, with `CONTEXT-MAP.md` at the root pointing to relevant per-context `CONTEXT.md` files. See `docs/agents/domain.md`.

## 1) Repository Purpose

`III-Drone-ros2-ws` is the workspace-level integration repository for the III-Drone stack.
It composes multiple sub-repositories (mostly under `src/`) plus `PX4-Autopilot/`, tooling, and environment/bootstrap glue.

Treat this repo as the source of truth for:
- integration workflows
- environment setup
- dependency pinning/governance
- runtime bringup conventions

## 2) Canonical Runtime Model

Bringup flow:
1. Source an environment profile from `setup/` (usually `setup/setup_dev.bash`).
2. Start runtime via III CLI and tmux layout:
   - `iii system boot`
   - `iii system attach`
3. Use supervision/configuration services for lifecycle/state management.

Do not assume direct `ros2 launch ...` alone matches operational behavior.

For agent-operated runtime work, prefer the III-Drone MCP tools over ad hoc
`docker exec` commands. See `tools.md` for the MCP registration expectations,
tool map, and the Docker-exec-to-MCP audit.

## 3) Environment And Build Baseline

Default dev path (inside devcontainer):
- workspace path: `/home/iii/ws`
- ROS distro target: Jazzy in devcontainer config and Dockerfile args

### 3.1 Devcontainer Build/Test Execution

Agents may run build and test commands inside the active devcontainer instead of the host shell.

Do not hardcode a container id. Discover the container from the workspace path/labels first.

Preferred discovery command from the host workspace root:
```bash
docker ps \
  --filter "label=devcontainer.local_folder=$(pwd)" \
  --format '{{.ID}}\t{{.Names}}'
```

The devcontainer config is in `.devcontainer/devcontainer.json` and the in-container workspace root is:
- `/home/iii/ws`

Preferred execution pattern:
```bash
CONTAINER_ID="$(docker ps --filter "label=devcontainer.local_folder=$(pwd)" --format '{{.ID}}' | head -n1)"
docker exec --user iii "$CONTAINER_ID" bash -lc '
  source /opt/ros/jazzy/setup.bash
  cd /home/iii/ws
  colcon build --base-paths src --packages-select <pkg> --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
'
```

Preferred test pattern:
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
- Only run tests for III packages. Do not run test commands for non-III third-party packages.
- Use `--base-paths src` when running `colcon` in the devcontainer to avoid package discovery in unrelated workspace directories.
- Source `/opt/ros/jazzy/setup.bash` before `colcon test`, otherwise Python-based `ament` test helpers may be missing from the environment.

Common build command:
```bash
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

Workspace defaults are in `defaults.yaml` (`src` base path, skip `example_*`).

## 4) Dependency Governance (Strict)

Submodule refs are lock-governed:
- lock file: `deps/submodule-lock.txt`
- verify: `./scripts/git/verify_submodule_lock.sh`
- update lock intentionally: `./scripts/git/update_submodule_lock.sh`

Rules:
- Do not change submodule commits casually.
- If submodule refs are intentionally changed, update and verify lock file in the same change.
- Document why each submodule bump is needed.

## 5) Submodule Edit Policy (Authoritative)

Primary workspace-owned integration areas are still safe default targets:
- `setup/`
- `scripts/`
- top-level docs and workflow files

For submodules, use this strict policy.

### 5.1 Main III codebase (editable when appropriate)

These are core project code and can be edited when the task needs it:
- `src/III-Drone-Core`: main control/perception runtime code.
- `src/III-Drone-Configuration`: configuration server/client and parameter model.
- `src/III-Drone-Contracts`: ROS-free Pydantic API contracts for the operator/runtime boundary.
- `src/III-Drone-Interfaces`: ROS message/service/action contracts.
- `src/III-Drone-Mission`: mission and behavior execution layer.
- `src/III-Drone-Simulation`: simulation integration and assets glue.
- `src/III-Drone-Supervision`: supervision and lifecycle orchestration.
- `src/III-Drone-Runtime`: runtime-host control plane (`iii-runtime-api`, daemon client, host adapters).
- `src/III-Drone-GC`: ground control/operator tooling package.
- `tools/III-Drone-CLI`: main CLI used for canonical bringup.

### 5.2 Forked open-source libraries (ask for verification first)

These are open-source libraries maintained as forks. Editing may be needed, but requires user verification first:
- `src/BehaviorTree.CPP`
- `src/BehaviorTree.ROS2`
- `src/px4-ros2-interface-lib`
- `src/iwr6843aop-ROS2-pkg`

Rule: before changing any of these, pause and ask for explicit verification.

### 5.3 Third-party dependencies (do not edit by default)

Everything else is considered third-party and should not be edited unless there is a strong technical reason, then ask first:
- `PX4-Autopilot`
- `src/Micro-XRCE-DDS-Agent`
- `src/dynamic_message_introspection`
- `src/micro-ROS-Agent`
- `src/micro_ros_msgs`
- `src/px4_msgs`

Recursive/nested third-party submodules (for example under `PX4-Autopilot` and `src/III-Drone-Simulation`) are also no-touch by default.

`build/`, `install/`, and `log/` are generated artifacts: do not hand-edit.

## 6) Configuration And Runtime Assumptions

Runtime expects environment variables and config layout from `setup/paths.bash`, especially:
- `CONFIG_BASE_DIR`
- `NODE_MANAGEMENT_CONFIG_DIR`

Mission specifications and behavior trees are installed, content-addressed
`iii_drone_mission` catalog assets. Runtime APIs use catalog IDs only and must not
fall back to source paths or mission-asset environment variables.

Bringup often depends on installed config content under `.config/iii_drone`.
If config-dependent behavior fails, verify setup/install scripts were run.

HIL always runs without the drone propulsion battery. Do not request physical
verification for HIL startup, restart, or virtual mission runs. Verify in
software that the Pi uses the `hil` runtime profile and that PX4 is the
workstation-owned SITL instance. This HIL rule does not apply to real or
opti-track operations.

## 7) Agent Work Protocol

When implementing changes:
1. Read relevant local docs first:
   - `README.md`
   - `docs/README.md`
   - `docs/runtime-launch-and-node-graph.md`
   - `docs/build-and-environments.md`
   - `docs/dependency-governance.md`
2. Prefer minimal diffs and keep behavior consistent with CLI-first bringup.
3. Validate with the smallest meaningful command set for the touched area.
4. Report any observed inconsistencies instead of silently “fixing” architecture.
5. Run focused tests after each backlog task and the full applicable regression
   once at the end of each phase.

### 7.1 Long-running background work and heartbeat monitors

- Launch one coherent full job for long-running work; do not replace it with a
  chain of short ad-hoc jobs unless a documented safety gate or recovery action
  requires a new attempt.
- When background work is submitted under repository policy, or when a new work
  order monitor is created, attach a heartbeat monitor before returning. Use a
  30-minute heartbeat interval (`FREQ=MINUTELY;INTERVAL=30`). Keep unchanged and
  healthy state quiet; notify on meaningful progress, completion, failure,
  material state change, or required operator action.
- Reuse or update the existing monitor for the same work rather than creating
  duplicate monitors. Preserve the exact job/artifact paths and continue the
  same job after a monitor wake-up.
- For Pi work, never issue an operating-system shutdown. Reboot is permitted;
  runtime stop/restart is permitted when required for a controlled deployment
  or test transition.

### 7.2 Deployment and repository automation

- Deployment is a direct developer workflow: use `iii deploy dev` for ordinary
  SSH/rsync synchronization, a workstation-side ARM64 cross-build (including
  the Pi's Micro XRCE-DDS agent), and runtime restart.  `--build` must never
  invoke colcon or a compiler on the Pi.
- Do not introduce release signing, receiver protocols, trust stores,
  immutable slots, field qualification gates, forced-command SSH, or deployment
  operation nonces.
- A dirty editable workspace is expected during field and HIL development.
- Preserve unrelated dirty files and the physical safety boundary: deployment
  never arms the vehicle or writes PX4 firmware/parameters.
- `iii deploy dev` and `iii host provision` always restart the system daemon and
  the Runtime API and are refused unless the aircraft is provably disarmed and
  landed (`--force` only when that state cannot be read). PX4 parameters change
  only through the explicit `iii px4 param-baseline --profile <profile>`; see
  [`docs/px4-parameter-baselines.md`](docs/px4-parameter-baselines.md).

## 8) Validation Checklist

Use as applicable:
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

## 9) Known Project Risks To Keep In Mind

Active risks documented in the workspace:
- launch-path inconsistencies between some launch files and supervision-managed flows
- tight cross-package coupling via shared interfaces/config
- fragility when env/config setup is incomplete

Agents should preserve stability and avoid broad refactors unless explicitly requested.


## 10) Delegated workflow

By default, the parent owns the task, decisions, integration, and final signoff. Handle a
short dependent change, routine document edit, deterministic inventory, or focused check
directly when one agent can complete it without a handoff. Delegate a coherent, independent
packet only when its expected time or context saving or independent judgment exceeds its
briefing, waiting, and reintegration cost. Broad searches, bulky logs, long test batches,
and separable implementation slices are suitable examples. Judge the workflow by elapsed
time and total model work per accepted task, not token throughput. There is no mandatory
agent chain or routine review agent for ordinary tasks; backlog execution has a
required final instruction review.

For a request to **execute a substantial batch of instructions** with multiple coherent
outcomes or consequential dependencies (such as a multi-part work order),
automatically use [backlog-planning](.agents/skills/backlog-planning/SKILL.md)
and then [backlog-execution](.agents/skills/backlog-execution/SKILL.md) in the
same chat. Planning happens in a Terra backlog writer; document-grounded plans
receive one independent Terra backlog-verifier pass before user questions. A
plan-only request stops after planning; a request to execute an existing ready
backlog starts with execution. A long prompt or several checklist items do not
by themselves require a backlog. Backlog task IDs do not each require a fresh agent; the parent may complete
small, coupled steps directly.

Keep [subagent-orchestration](.agents/skills/subagent-orchestration/SKILL.md)
opt-in: use it only when the user explicitly invokes `$subagent-orchestration`
for an open-ended objective such as desired behavior changes. Without that
invocation, use the ordinary light-delegation policy for such requests.
Subagents follow their bounded role and packet without loading parent-only skills.
Nested delegation is forbidden: only the parent may dispatch agents, coordinate
their work, verify the integrated execution result, and sign off. The parent may
request an occasional independent, read-only `terra_verifier` review for a
specifically difficult material boundary; backlog execution also requires one
final Terra review against the original instructions after all tasks are executed.
The hook in `.codex/hooks/enforce_subagent_policy.py` enforces the no-nesting rule. When
opening a new implementation agent, start with `luna_worker` and escalate only
from documented insufficiency. The opt-in orchestration skill may start a genuinely
difficult slice with Terra when specific prior evidence shows Luna is predictably
unsuitable. Do not silently change the parent model or reload roles by
restarting a daemon.

Reuse an existing subagent by default whenever the next packet has any useful
overlap with its context, even if the relation is slight or crosses task IDs.
Do not open a new agent merely because a packet is new or an existing agent is a
higher tier than the packet would otherwise need. Open a new agent when the work
is completely unrelated to available contexts, the existing agent's context is
full or nearly full, an independent reviewer is required, or a hard role
capability such as read-only access prevents the assignment. The parent may
also replace an agent to free capacity. Give each reused agent a revised packet;
give a replacement a concise evidence handoff.

Subagent selection must always use an explicit repository-defined `agent_type`.
Never use generic/default/built-in agents or model inheritance as a fallback.
`task_name` does not select an agent profile. If explicit custom-agent routing
is unavailable, do not spawn a generic replacement.


# Additional workspace instruction:
- Only run tests for III packages. Do not run test commands for non-III third-party packages.
