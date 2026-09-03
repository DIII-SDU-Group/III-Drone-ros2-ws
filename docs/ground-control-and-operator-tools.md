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

The Mission page is the routine inspection workspace: it exposes manual mapper
and current-position overview capture, readiness, the fixed inspection mission,
recharge intents, recording, and recovery state. The Runtime page owns III
lifecycle and service controls. Configuration edits are server-authorized;
constant changes are persisted for a cold restart. Simulation battery reset is
available only under the sim profile and is never exposed as a real-aircraft
operation.

GC proxy responsibilities:

- mDNS discovery of `_iii-runtime-api._tcp.local` runtime APIs.
- manual endpoint fallback when multicast is unavailable.
- `/identity` validation before target selection.
- selected-target HTTP/WebSocket forwarding only; it is not an open proxy.

The production host boundary is converged by `iii gc provision`, not by running
Compose manually. On supported graphical Ubuntu 22.04/24.04 computers it retains
an exact source/policy/cache plan, converges host-native user services and pinned
ROS-free application/controller environments, then refuses success unless a
second Ansible check predicts zero managed drift. See
[`gc-host-provisioning.md`](gc-host-provisioning.md) for online, prepared-offline,
replacement-host, login/logout, and persistent-state procedures.

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
`III_RUNTIME_API_URL` and `III_RUNTIME_API_CLI_TOKEN`; they never forward shell
commands over SSH. Deployment SSH is limited to key-only `iii-deploy@iii.local`, the
fixed receiver gateway, and resumable SFTP uploads. Legacy install, workspace
synchronization, arbitrary SSH, SCP, rsync, and source pull commands are absent.
The separately keyed `iii@iii.local` shell is the explicit attended
development/field-maintenance exception. It provides full sudo, is limited to the
operator network with forwarding disabled, and is never invoked by the CLI,
`iii-deploy` receiver, or Ansible inventory.

Use the process-local field defaults and explicit target/profile overrides:

```bash
source setup/setup_field.bash
iii field prepare v1.2.3 --dry-run
iii field verify v1.2.3 --offline --json
iii deploy plan --target real --json
iii deploy field --bundle-set /operator/cache/<release>.iii-release-v1 \
  --configuration-checkpoint-id <sha256> --target real --dry-run
iii system clock sync --target real --profile real --dry-run
iii field check --target real --json
iii field check --target real --signing-key /secure/field.pem \
  --trusted-signers /secure/trusted-signers.json --json
```

Every mutation first retains an exact operation plan. Applying it requires the
same operation ID and explicit confirmation. Compact plans, grouped mission/tree/
parameter impact, and actual phase results live under `.iii/operations/`; large
build and bundle payloads live in a separate cache. `iii field check` is read-only
against GC/aircraft state and its sealed result is evidence, never authorization.
Unsigned results are explicitly diagnostic. A release/commissioning evidence
record requires a private key whose active `workstation-field` identity is in the
selected trusted-signer store. `iii field acknowledge` applies the same trust
check, signs rationale for present warnings only, and cannot acknowledge a
failure or change severity. `iii deploy operations prune --dry-run` includes the
exact content identities of candidates and protected records; apply refuses a
changed plan and never touches the artifact cache.

The login-scoped GC clock companion invokes the same receiver-owned clock-sync
operation once for each newly present `iii.local` real-profile runtime. Simulation
and manually entered endpoints never trigger it. While the receiver advertises
`DEGRADED_CLOCK` or `CLOCK_FAULT_ACTIVE`, new runtime mutations fail closed while
status, diagnostics, deployment, recovery, and authenticated clock sync remain
available.

`iii field prepare` is the only cache-populating step and may refresh trusted
online release status. `iii field verify --offline` subsequently opens only the
authenticated local cache and proves representative GC-only, drone-only, and
paired component packaging. It retains a content-identified report whose every
scenario states `network_access: false` and `target_mutation: false`; absence of
either signed component or omission of explicit `--offline` is rejected.

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
