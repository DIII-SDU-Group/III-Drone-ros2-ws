# Testing

Use the devcontainer for ROS package tests. All commands below are limited to
III-owned packages and tooling; do not run tests for third-party submodules such
as PX4, BehaviorTree, Micro XRCE-DDS, or generated dependency workspaces.

## Full III Test Suite

From the workspace root inside the devcontainer:

```bash
scripts/workspace/run_iii_test_suite.sh
```

This script runs:

- `colcon build --base-paths src --packages-up-to ...` for III packages only.
- `colcon test --base-paths src --packages-select ...` for III packages only.
- `colcon test-result --verbose`.
- TypeScript contract freshness check:
  `python3 src/III-Drone-Contracts/scripts/generate_typescript.py --output src/III-Drone-GC/frontend/src/generated/contracts.ts --check`.
- GUI v2 frontend `npm ci`, `contracts:check`, `lint`, `typecheck`, `test`,
  and `build`.
- Top-level integration tests under `tests/`.
- CLI tests under `tools/III-Drone-CLI/test`.

If `npm` is unavailable, the script uses Docker's `node:22-alpine` image when
Docker is available. If neither is available, it downloads a pinned Node 22
toolchain into `.cache/` and runs the same frontend commands from there. Set
`III_NODE_VERSION` to override the fallback Node version.

## Targeted ROS Package Tests

Use targeted package selection while developing:

```bash
source /opt/ros/jazzy/setup.bash
colcon test --base-paths src --packages-select iii_drone_runtime --ctest-args --output-on-failure
colcon test-result --verbose
```

Common GUI v2/runtime packages:

- `iii_drone_contracts`
- `iii_drone_runtime`
- `iii_drone_gc`
- `iii_drone_supervision`
- `iii_drone_configuration`

## Frontend Tests

From the workspace root:

```bash
npm --prefix src/III-Drone-GC/frontend ci
npm --prefix src/III-Drone-GC/frontend run contracts:check
npm --prefix src/III-Drone-GC/frontend run lint
npm --prefix src/III-Drone-GC/frontend run typecheck
npm --prefix src/III-Drone-GC/frontend test
npm --prefix src/III-Drone-GC/frontend run build
```

The generated TypeScript contracts must stay in sync with
`III-Drone-Contracts`. Use `contracts:check` in CI-like runs and regenerate only
when contract models intentionally change.

## Compose Smoke

Validate GC compose files without starting containers:

```bash
docker compose -f src/III-Drone-GC/docker-compose.dev.yml config
docker compose -f src/III-Drone-GC/docker-compose.prod.yml config
```

For a local production smoke while a sim `iii-runtime-api` is running:

```bash
III_GC_FRONTEND_PORT=5174 docker compose -p iii-gc-smoke -f src/III-Drone-GC/docker-compose.prod.yml up -d --build
curl -fsS http://127.0.0.1:5174/
curl -fsS http://127.0.0.1:8780/identity
curl -fsS 'http://127.0.0.1:8780/runtime/discovery?timeout_s=2'
III_GC_FRONTEND_PORT=5174 docker compose -p iii-gc-smoke -f src/III-Drone-GC/docker-compose.prod.yml down --remove-orphans
```

## GUI v2 Sim E2E Smoke

With a sim `iii-runtime-api` reachable at `http://127.0.0.1:8765`, run the
read-only end-to-end smoke from the workspace root on the host:

```bash
III_GC_FRONTEND_PORT=5174 scripts/workspace/gui_v2_sim_e2e_smoke.py --start-compose
```

The script verifies the frontend/proxy/runtime path, selects the local sim
runtime, authenticates, reads every operator state domain, and writes artifacts
under `log/gui-v2-sim-e2e-smoke/`. Mutating sim-only workflow and flight command
extensions are documented in
`src/III-Drone-GC/docs/gui-v2-sim-e2e-smoke.md`.
