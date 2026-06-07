# Ground Control And Operator Tools

## 1. GUI v2 Operator Stack

GUI v2 is the primary operator GUI for the new architecture. It is a web stack
with a ROS-free ground-control computer and a runtime-host API:

```text
Browser frontend
  -> GC proxy on the ground-control computer
  -> iii-runtime-api on the runtime host
  -> III daemon, ROS graph, MAVLink/MAVSDK, logs, config, rosbag
```

The frontend and GC proxy live in `src/III-Drone-GC`:

- `frontend/`: React/TypeScript operator interface using generated contract
  types.
- `iii_drone_gc/v2_proxy`: thin FastAPI proxy that discovers, validates,
  selects, and forwards to one runtime API target.
- `docker-compose.dev.yml`: Vite frontend plus proxy for active development.
- `docker-compose.prod.yml`: static frontend plus proxy for production-style
  serving from the ground-control computer.

The runtime API lives in `src/III-Drone-Runtime` and is the only network-facing
operator/runtime API for GUI v2 and remote runtime-control CLI commands.

## 2. Legacy Tk Reference

The Tk GUI remains in `src/III-Drone-GC` as a legacy parity/reference
implementation:

- `gc_node.py`: ROS communication backbone for the Tk GUI.
- `gui.py`: current Tk GUI implementation.
- `gui_original.py`: earlier Tk implementation retained for reference.

Do not build new GUI v2 behavior on `IIIGCNode`. Current and legacy Tk
diagnostics/commands are mapped in
`src/III-Drone-GC/docs/gui-v2-parity.md`.

## 3. Runtime API And Proxy Workflows

Runtime API responsibilities:

- browser session authentication and single active GUI session.
- remote CLI token authentication.
- typed operator state domains and WebSocket state streaming.
- daemon-backed runtime control for boot/start/stop/restart/status.
- typed command dispatch for PX4, custom operation, payload, perception,
  configuration, rosbag, logs, map, and simulation surfaces.

GC proxy responsibilities:

- mDNS discovery of `_iii-runtime-api._tcp.local` runtime APIs.
- manual endpoint fallback when multicast is unavailable.
- `/identity` validation before target selection.
- selected-target HTTP/WebSocket forwarding only; it is not an open proxy.

Key docs:

- `src/III-Drone-GC/docs/gui-v2-deployment.md`
- `src/III-Drone-GC/docs/gui-v2-sim-e2e-smoke.md`
- `src/III-Drone-GC/docs/gui-v2-real-profile-acceptance.md`
- `src/III-Drone-GC/docs/gui-v2-security-checklist.md`
- `src/III-Drone-Runtime/docs/runtime-api-configuration.md`

## 4. CLI And Supporting Tools

`tools/III-Drone-CLI` provides the `iii` command for build, deploy,
configuration, system runtime control, and admin workflows.

Canonical local/runtime-host bringup remains:

```bash
source setup/setup_dev.bash
iii system boot
iii system attach
iii system start
```

Remote runtime-control commands use `iii-runtime-api` with
`III_RUNTIME_API_URL` and `III_RUNTIME_API_CLI_TOKEN`; they no longer forward
runtime-control shell commands over SSH. SSH remains for deployment, sync,
install, and explicit admin sessions.

Service-scoped CLI commands control daemon-managed services:

```bash
iii system service list
iii system service restart micro_ros_agent
```

QGroundControl is outside the III supervision scope. It connects to PX4/operator
telemetry and does not gate `iii system start`.

## 5. Deployment And Smoke Commands

Validate and run the GC stack:

```bash
docker compose -f src/III-Drone-GC/docker-compose.dev.yml config
docker compose -f src/III-Drone-GC/docker-compose.prod.yml config

III_GC_FRONTEND_PORT=5174 \
docker compose -p iii-gc-smoke -f src/III-Drone-GC/docker-compose.prod.yml up -d --build
```

Run the sim E2E smoke while a sim runtime API is available:

```bash
III_GC_FRONTEND_PORT=5174 \
scripts/workspace/gui_v2_sim_e2e_smoke.py --start-compose
```

Stop the production-style smoke stack:

```bash
docker compose -p iii-gc-smoke -f src/III-Drone-GC/docker-compose.prod.yml down --remove-orphans
```

## 6. Operational Importance

Ground control is not only visualization; it is an active control and
configuration interface. GUI v2 therefore treats authentication, command
gating, selected-runtime validation, and stale/disconnected state as operational
safety surfaces.
