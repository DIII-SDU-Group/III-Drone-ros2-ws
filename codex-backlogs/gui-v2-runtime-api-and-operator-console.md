# GUI v2 Runtime API And Operator Console Backlog

## Context

This backlog implements the complete GUI v2 architecture specified in
`src/III-Drone-GC/docs/gui-v2-spec.md`.

Goal:
- Replace the hacked-together Tkinter GUI with a web-based, ROS-agnostic ground
  control frontend.
- Move runtime/network control into a runtime-host `iii-runtime-api` so the
  ground-control computer does not need ROS 2, DDS discovery, MAVSDK, or III
  runtime packages.
- Make `iii-runtime-api` the single network-facing operator/runtime API for GUI
  and remote CLI runtime-control workflows.
- Preserve current GUI functionality while adding runtime control, PX4 command
  control, workflow pages, rosbag/log/config/map functionality, typed health
  topics, and extensive tests.

Agreed architecture:
- `src/III-Drone-GC`
  - Owns React + TypeScript + Vite frontend.
  - Owns thin Python/FastAPI ground-control backend/proxy.
  - Owns GC Docker Compose stack.
  - Depends only on `III-Drone-Contracts`.
  - Must not depend on `III-Drone-Runtime`, `III-Drone-Supervision`,
    `III-Drone-Interfaces`, MCP, ROS, DDS, MAVSDK, or runtime-heavy packages.
- `src/III-Drone-Runtime`
  - New submodule/package.
  - Owns III daemon transport/control plane and `iii-runtime-api`.
  - Runs on the runtime host: devcontainer/runtime environment in sim, onboard
    host in real profile.
  - Talks locally to systemd, the III daemon Unix socket, ROS/DDS, MAVSDK, and
    owned III runtime packages.
  - Owns browser authentication, single active GUI session, heartbeat lease,
    command gating, runtime API WebSocket/REST, and remote CLI token auth.
  - Depends on `III-Drone-Supervision`, `III-Drone-Contracts`,
    `III-Drone-Interfaces`, and selected runtime/operator-control modules.
- `src/III-Drone-Contracts`
  - New ROS-free submodule/package.
  - Owns Pydantic API contracts, enums, command identifiers, state/event models,
    error/rejection models, parameter manifest models, and API version metadata.
  - Source of truth for generated TypeScript types consumed by `III-Drone-GC`.
- `tools/III-Drone-CLI`
  - Remote runtime-control commands move to `iii-runtime-api`.
  - SSH command forwarding is removed for remote runtime-control commands.
  - SSH remains only for workflows that inherently require it, such as file
    transfer/sync and explicit shell access.
  - Local CLI may initially continue using the local daemon Unix socket.

Current code findings:
- Current Tk GUI and ROS node are in:
  - `src/III-Drone-GC/iii_drone_gc/gui.py`
  - `src/III-Drone-GC/iii_drone_gc/gc_node.py`
  - `src/III-Drone-GC/test/test_gc_node_logic.py`
- Current daemon/supervision control path is in:
  - `src/III-Drone-Supervision/iii_drone_supervision/system_daemon.py`
  - `src/III-Drone-Supervision/iii_drone_supervision/system_manager.py`
  - `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py`
  - `src/III-Drone-Supervision/iii_drone_supervision/service_manager.py`
  - `src/III-Drone-Supervision/iii_drone_supervision/tmux_spec.py`
- Current CLI daemon client and remote forwarding are in:
  - `tools/III-Drone-CLI/iii/system_client.py`
  - `tools/III-Drone-CLI/iii/system.py`
  - `tools/III-Drone-CLI/iii/container_manager.py`
  - `tools/III-Drone-CLI/iii/ssh_manager.py`
- Current custom-operation client is in:
  - `src/III-Drone-Mission/iii_drone_mission/operations_client.py`
- MCP contains reference implementations only, not dependencies:
  - `tools/III-Drone-MCP/iii_drone_mcp/agent_tools.py`
  - `tools/III-Drone-MCP/iii_drone_mcp/px4_command_client.py`
  - `tools/III-Drone-MCP/iii_drone_mcp/mission_deploy_workflow.py`
  - `tools/III-Drone-MCP/iii_drone_mcp/simulation_observation.py`
- Current package boundaries and submodule refs are lock-governed by
  `deps/submodule-lock.txt`; intentional submodule additions/moves must update
  and verify the lock.

Spec coverage audit, updated 2026-05-27:
- Spec 1, Purpose: covered by this backlog context, P0-P12, and P11 parity.
- Spec 2, Fixed Decisions / Resolved Decision Summary: covered by P0 package
  boundaries, P1 contracts, P2 runtime API/auth, P3 runtime/CLI/logs, P5 PX4,
  P8 GC proxy, P9 frontend, and P10 deployment.
- Spec 3, Non-Goals: recorded in this context and reinforced by P5.T0,
  P8.T0, P9.T1, P9.T15, P10.T3, and P11.T4.
- Spec 4, Runtime Architecture: covered by P1 contracts, P2 runtime API,
  session, WebSocket, event, ROS executor, and dispatch work, P3 runtime/logs/
  CLI, P4 health, P5 PX4, P6 operator handlers, and P8 proxy.
- Spec 5, Ownership And Shared Code: covered by P0 submodule/dependency tasks,
  P0.T5 shared utility isolation, P5 PX4 adapter, P6 operator-control facade,
  and P11 documentation/parity.
- Spec 6, Functional Scope To Preserve: covered by P6 handlers, P7 map data,
  P9 pages, P9.T15 camera/video scope guard, and P11.T1 parity checklist.
- Spec 7, Additional Functional Scope: covered by P2 event/dispatch, P3
  runtime/log/sim controls, P5 flight/PX4, P6 operations, payload, perception,
  config, rosbag, P7 map, and P9 UI pages.
- Spec 8, Safety And Command Gating: covered by P2 auth/session, P2.T8
  dispatch, P3 fail-closed runtime gating, P5 flight gating/hold
  reconciliation, P6.T6 permission classification, and P9 interaction
  primitives/pages.
- Spec 9, Deployment Model: covered by P8 GC proxy/compose and P10 runtime
  config, mDNS advertisement, devcontainer, real-profile docs, compose, and
  TLS/network trust decision.
- Spec 10, Open Design Questions: resolved into concrete tasks where decided;
  any remaining open/deferred item is tracked by P9.T15, P10.T5, P11, and P12.
- Spec 11, Tests And Acceptance Criteria: covered by per-task test sections
  and P11 full-system verification.
- Spec 12, Open Risks And Dependencies: covered by task-specific acceptance
  plus P12 risk closure.
- Spec 11 Current Recommendation / Spec 12 Information Architecture: covered
  by P9 frontend shell, dashboard, workflow pages, global status bar, and map
  tasks.

## Spec-To-Backlog Coverage Matrix

Updated 2026-05-27. This matrix is the implementation-agent index for mapping
`src/III-Drone-GC/docs/gui-v2-spec.md` to completed backlog work, acceptance
evidence, and explicit deferred/non-goal decisions.

| Spec section / decision area | Backlog owner tasks | Evidence | Status / deferred note |
| --- | --- | --- | --- |
| 1 Purpose: replace Tk GUI with web operator interface | P0-P2, P8-P11 | GUI v2 frontend/proxy/runtime packages, `docs/ground-control-and-operator-tools.md`, P11 parity | Covered |
| 2 Fixed Decisions and 2.1 resolved summary | P0 package/submodule prep, P1 contracts, P2 runtime API/auth, P8 proxy, P9 frontend, P10 deployment | `III-Drone-GC`, `III-Drone-Runtime`, `III-Drone-Contracts`; docs and tests | Covered |
| 3 Non-goals: no MCP/browser ROS/QGC/mobile/audio/raw JSON/control joystick | P5.T0, P8.T0, P9.T1, P9.T15, P10.T3, P11.T1, P11.T4 | Import-boundary tests, parity doc, camera/video scope guard, security checklist | Covered; camera/video stream deferred explicitly |
| 4 Runtime architecture and executor model | P1, P2, P3, P4, P5, P6 | `iii_drone_runtime/api/app.py`, state bus, command dispatch, runtime daemon client, ROS cache/adapters | Covered |
| 4.1 Runtime API shape: REST/WebSocket/state domains/events | P1.T0-P1.T3, P2.T0-P2.T8, P11.T0 | contracts, generated TS, runtime API tests, frontend store/tests | Covered |
| 4.2 ROS status and health sources | P4.T0-P4.T4 | `SupervisionHealthCache`, mission/custom-operation status caches, typed interfaces/tests | Covered |
| 4.3 III runtime API: daemon/systemd/bootstrap/logs/CLI | P2.T0, P3.T0-P3.T6, P10.T2, P11.T0 | runtime commands, daemon client, CLI remote tests, systemd unit, full suite | Covered |
| 5 Ownership and shared code | P0.T0-P0.T5, P1, P8.T1, P11.T5 | package READMEs, dependency-boundary tests, docs sweep | Covered |
| 6 Preserve Tk GUI functionality | P6, P7, P9, P9.T15, P11.T1 | `gui-v2-parity.md`, frontend pages, runtime handlers, map/camera scope docs | Covered; live video stream deferred |
| 7 Additional functional scope: runtime/logs/sim/PX4/config/rosbag/map | P3, P5, P6, P7, P9, P10, P11.T2 | runtime API endpoints, frontend workflow pages, sim E2E smoke artifacts | Covered |
| 8 Safety and command gating | P2.T8, P3 runtime mutation gate, P5 flight gate, P6 permissions, P9 press-and-hold UI, P11.T4 | handler metadata, command-gating tests, security tests, real-profile checklist | Covered |
| 8.1 Operational permission model | P5.T4-P5.T7, P6.T6, P9 page gating | payload/perception/config/operation/flight tests | Covered |
| 8.2 Operator session authority | P2 auth/session tasks, P11.T4 | browser-session tests, CLI auth policy tests, security checklist | Covered |
| 9 Deployment model | P8, P10, P11.T2, P11.T3, P11.T4, P11.T5 | compose files, deployment/security docs, sim smoke script, real checklist | Covered |
| 9.1 Runtime API dependency surface | P0, P2, P8, P10, P11.T5 | dependency-boundary tests, runtime/GC READMEs, config docs | Covered |
| 10 Open design questions | P9.T15, P10.T5, P11, P12.T1 | resolved/deferred notes in spec, parity/security/real docs, risk register | Covered by decisions or P12.T1 closure |
| 11.1 Contracts acceptance | P1, P11.T0 | Pydantic contracts, TS generation, freshness check in full suite | Covered |
| 11.2 Runtime API acceptance | P2-P7, P11.T0, P11.T2, P11.T4 | runtime API tests, sim smoke, security tests | Covered |
| 11.3 Ground-control proxy acceptance | P8, P10.T4, P11.T0, P11.T4 | proxy tests, compose tests, sim smoke, open-proxy regression | Covered |
| 11.4 Frontend acceptance | P9, P11.T0-P11.T2 | frontend tests/build, sim smoke, parity doc | Covered |
| 11.5 CLI remote migration | P3.T6, P11.T0, P11.T4, P11.T5 | CLI remote API client/tests, CLI docs, active-GUI conflict tests | Covered |
| 11.6 End-to-end acceptance | P11.T1-P11.T4 | sim E2E smoke, real-profile checklist, parity checklist, security checklist | Covered; real flight/lab run remains manual acceptance |
| 12 Open risks and dependencies | P12.T1 | risk register/checklist with evidence references | Pending P12.T1 |
| Current recommendation / information architecture | P9, P11.T1, P11.T2 | app shell, workflow pages, dashboard, map page, parity/smoke docs | Covered |

No unmapped spec section remains. P12.T1 owns the final risk register so
accepted/deferred risks are explicit before declaring GUI v2 complete.

Non-goals:
- Do not make the browser talk to MCP.
- Do not make `III-Drone-GC` depend on ROS or `III-Drone-Interfaces`.
- Do not implement browser joystick/stick control.
- Do not launch/control QGroundControl from GUI v2.
- Do not implement emergency force-stop/kill override while armed, in flight,
  or vehicle state unknown.
- Do not add audio alerts or mobile/tablet support in v2.
- Do not expose raw/custom JSON operation calls in the normal operator UI.
- Do not implement high-bandwidth camera/video streaming in v2; include only
  explicit placeholders/foundation so the scope is not silently forgotten.

Validation baseline:
- Only run tests for III packages.
- Use devcontainer execution when building/testing ROS packages.
- Use `--base-paths src` for colcon commands in the devcontainer.
- Source `/opt/ros/jazzy/setup.bash` before colcon tests.

## Incomplete

No remaining tasks.

## In-Progress

No remaining tasks.

## Completed

### Completed Phase Acceptance Summary

### P0: Repository And Package Boundary Preparation

Phase acceptance:
- [x] New package/submodule boundaries are explicit and lock-governed.
- [x] Existing local CLI/devcontainer workflows remain usable during migration.
- [x] `III-Drone-GC` remains ROS-free at runtime.

### P1: Shared Contracts And Generated Frontend Types

Phase acceptance:
- [x] `III-Drone-Contracts` defines every API-facing model and command id.
- [x] Runtime API, GC proxy, and frontend use the shared contracts.
- [x] TypeScript generation is reproducible.

### P2: Runtime API Service, Daemon Control, And Auth

Phase acceptance:
- [x] `iii-runtime-api` is a separate service from `iii-system-daemon`.
- [x] It remains reachable when daemon/ROS graph is down.
- [x] It owns browser session auth and remote CLI token auth.

### P3: Runtime Control, Logs, And Remote CLI Migration

Phase acceptance:
- [x] Runtime API covers III CLI-equivalent runtime control.
- [x] Remote CLI uses runtime API, not SSH command forwarding.
- [x] Logs are available through REST and WebSocket follow.

### P4: ROS Health, System Status, And Runtime Aggregation

Phase acceptance:
- [x] Critical health/status comes through typed ROS topics.
- [x] Runtime API aggregates state without log scraping.
- [x] Gating depends on typed status fields.

### P5: PX4 Command Adapter And Fused Vehicle State

Phase acceptance:
- [x] PX4 commands use runtime-host adapter.
- [x] MAVLink/MAVSDK is primary command transport.
- [x] Safety-critical state is fused and fail-closed.

### P6: Operator Control Facade And ROS Command Paths

Phase acceptance:
- [x] Runtime API exposes all current GUI actions/services through typed
  handlers.
- [x] Custom operation actions are nonblocking.
- [x] Runtime API never exposes arbitrary ROS action/service/topic by string.

### P7: Map, Geometry, And Visualization Data Pipeline

Phase acceptance:
- [x] Runtime API transforms ROS TF/perception/powerline state into compact
  contracts.
- [x] Frontend never receives raw ROS message dependency.
- [x] Map supports agreed projections and layers.

### P8: Ground-Control Proxy And Docker Compose

Phase acceptance:
- [x] GC proxy discovers and validates runtime APIs.
- [x] Frontend and proxy run in GC Docker Compose.
- [x] GC proxy is thin, schema-aware, and not an open proxy.

### P9: Frontend Application And Workflow Pages

Phase acceptance:
- [x] React/TypeScript frontend implements all agreed pages and global state.
- [x] UI uses generated contracts.
- [x] Dashboard is diagnostic-first; controls are on workflow pages.

### P10: Deployment, Devcontainer, And Runtime Service Integration

Phase acceptance:
- [x] Sim/dev and real deployment models are documented and runnable.
- [x] Runtime API and GC stack have clear environment variables and secrets.
- [x] Network/security decisions are explicit.

### P11: Full-System Verification And Parity

Phase acceptance:
- [x] Automated tests cover contracts, runtime API, proxy, frontend, and CLI.
- [x] Sim E2E acceptance passes.
- [x] Old GUI parity checklist is satisfied.

### P12: Spec Coverage, Risk Closure, And Implementation Governance

Phase acceptance:
- [x] Every section of `src/III-Drone-GC/docs/gui-v2-spec.md` maps to backlog
  tasks, non-goals, or explicit future/deferred work.
- [x] Open risks have owners, acceptance criteria, and verification steps.
- [x] Implementation can proceed incrementally without rediscovering scope.

#### P12.T1: Close Open Risks Before Declaring GUI v2 Complete

Description:
Turn the spec's open risks and dependencies into a completion gate. GUI v2 is
not considered complete until each risk is either implemented, tested,
documented as an accepted risk, or explicitly deferred.

Risks closed:
- submodule split and lock governance.
- runtime API bootstrap/systemd permissions.
- remote CLI migration away from SSH command forwarding.
- mDNS blocking and manual endpoint fallback.
- browser password/CLI token security and TLS/trusted-network decision.
- PX4 MAVLink/MAVSDK availability over FCU Ethernet.
- fused PX4/ROS fail-closed state.
- required typed ROS health/status topics.
- configuration manifest/snapshot semantics.
- rosbag recorder ownership/reconciliation/export.
- map/perception geometry shaping and stale-source behavior.
- all-logs/ROS-node log source model.
- full-scope implementation size and incremental testability.

Acceptance:
- [x] Risk register/checklist exists with owner task references.
- [x] Each risk has evidence: automated test, manual checklist item, doc, or
  accepted/deferred decision.
- [x] No open risk remains unclassified at final acceptance.

Implementation notes:
- Added `src/III-Drone-GC/docs/gui-v2-risk-register.md` as the GUI v2
  completion gate for all open spec risks, including owner task references,
  classifications, evidence, and final gate checks.
- Linked the risk register from `src/III-Drone-GC/README.md` and the open-risk
  section in `src/III-Drone-GC/docs/gui-v2-spec.md`.
- Added `src/III-Drone-GC/test/test_gui_v2_risk_register_doc.py` so the risk
  list and final-gate language remain covered by automated documentation tests.

Tests:
- `python3 -m pytest src/III-Drone-GC/test/test_gui_v2_risk_register_doc.py -q`
  passed in the devcontainer.

#### P12.T0: Maintain Spec-To-Backlog Coverage Matrix

Description:
Create and maintain a coverage matrix that maps each spec section and major
decision to the backlog task(s) that implement or verify it.

The matrix can live in this backlog, the GUI v2 spec, or a companion document,
but it must be easy for implementation agents to use. It should cover:
- architecture/package boundaries.
- authentication/session/heartbeat.
- discovery/proxy/deployment.
- runtime API and ROS executor model.
- PX4/MAVSDK/uXRCE state.
- operational permissions and command gating.
- runtime control, logs, CLI migration.
- configuration, rosbag, payload, perception, operations.
- map/geometry/camera-video scope.
- frontend information architecture.
- tests and acceptance criteria.

Acceptance:
- [x] Coverage matrix exists.
- [x] Each GUI v2 spec section has at least one mapped backlog task, non-goal,
  or deferred/future note.
- [x] Any unmapped item creates a backlog task before implementation proceeds.

Implementation notes:
- Expanded the backlog's spec coverage audit into a `Spec-To-Backlog Coverage
  Matrix` mapping every GUI v2 spec section and major decision area to owner
  tasks, evidence, and deferred/manual notes.
- Confirmed no unmapped spec section remains; P12.T1 owns the final risk
  register gate.

Tests:
- [x] Manual spec/backlog review.

#### P11.T5: Add Documentation Update Sweep

Description:
Update docs to reflect new architecture:
- `src/III-Drone-GC/docs/gui-v2-spec.md` if decisions changed.
- `docs/ground-control-and-operator-tools.md`.
- `docs/runtime-launch-and-node-graph.md`.
- `docs/build-and-environments.md`.
- `src/III-Drone-Supervision/docs/architecture.md`.
- new READMEs for `III-Drone-Runtime` and `III-Drone-Contracts`.
- CLI remote-profile docs.

Acceptance:
- [x] Docs no longer describe Tk GUI as the primary operator GUI.
- [x] Runtime API and GC proxy architecture are documented.
- [x] CLI remote behavior is documented.
- [x] Deployment commands are documented.

Implementation notes:
- Rewrote `docs/ground-control-and-operator-tools.md` around GUI v2,
  `iii-runtime-api`, the GC proxy, legacy Tk reference status, remote CLI
  behavior, and compose/smoke commands.
- Updated runtime launch, build/environment, and supervision architecture docs
  to describe `iii-runtime-api` as the authenticated network control plane over
  daemon/ROS/MAVLink surfaces.
- Updated Runtime and Contracts READMEs with full-suite, generated TypeScript,
  and GUI v2 acceptance/security doc links.
- Updated CLI README to document remote runtime API control and SSH-only
  deployment/admin workflows.

Tests:
- [x] Documentation review with `rg` confirmed stale Tk-primary and SSH-forwarded
  remote-runtime-control wording was removed from the swept docs.

#### P11.T4: Add Security And Network Safety Verification

Description:
Verify:
- minimal unauthenticated identity only.
- no detailed state/logs before login.
- one active browser session.
- GC proxy not open proxy.
- remote CLI token auth separate from browser password.
- mutating remote CLI blocked during active GUI session.
- no secrets committed.

Acceptance:
- [x] Security checklist exists.
- [x] Automated tests cover open-proxy and unauthenticated access.

Implementation notes:
- Updated `src/III-Drone-GC/docs/gui-v2-security-checklist.md` with the exact
  automated verification command and coverage expectations.
- Fixed the GC proxy URL builder so absolute caller-supplied paths under
  `/proxy/...` cannot override the selected runtime base URL.
- Added HTTP and WebSocket proxy regression tests for absolute upstream paths.
- Added unauthenticated runtime API coverage for detailed state, logs, events,
  map, configuration, and command surfaces.
- Secret scan found only documented/test/dev placeholders; no real deployment
  secrets were found.

Tests:
- [x] `python3 -m pytest src/III-Drone-Runtime/test/test_runtime_api_skeleton.py src/III-Drone-Runtime/test/test_browser_session.py src/III-Drone-Runtime/test/test_cli_auth_policy.py src/III-Drone-Runtime/test/test_runtime_api_config.py src/III-Drone-GC/test/test_v2_proxy_targets.py src/III-Drone-GC/test/test_v2_proxy_forwarding.py -q`
  in devcontainer: 33 passed.

#### P11.T3: Add Real-Profile Acceptance Checklist

Description:
Create manual real-profile checklist:
- GC computer discovers drone runtime API.
- GC computer has no ROS/DDS/MAVSDK.
- frontend remains available during disconnection.
- reconnect restores state without queued commands.
- runtime API status distinguishes API/daemon/socket/booted/active.
- MAVSDK/MAVLink state is visible.
- dangerous runtime mutations blocked while armed/in-flight/unknown.

Acceptance:
- [x] Checklist exists.
- [x] Each item references UI/API evidence to collect.

Implementation notes:
- Added `src/III-Drone-GC/docs/gui-v2-real-profile-acceptance.md` with
  check/procedure/evidence/pass-condition rows for real-profile discovery,
  ROS-free GC operation, disconnect/reconnect behavior, runtime status
  evidence, MAVLink/PX4 state, command gating, logs/rosbags, mDNS fallback, and
  logout/session release.
- Linked the checklist from the GC README.
- Added a documentation regression test for required real-profile evidence
  topics.

Tests:
- [x] `python3 -m pytest src/III-Drone-GC/test/test_gui_v2_real_profile_acceptance_doc.py -q`
  in devcontainer: 1 passed.

#### P11.T2: Add Sim End-To-End Smoke Scenario

Description:
Create a repeatable sim smoke test:
- start GC stack.
- discover local runtime API.
- authenticate.
- boot/start runtime from GUI/API.
- observe dashboard updates.
- observe fused PX4 once PX4/Gazebo available.
- arm, takeoff, hold, land with confirmations.
- activate Custom Operation mode.
- validate/start/cancel safe operation.
- run gripper/perception/config/rosbag/log/map workflows.

Acceptance:
- [x] Scenario documented and preferably scriptable.
- [x] Artifacts/logs captured.
- [x] Failures produce actionable diagnostics.

Implementation notes:
- Added `scripts/workspace/gui_v2_sim_e2e_smoke.py`, a repeatable sim smoke
  runner that can start the GC compose stack, select the local sim runtime,
  authenticate through the proxy, read every operator state domain, and capture
  per-step JSON artifacts plus compose logs.
- Added guarded `--run-mutating-workflows` and `--run-flight-commands` flags for
  the sim-only command/flight extension. The default mode is read-only and will
  not arm/takeoff/land unless those flags are explicitly supplied.
- Added `src/III-Drone-GC/docs/gui-v2-sim-e2e-smoke.md` and linked it from
  `docs/testing.md` and the GC README.

Tests:
- [x] `python3 -m py_compile scripts/workspace/gui_v2_sim_e2e_smoke.py`.
- [x] `III_GC_FRONTEND_PORT=5174 scripts/workspace/gui_v2_sim_e2e_smoke.py --start-compose`
  on the host with sim `iii-runtime-api` running: 26 HTTP steps passed,
  including runtime identity/profile check, proxy discovery/manual endpoint,
  target validation/selection, browser login, runtime/system/vehicle/control/
  mission/operation/payload/perception/powerline/configuration/map/rosbag/logs/
  command-handler/event reads, and logout. Artifacts:
  `log/gui-v2-sim-e2e-smoke/20260527T182926Z/summary.json`.

#### P11.T1: Add Legacy GUI Parity Checklist Test/Doc

Description:
Create a parity checklist mapping every current Tk GUI diagnostic/command to
new runtime API domain/page coverage.

Current functionality to map:
- drone location/status.
- armed/offboard.
- target status/known.
- on-cable ID.
- ground altitude estimate.
- current maneuver/status.
- reference client mode.
- PL mapper/state/status.
- PL direction/Hough status.
- stored powerline status.
- battery/charging/charger/gripper.
- gripper commands.
- PL mapper commands.
- update powerline overview.
- parameter workflows.

Acceptance:
- [x] Checklist exists.
- [x] Each old GUI item maps to a new page/domain/API handler or an explicit
  superseded workflow.
- [x] Legacy live image/visualization paths are mapped to Map/Perception or an
  explicit deferred camera/video item.
- [x] No old GUI functionality is silently dropped.

Implementation notes:
- Expanded `src/III-Drone-GC/docs/gui-v2-parity.md` from a visualization-only
  note into a full diagnostic, command, workflow, and camera/video parity
  checklist covering `gui.py`, `gui_original.py`, and `gc_node.py`.
- Linked the parity checklist from `src/III-Drone-GC/README.md`.
- Added a documentation regression test to keep required legacy surfaces listed.

Tests:
- [x] `python3 -m pytest src/III-Drone-GC/test/test_gui_v2_parity_doc.py -q`
  in devcontainer: 1 passed.

#### P11.T0: Add Contract, Runtime, Proxy, Frontend Test Suites To CI/Docs

Description:
Collect all test commands into package docs and CI-like scripts if available.
Ensure implementation agents know how to run targeted tests only for III
packages.

Acceptance:
- [x] Test commands documented.
- [x] Generated TypeScript freshness check documented.
- [x] No tests invoke non-III third-party package tests.

Implementation notes:
- Updated `scripts/workspace/run_iii_test_suite.sh` to build required
  dependencies, test only III packages, check generated TypeScript freshness,
  run frontend install/lint/typecheck/test/build, run top-level integration
  tests, and run CLI tests.
- Added `docs/testing.md` and linked it from root/doc indexes.
- Reconciled stale CLI tests with the runtime-owned daemon client contract.

Tests:
- [x] `python3 -m pytest tools/III-Drone-CLI/test -q` in devcontainer:
  22 passed.
- [x] `./scripts/workspace/run_iii_test_suite.sh` in devcontainer:
  ROS package tests 368 passed, frontend Vitest 82 passed, top-level
  integration 3 passed, CLI 22 passed. Frontend tests still emit React
  `act(...)` warnings; they are non-failing but should be cleaned up in a
  future quality pass.

#### P10.T5: Decide And Document TLS / Network Trust Model

Description:
Close the open deployment decision around transport security. Decide whether
the first deployment relies on a trusted isolated operator network or requires
TLS for `iii-runtime-api` and GC proxy connections.

At minimum, document:
- expected network topology in sim and real profiles.
- exposed ports on runtime host and GC computer.
- password/token handling.
- whether TLS is required, optional, or deferred.
- what risks remain if using a trusted isolated network.

Acceptance:
- [x] Deployment docs contain a concrete v2 security/network position.
- [x] Runtime API and GC proxy config match that position.
- [x] Any deferred TLS work is recorded as future work with explicit risk.
- [x] Security verification checklist references the chosen model.

Tests:
- [x] Documentation review.
- [x] Config smoke test for the chosen deployment mode.

Implementation notes:
- Closed the initial deployment decision as trusted isolated operator network
  with runtime API browser-password authentication and CLI-token authentication.
- Documented TLS as deferred for first field deployment, including passive
  observation/replay, mDNS spoofing, and shared-credential attribution risks.
- Added `gui-v2-security-checklist.md` with real-profile secret, firewall,
  mDNS, CORS, endpoint validation, unauthenticated surface, static-asset, and
  deferred-TLS checks.
- Updated GUI v2 deployment docs, runtime API configuration docs, and the GUI
  v2 spec to reference the chosen model and deferred TLS work.
- Added a regression test that checks the security docs continue to state the
  trusted-network decision, TLS deferral, required secret enforcement, port
  controls, and endpoint identity caveat.

Changed files:
- `src/III-Drone-GC/docs/gui-v2-deployment.md`
- `src/III-Drone-GC/docs/gui-v2-security-checklist.md`
- `src/III-Drone-GC/docs/gui-v2-spec.md`
- `src/III-Drone-GC/README.md`
- `src/III-Drone-GC/test/test_gc_compose_stack.py`
- `src/III-Drone-Runtime/docs/runtime-api-configuration.md`

Verification:
- Documentation review with `rg` confirmed trusted-network decision, TLS
  deferral, required secrets, CORS, runtime/GC/mDNS port controls, default
  secret replacement, and the mDNS identity caveat.
- `docker compose -f src/III-Drone-GC/docker-compose.prod.yml config` and
  `docker compose -f src/III-Drone-GC/docker-compose.dev.yml config` confirmed
  the HTTP trusted-network compose mode.
- Host `python -m pytest src/III-Drone-GC/test/test_gc_compose_stack.py -q`
  passed: 3 tests.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  342 tests, 0 errors, 0 failures.
- ASCII check passed for the updated security/deployment docs.

#### P10.T4: Add GC Production Compose And Development Compose

Description:
Add GC Compose files:
- development: Vite frontend + GC proxy.
- production: static frontend + GC proxy.

Expose both frontend and proxy ports on the GC computer for v2.

Acceptance:
- [x] Compose files exist and validate.
- [x] Frontend can be served from GC computer.
- [x] GC proxy can discover/select runtime API.
- [x] Runtime static assets are not served from drone/runtime host.

Tests:
- [x] `docker compose config`
- [x] Local compose smoke test.

Implementation notes:
- Added standalone `docker-compose.dev.yml` for Vite frontend plus GC proxy.
- Added standalone `docker-compose.prod.yml` for static frontend plus GC proxy,
  and aligned the default `docker-compose.yml` with the production stack.
- Configured the GC proxy with host networking so mDNS discovery can observe
  `_iii-runtime-api._tcp.local` on the operator network; the frontend remains
  exposed through `III_GC_FRONTEND_PORT`.
- Documented the compose entrypoints and host-networking reason in the GUI v2
  deployment note.
- Extended compose regression tests to cover dev/prod/default compose files and
  to keep ROS/DDS/MAVSDK/PX4 dependencies out of the GC containers.

Changed files:
- `src/III-Drone-GC/docker-compose.yml`
- `src/III-Drone-GC/docker-compose.dev.yml`
- `src/III-Drone-GC/docker-compose.prod.yml`
- `src/III-Drone-GC/docs/gui-v2-deployment.md`
- `src/III-Drone-GC/test/test_gc_compose_stack.py`

Verification:
- `docker compose -f src/III-Drone-GC/docker-compose.dev.yml config`
- `docker compose -f src/III-Drone-GC/docker-compose.prod.yml config`
- `docker compose -f src/III-Drone-GC/docker-compose.yml config`
- Host `python -m pytest src/III-Drone-GC/test/test_gc_compose_stack.py -q`
  passed.
- Production compose smoke with `III_GC_FRONTEND_PORT=5174` built and started
  the stack, served the static frontend, returned GC proxy identity, discovered
  local `iii-runtime` through mDNS, validated the runtime endpoint, selected it,
  and was cleaned down with `docker compose down`.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  341 tests, 0 errors, 0 failures.

#### P10.T3: Add Real Profile Deployment Notes

Description:
Document real-profile deployment:
- runtime API runs on onboard host.
- GC frontend/proxy runs on ground-control computer.
- GC computer needs no ROS/DDS/MAVSDK.
- frontend/proxy connects over network to runtime API.
- static frontend survives runtime disconnection.
- TLS vs trusted isolated network is a deployment decision.

Acceptance:
- [x] Docs exist in relevant package README/docs.
- [x] Environment variables for real profile are listed.
- [x] Network ports are documented.

Tests:
- [x] Documentation review.

Implementation notes:
- Added `src/III-Drone-GC/docs/gui-v2-deployment.md` covering sim/dev and real
  two-host topology, onboard runtime API placement, GC proxy/frontend placement,
  static frontend disconnected behavior, real runtime environment variables,
  GC environment variables, and exposed ports.
- Linked the deployment note from both `III-Drone-GC` and `III-Drone-Runtime`
  package READMEs.
- Extended runtime API configuration docs with the real-profile environment,
  runtime-host ports, and the rule that ROS/DDS/MAVSDK/daemon/systemd access
  remains local to the runtime host.
- Left the TLS versus trusted isolated operator-network choice as the explicit
  security decision for P10.T5.

Changed files:
- `src/III-Drone-GC/docs/gui-v2-deployment.md`
- `src/III-Drone-GC/README.md`
- `src/III-Drone-Runtime/README.md`
- `src/III-Drone-Runtime/docs/runtime-api-configuration.md`

Verification:
- Documentation review with `rg` confirmed real-profile env vars, GC env vars,
  runtime/GC ports, static frontend disconnected behavior, and TLS/trusted
  network decision reference.
- ASCII check passed for the added/updated docs.

#### P10.T2: Integrate Runtime API Into Devcontainer Startup

Description:
Update `.devcontainer/post_start.sh`, systemd install files, and setup scripts
so `iii-runtime-api` autostarts alongside daemon in devcontainer sim profile.

Acceptance:
- [x] Runtime API service installed/enabled/restarted in devcontainer.
- [x] `iii system boot/start` local workflows still work.
- [x] GC stack can discover local runtime API in sim.

Tests:
- [x] Devcontainer service smoke test.

Implementation notes:
- Added post-start Python dependency refresh from `requirements.txt` so
  existing devcontainers pick up runtime API dependencies such as FastAPI,
  Uvicorn, and zeroconf for the `iii` service user.
- Completed the runtime API systemd unit with sim-profile environment,
  mDNS enablement, stable devcontainer discovery metadata, optional local
  environment override file, and runtime log directory creation.
- Fixed `iii_drone_runtime.api.main` so `python3 -m
  iii_drone_runtime.api.main` actually starts the API process under systemd.
- Moved blocking zeroconf register/unregister calls off the FastAPI event loop
  and removed the empty `path` TXT value so GC discovery builds
  `http://127.0.0.1:8765` instead of an invalid `None` suffix.
- Extended systemd service regression tests to cover post-start dependency
  refresh, runtime unit mDNS/profile configuration, optional env file, and
  module entrypoint execution guard.

Changed files:
- `.devcontainer/post_start.sh`
- `tools/systemd/iii-runtime-api.service`
- `src/III-Drone-Runtime/iii_drone_runtime/api/app.py`
- `src/III-Drone-Runtime/iii_drone_runtime/api/main.py`
- `src/III-Drone-Runtime/iii_drone_runtime/api/mdns.py`
- `src/III-Drone-Runtime/test/test_runtime_api_config.py`
- `src/III-Drone-Runtime/test/test_systemd_service_files.py`

Verification:
- Host `python -m compileall -q src/III-Drone-Runtime/iii_drone_runtime
  src/III-Drone-Runtime/test/test_runtime_api_config.py
  src/III-Drone-Runtime/test/test_system_adapter.py
  src/III-Drone-Runtime/test/test_systemd_service_files.py`
- Devcontainer rebuilt `iii_drone_runtime`, installed/restarted
  `iii-runtime-api.service`, verified `iii-system-daemon.service` and
  `iii-runtime-api.service` active, and fetched
  `http://127.0.0.1:8765/identity`.
- Devcontainer GC discovery smoke found `iii-runtime` via mDNS with base URL
  `http://127.0.0.1:8765`.
- Devcontainer `iii system status` reported the sim runtime booted and managed
  services/processes alive.
- Devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` passed: 341 tests, 0 errors, 0 failures.

#### P10.T1: Add mDNS Advertisement For Runtime API

Description:
`iii-runtime-api` advertises `_iii-runtime-api._tcp.local` with metadata:
instance/system name, host, port, profile, system/drone identifier, and API
version when available.

Acceptance:
- [x] Advertisement starts with runtime API service.
- [x] Minimal identity endpoint matches advertised metadata.
- [x] Service can run without exposing operational state unauthenticated.

Tests:
- [x] Advertisement tests with fake zeroconf if possible.

Implementation notes:
- Added `iii_drone_runtime.api.mdns` with `_iii-runtime-api._tcp.local.`
  service registration, deterministic metadata properties, address/server
  resolution, and clean unregister/close behavior on shutdown.
- Wired runtime API startup/shutdown to start and stop mDNS advertisement when
  `III_RUNTIME_API_MDNS_ENABLED` is enabled; programmatic tests can inject a
  fake advertiser.
- Added environment and documentation entries for mDNS enablement, instance
  name, advertised host, system ID, and unauthenticated discovery boundaries.
- Moved `/runtime/status` behind browser-session authentication so
  unauthenticated runtime discovery is limited to `/identity` and `/health`.
- Added fake-advertiser and fake-zeroconf tests covering lifecycle behavior and
  advertised metadata parity with `/identity`.

Changed files:
- `src/III-Drone-Runtime/iii_drone_runtime/api/app.py`
- `src/III-Drone-Runtime/iii_drone_runtime/api/mdns.py`
- `src/III-Drone-Runtime/config/iii-runtime-api.env.example`
- `src/III-Drone-Runtime/docs/runtime-api-configuration.md`
- `src/III-Drone-Runtime/package.xml`
- `src/III-Drone-Runtime/setup.py`
- `src/III-Drone-Runtime/test/test_runtime_api_config.py`
- `src/III-Drone-Runtime/test/test_system_adapter.py`

Verification:
- Host `python -m compileall -q src/III-Drone-Runtime/iii_drone_runtime
  src/III-Drone-Runtime/test/test_runtime_api_config.py
  src/III-Drone-Runtime/test/test_system_adapter.py`
- Devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` passed: 341 tests, 0 errors, 0 failures.

#### P10.T0: Add Runtime API Configuration And Secrets

Description:
Define environment/config for:
- browser password.
- remote CLI token.
- runtime API host/port.
- mDNS instance name/system ID/profile metadata.
- MAVLink endpoint.
- session heartbeat settings.
- log paths.

Acceptance:
- [x] Example env/config files exist.
- [x] Secrets are not committed with real values.
- [x] Runtime API fails clearly if required secrets/config missing.

Tests:
- [x] Config loading unit tests.

Implementation notes:
- Extended `RuntimeApiSettings` to cover host, port, mDNS instance, system id,
  secrets, session lease, PX4 MAVLink endpoint, PX4 enable flag, and log dir.
- Added real-profile/explicit secret enforcement with clear missing-env errors
  while preserving sim/dev defaults.
- Added runtime API env example and configuration documentation.

Changed files:
- `src/III-Drone-Runtime/iii_drone_runtime/api/app.py`
- `src/III-Drone-Runtime/iii_drone_runtime/api/main.py`
- `src/III-Drone-Runtime/config/iii-runtime-api.env.example`
- `src/III-Drone-Runtime/docs/runtime-api-configuration.md`
- `src/III-Drone-Runtime/test/test_runtime_api_config.py`

Verification:
- Host `python -m compileall -q src/III-Drone-Runtime/iii_drone_runtime
  src/III-Drone-Runtime/test/test_runtime_api_config.py`
- Host `pytest src/III-Drone-Runtime/test/test_runtime_api_config.py` could not
  run because `pytest` is not installed on the host.
- Devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` passed: 338 tests, 0 errors, 0 failures.

#### P9.T15: Add Camera/Video Placeholder And Scope Guard

Description:
Represent camera/video as a deliberate future extension without implementing
streaming in v2. The UI and parity checklist should not imply that a
high-bandwidth image stream is available when the runtime link cannot support
it.

Implementation expectations:
- If legacy GUI had image/visualization paths, map each one in the parity
  checklist to either the new Map/Perception workflow or an explicit deferred
  camera/video item.
- Frontend may show a disabled camera/video panel or diagnostic note only where
  useful, but should not reserve primary dashboard space for unavailable
  streaming.
- Runtime/API contracts should leave room for future stream metadata without
  adding an active stream transport.

Acceptance:
- [x] No v2 code attempts high-bandwidth camera/video streaming.
- [x] Legacy image/visualization parity is explicitly mapped to Map/Perception
  or deferred camera/video scope.
- [x] Any disabled camera/video UI has a clear reason and does not look broken.
- [x] Future stream metadata is documented without implementing transport.

Tests:
- [x] Parity checklist review.
- [x] Frontend rendering test for no camera/video stream available.

Implementation notes:
- Added a disabled Camera/Video scope panel to the Map page with an explicit
  reason and no player or stream transport.
- Added GUI v2 parity checklist documenting that legacy `put_img()`/`label_viz`
  visualization maps to Map/Perception while high-bandwidth camera/video is
  deferred.
- Extended the spec with reserved future stream metadata guidance without
  adding an active stream contract.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/MapPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MapPage.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- `src/III-Drone-GC/docs/gui-v2-spec.md`
- `src/III-Drone-GC/docs/gui-v2-parity.md`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- MapPage`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- `rg` scan for camera/video stream implementation found only intentional
  deferred docs/UI references.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T14: Implement Map Page And Dashboard Mini-Map

Description:
Map page includes:
- powerline-orthogonal 2D projection.
- top-down 2D projection.
- selectable or side-by-side views.
- layer toggles: live perception, stored overview, drone trail, target history,
  trajectory/path, labels.
- auto-fit default.
- manual pan/zoom disables auto-fit until recenter/auto-fit.
- no simulation ground-truth geometry.

Dashboard includes compact diagnostic map.

Acceptance:
- [x] Stored overview on if available.
- [x] Live perception on.
- [x] Short drone trail on.
- [x] Target history on during operations.
- [x] Trajectory/path on when available.
- [x] Labels avoid clutter.
- [x] No-reference degraded state visible.

Tests:
- [x] Map rendering tests for empty/live/stored/combined states.

Implementation notes:
- Added reusable SVG `MapView` for powerline-orthogonal and top-down map
  projections with stored overview, live perception, trajectory, drone trail,
  target history, drone, target, and bounded label layers.
- Added Map page with projection controls, side-by-side mode, layer toggles,
  auto-fit default, and manual pan/zoom state that stays disabled until
  Recenter/auto-fit.
- Added compact dashboard mini-map fed by the same `MapState` contract and no
  simulation ground-truth layer.

Changed files:
- `src/III-Drone-GC/frontend/src/components/MapView.tsx`
- `src/III-Drone-GC/frontend/src/components/mapTypes.ts`
- `src/III-Drone-GC/frontend/src/components/index.ts`
- `src/III-Drone-GC/frontend/src/pages/MapPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/MapPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/Dashboard.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- MapPage`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T13: Implement Logs Page

Description:
Logs page includes:
- source list.
- daemon logs.
- runtime API logs.
- daemon-managed service logs.
- managed entity/ROS node logs.
- all-logs view.
- follow.
- search/filter.
- download/export current view.

Use REST for source listing/history/download and WebSocket for live follow.

Acceptance:
- [x] Logs page is login-gated.
- [x] All-logs view source labels lines.
- [x] Follow can be stopped/changed cleanly.

Tests:
- [x] Logs page tests.

Implementation notes:
- Added a runtime logs client abstraction for proxied REST source/tail/download
  calls and proxied WebSocket follow streams.
- Added Logs page with login gating, source list, tail history, all-logs view,
  search/filter, export current view, and follow start/stop controls.
- Follow handles are closed when stopped, when changing sources, and on page
  unmount.

Changed files:
- `src/III-Drone-GC/frontend/src/api/logs.ts`
- `src/III-Drone-GC/frontend/src/pages/LogsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/LogsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- LogsPage`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T12: Implement Rosbags Page

Description:
Rosbags page includes:
- current recorder state.
- owner/source: GUI/manual or mission/runtime when known.
- manual start/stop.
- list recordings.
- download/export through GC proxy/runtime API.

Controls are available across operational modes. Starting/stopping during
Mission mode requires press-and-hold. Stopping mission/runtime-owned recording
shows ownership warning and requires press-and-hold.

Acceptance:
- [x] Recorder state continuously reconciles.
- [x] Manual commands are available with required confirmations.
- [x] Downloads stream through proxy.

Tests:
- [x] Rosbag page tests.

Implementation notes:
- Added Rosbags page with recorder status, owner/source, active recording
  metadata, manual start/stop controls, and recordings list.
- Start/stop controls stay available across modes and switch to press-and-hold
  for Mission mode. Mission/runtime-owned stops show ownership warning and
  require press-and-hold confirmation.
- Downloads use an injectable GC-proxy streaming callback when available, with
  `rosbag.download` runtime command fallback for download metadata.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RosbagsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- RosbagsPage`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T11: Implement Configuration Page

Description:
Configuration page includes:
- structured parameter manifest browser.
- search/filter.
- staged edits.
- per-parameter apply/reset.
- batch apply/reset.
- save/load/snapshot/default workflows.
- restart-required grouping/indicators.
- validation errors.
- pending edits navigation guard: Stay or Discard only.

Snapshot confirmation:
- download immediate.
- save new immediate.
- overwrite existing confirm.
- set default press-and-hold.
- load press-and-hold.
- warn when setting default while runtime active.

Acceptance:
- [x] No raw YAML/text editing as main workflow.
- [x] Pending edits are frontend-only until apply.
- [x] `Pending edits`, `Unsaved`, `Non-default`, and restart-required badges
  follow spec semantics.
- [x] Navigation away prompts Stay/Discard.

Tests:
- [x] Configuration page tests.

Implementation notes:
- Added a structured parameter manifest browser driven by generated
  configuration contracts and runtime snapshot manifest data.
- Added search/filter, local-only staged edits, validation, per-parameter
  apply/reset, and batch apply/reset using `configuration.apply`.
- Added snapshot list, download/list commands, save-new flow, explicit
  overwrite confirmation, press-and-hold load/default controls, and active
  runtime default warning.
- Added AppShell navigation guard that blocks leaving Configuration while
  frontend-only edits are pending and offers only Stay or Discard.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/ConfigurationPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- ConfigurationPage AppShell`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T10: Implement Perception Page

Description:
Perception page includes:
- PL mapper state/status.
- PL direction computer status.
- Hough transformer status.
- stored powerline overview status.
- PL mapper controls.
- update powerline overview.
- live vs stored powerline diagnostics.

Acceptance:
- [x] Perception status parity with old GUI.
- [x] Commands honor mode permissions.
- [x] Update overview command result visible.

Tests:
- [x] Perception page tests.

Implementation notes:
- Added the Perception workflow page with PL mapper, direction computer, Hough
  transformer, stored overview, live perception, freshness, source, and live-vs-
  stored diagnostics.
- Wired PL mapper start/pause/freeze/stop and powerline overview update through
  the shared runtime command dispatcher, including timeout parameter handling.
- Mirrored runtime permission semantics for disconnected, Mission-mode, and
  active custom-operation mutation blocking.
- Integrated the page into AppShell navigation and added compact input styling.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PerceptionPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend run lint`
- `npm --prefix src/III-Drone-GC/frontend run typecheck`
- `npm --prefix src/III-Drone-GC/frontend test -- PerceptionPage`
- `npm --prefix src/III-Drone-GC/frontend test`
- `npm --prefix src/III-Drone-GC/frontend run build`
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` passed:
  335 tests, 0 errors, 0 failures.

#### P9.T9: Implement Payload Page

Description:
Payload page includes:
- gripper status.
- open/close gripper commands.
- charger operating mode/status.
- battery voltage.
- charging power.
- needed diagnostics for permissions.

Acceptance:
- [x] Payload status parity with old GUI.
- [x] Gripper controls show disabled reasons in Mission/active operation.
- [x] Commands and rejections are displayed.

Implementation notes:
- Added Payload page with gripper, charger, charger mode, battery voltage, and
  charging power diagnostics.
- Added open/close gripper command buttons using `payload.gripper.open` and
  `payload.gripper.close`.
- Gripper controls disable with runtime-aligned reasons during Mission mode,
  active custom operation, or runtime disconnection.
- Command accept/reject results render inline and in the toast region.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/PayloadPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/PayloadPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 10 files, 50 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T8: Implement Operations Page

Description:
Operations page includes:
- Custom Operation activation button.
- typed forms for `fly_to_position`, `cable_aware_fly_to_position`,
  `fly_to_object`, `cable_landing`, `cable_takeoff`, `hover`,
  `hover_by_object`, and `hover_on_cable`.
- validate-only button where supported.
- start via press-and-hold for flight-affecting actions.
- active operation status/feedback/result.
- one-click cancel.

No raw/custom JSON operation calls.

Acceptance:
- [x] All current operation helpers have typed forms.
- [x] Coordinate frame semantics are explicit in every relevant operation form.
- [x] Frame selectors/defaults are operation-specific and disabled with clear
  reasons when required frame/context is unavailable or stale.
- [x] Required context missing/stale disables relevant forms with reasons.
- [x] Validate-only displays readiness.
- [x] Active operation feedback/result displayed.
- [x] Cancel available on page and global status bar.

Implementation notes:
- Added registry-driven typed forms for all current custom operation helpers:
  `fly_to_position`, `cable_aware_fly_to_position`, `fly_to_object`,
  `cable_landing`, `cable_takeoff`, `hover`, `hover_by_object`, and
  `hover_on_cable`.
- Coordinate-frame fields are explicit select controls with operation-specific
  defaults such as `map`, `powerline`, and `target`; no raw JSON operation
  input is exposed.
- Validate uses `custom_operation.validate`; starts use the specific operation
  command IDs with `hold_confirmed: true`.
- Starts are press-and-hold, cancel is one-click, and stale/missing vehicle,
  perception, or powerline context disables relevant forms with inline reasons.
- Active operation status, feedback, and recent custom-operation command result
  are displayed.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/OperationsPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/OperationsPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 9 files, 45 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T7: Implement Flight Page

Description:
Flight page owns:
- Arm.
- Takeoff.
- Land.
- Hold.
- Mission activation.
- Custom Operation activation.
- fused PX4 diagnostics.
- MAVSDK and ROS/uXRCE source drill-down.
- control-owner transition state.

Rules:
- Arm, Takeoff, Land, Mission activation, Custom Operation activation require
  press-and-hold.
- Hold is one-click.
- Takeoff requires armed; no implicit arm.
- Mission/custom activation require in-flight and preconditions.

Acceptance:
- [x] Flight controls are wired to runtime API.
- [x] Gating/disabled reasons match runtime API.
- [x] Source disagreement/degraded state visible.
- [x] Transitioning state visible with target and timeout warning.

Implementation notes:
- Added Flight page with Arm, Takeoff, Land, Mission activation, and Custom
  Operation activation as 1.5-second press-and-hold controls.
- Hold is a one-click urgent action.
- Frontend gating uses runtime `control.latest.command_permissions` when
  present, then falls back to the same fail-closed rules as the runtime:
  stale/missing vehicle state, takeoff requires armed, land requires in-flight,
  mission/custom activation require running system and in-flight context.
- Added fused PX4 diagnostics for MAVSDK command transport, ROS/uXRCE state,
  source disagreements, control owner, transition target, and timeout warning.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/FlightPage.tsx`
- `src/III-Drone-GC/frontend/src/pages/FlightPage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 8 files, 39 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T6: Implement Runtime Page

Description:
Runtime page provides terminal-replacement system controls:
- daemon/API status.
- boot/start/stop/restart/shutdown.
- entity list/status.
- daemon-managed service list/status.
- service start/stop/restart.
- runtime profile info.
- links to logs.

Mutating runtime controls require 1.5-second press-and-hold. Read-only actions
are normal click. Dangerous runtime mutations disabled while armed/in-flight or
vehicle state unknown/stale.

Acceptance:
- [x] Runtime page uses runtime domain and command endpoints.
- [x] Press-and-hold controls show hint on brief click.
- [x] Disabled reasons are inline.
- [x] Rejections show inline/event/toast.

Implementation notes:
- Added runtime command dispatcher boundary for
  `/proxy/commands/actions/start`.
- Added Runtime page showing daemon/API status, profile, managed entities,
  daemon services, and log links.
- Wired read-only runtime refresh/list actions as normal clicks and runtime
  mutations/service mutations as 1.5-second press-and-hold controls.
- Runtime mutations fail closed when runtime is disconnected or vehicle state
  is missing, stale, armed, or in flight.
- Command rejections render inline and in the toast region.

Changed files:
- `src/III-Drone-GC/frontend/src/api/commands.ts`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.tsx`
- `src/III-Drone-GC/frontend/src/pages/RuntimePage.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 7 files, 34 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T5: Implement Dashboard Diagnostic Overview

Description:
Dashboard is diagnostic-first, not a landing page and not the main control
surface.

Show:
- session/authentication.
- selected runtime endpoint.
- runtime/API/daemon/system status.
- fused PX4 state.
- control owner.
- active mission/maneuver/custom operation.
- perception/powerline readiness.
- payload/charger/gripper.
- rosbag state.
- config badge.
- compact map/geometry.
- event log/recent command results.

Acceptance:
- [x] Dashboard shows all major diagnostic categories.
- [x] No critical control workflow exists only on dashboard.
- [x] Stale/degraded/missing state is visually clear.

Implementation notes:
- Added diagnostic-first Dashboard page with panels for session, runtime,
  vehicle, control, mission/operation, perception/powerline, payload, rosbag,
  configuration, map/geometry, recent events, and command results.
- Dashboard uses runtime store data and selected-runtime/session props without
  adding any critical command controls on the dashboard itself.
- Panels surface `fresh`, `stale`, `degraded`, and `missing` states with
  visible status badges.

Changed files:
- `src/III-Drone-GC/frontend/src/pages/Dashboard.tsx`
- `src/III-Drone-GC/frontend/src/pages/Dashboard.test.tsx`
- `src/III-Drone-GC/frontend/src/pages/index.ts`
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 6 files, 29 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T4: Implement Global Layout, Navigation, And Status Bar

Description:
Implement app shell:
- diagnostic dashboard first after login.
- dedicated pages: Runtime, Flight, Operations, Payload, Perception,
  Configuration, Rosbags, Logs, Map.
- global bottom status bar visible on every page.

Status bar shows compact runtime/API connection, fused PX4 state,
armed/in-air/mode, control owner, active operation/mission, major health,
config badge, rosbag state, critical warnings, global one-click Hold, and
custom-operation cancel when active.

Status bar is interactive:
- runtime -> Runtime page.
- PX4/control owner -> Flight page.
- operation -> Operations page.
- config -> Configuration page.
- rosbag -> Rosbags page.
- health/warning -> relevant detail/logs.

Acceptance:
- [x] Layout renders at 1440x900.
- [x] Status bar visible on all pages.
- [x] Status bar Hold and custom-operation cancel behavior wired.
- [x] Status bar items navigate/open details.

Implementation notes:
- Added `AppShell` with dashboard-first navigation and dedicated pages for
  Runtime, Flight, Operations, Payload, Perception, Configuration, Rosbags,
  Logs, and Map.
- Added a persistent global status bar backed by runtime store summaries for
  runtime/API, PX4, control owner, operation/mission, config, rosbag, and
  health/warnings.
- Wired status bar items to page navigation and wired Hold/custom-operation
  cancel to urgent action controls.
- Added lucide icons for page and status navigation.

Changed files:
- `src/III-Drone-GC/frontend/src/layout/AppShell.tsx`
- `src/III-Drone-GC/frontend/src/layout/AppShell.test.tsx`
- `src/III-Drone-GC/frontend/src/layout/index.ts`
- `src/III-Drone-GC/frontend/src/App.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- `src/III-Drone-GC/frontend/package.json`
- `src/III-Drone-GC/frontend/package-lock.json`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 5 files, 26 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Playwright was not available in the frontend package; component tests covered
  the layout/status-bar smoke criteria.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T3: Implement State Store For Snapshot/Patch/Event Protocol

Description:
Create frontend state store keyed by runtime API domains:
system, vehicle, control, mission, operation, perception, powerline, payload,
configuration, simulation, and events.

Handle:
- full snapshot.
- domain patches.
- runtime events.
- local events.
- command results.
- stale/disconnected state.
- reconnect with backoff.
- no queued commands while disconnected.

Acceptance:
- [x] Snapshot initializes store.
- [x] Patch updates correct domain.
- [x] Runtime/local events are source-labelled.
- [x] Disconnected state marks stale data and disables commands.

Implementation notes:
- Added reducer-based runtime store keyed by every contract domain:
  system, vehicle, control, mission, operation, perception, powerline, payload,
  configuration, simulation, rosbag, and events.
- WebSocket messages reduce through snapshot, patch, event, and command-result
  paths.
- Runtime and frontend/local events are retained with explicit transport-source
  labels.
- Disconnect actions mark domain data stale, store the disabled-command reason,
  and schedule exponential reconnect delay metadata.

Changed files:
- `src/III-Drone-GC/frontend/src/state/runtimeStore.ts`
- `src/III-Drone-GC/frontend/src/state/runtimeStore.test.ts`
- `src/III-Drone-GC/frontend/src/state/index.ts`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 4 files, 21 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T2: Implement Runtime Discovery, Selection, Login, And Session UX

Description:
Implement pre-login flow:
- discovery screen.
- manual endpoint fallback.
- select one runtime.
- authenticate against selected runtime through GC proxy.
- session token in `sessionStorage`.
- page refresh survival.
- heartbeat every 2 seconds.
- disconnect/logout releases session.
- optional local memory of the last selected runtime endpoint, validated before
  reuse.

Pre-login shows only minimal identity/reachability. Everything else is login
gated.

Acceptance:
- [x] Discovery list displays minimal metadata only.
- [x] Manual endpoint validation errors are clear.
- [x] Login obtains session and opens WebSocket.
- [x] Session survives refresh.
- [x] Closing/lost heartbeat leads to release after runtime timeout.
- [x] Last selected endpoint may be remembered, but is revalidated and does not
  expose detailed state before login.

Implementation notes:
- Added a typed GC-proxy frontend client for discovery, manual endpoint
  registration, target validation/selection, proxied login/session/heartbeat,
  logout, and proxied WebSocket URL construction.
- Added `SessionGate` for minimal pre-login discovery, manual endpoint
  validation errors, target selection, session login, WebSocket open,
  two-second heartbeat, logout cleanup, stored-session restoration, and
  remembered-endpoint revalidation.
- Session tokens are stored only in `sessionStorage`; last endpoint memory uses
  `localStorage` and is revalidated before reuse.
- Pre-login UI displays only runtime name, source, address/port, schema, and
  reachability.

Changed files:
- `src/III-Drone-GC/frontend/src/api/gcProxy.ts`
- `src/III-Drone-GC/frontend/src/session/SessionGate.tsx`
- `src/III-Drone-GC/frontend/src/session/SessionGate.test.tsx`
- `src/III-Drone-GC/frontend/src/session/sessionStorage.ts`
- `src/III-Drone-GC/frontend/src/App.tsx`
- `src/III-Drone-GC/frontend/src/App.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 3 files, 16 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- `npm --prefix src/III-Drone-GC/frontend run contracts:check` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T1: Build Shared UI Interaction Primitives

Description:
Implement reusable frontend primitives used across workflow pages:
- press-and-hold command button with 1.5-second hold duration.
- brief-click hint for press-and-hold controls.
- one-click urgent action button for Hold and custom-operation cancel.
- disabled control reason display.
- inline command rejection/result display.
- persistent critical warning banner/inline component.
- auto-dismiss informational toast.
- numeric field with units and constraints.
- angle/yaw field displayed in degrees.

Keyboard shortcuts may be used for navigation, search, and filtering, but not
for critical/destructive operations such as Arm, Takeoff, Land, runtime
mutations, Mission activation, Custom Operation activation, or operation start.
Audible/browser sound alerts are out of scope.

Acceptance:
- [x] Press-and-hold component uses 1.5 seconds and exposes progress.
- [x] Brief click shows "press and hold" style hint.
- [x] Disabled controls display inline reasons.
- [x] Rejected command attempts can show inline result, event entry, and toast.
- [x] Critical warnings persist until resolved/acknowledged.
- [x] Informational toasts can auto-dismiss.
- [x] Numeric fields show units/constraints.
- [x] Angle/yaw inputs display degrees and pass explicit units to API layer.
- [x] No critical/destructive operation is triggerable by keyboard shortcut.

Implementation notes:
- Added reusable interaction primitives in `src/components/interaction.tsx` for
  press-and-hold, urgent one-click actions, disabled reasons, command result
  notices, event entries, critical warnings, toast regions, numeric fields, and
  degree angle fields.
- Critical controls are pointer-driven and block Enter/Space activation so
  critical or destructive commands are not triggerable by keyboard shortcuts.
- Added shared styles for fixed-size control affordances, progress, warnings,
  toasts, and unit fields.

Changed files:
- `src/III-Drone-GC/frontend/src/components/interaction.tsx`
- `src/III-Drone-GC/frontend/src/components/index.ts`
- `src/III-Drone-GC/frontend/src/components/interaction.test.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`

Verification:
- `npm --prefix src/III-Drone-GC/frontend test` - 2 files, 10 tests passed.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P9.T0: Scaffold React + TypeScript + Vite Frontend

Description:
Create frontend under `src/III-Drone-GC` using React, TypeScript, Vite, and
generated contract types. Add lint/typecheck/test scripts.

Target:
- computer/laptop only.
- minimum usable layout 1440x900.
- high-contrast light theme required.
- dark mode optional later.
- no mobile/tablet requirement.

Acceptance:
- [x] Frontend dev server runs.
- [x] Typecheck passes.
- [x] Generated contract types are imported.
- [x] High-contrast light theme baseline exists.

Implementation notes:
- Added React, TypeScript, Vite, Vitest, and ESLint scaffold under
  `src/III-Drone-GC/frontend`.
- Added a high-contrast light shell with a 1440-oriented operator frame and
  generated-contract type import path.
- Added initial proxy health API client and component test.
- Added `dev`, `build`, `lint`, `test`, `typecheck`, and `contracts:check`
  scripts.

Changed files:
- `src/III-Drone-GC/frontend/index.html`
- `src/III-Drone-GC/frontend/src/App.tsx`
- `src/III-Drone-GC/frontend/src/App.test.tsx`
- `src/III-Drone-GC/frontend/src/api/runtimeProxy.ts`
- `src/III-Drone-GC/frontend/src/main.tsx`
- `src/III-Drone-GC/frontend/src/styles.css`
- `src/III-Drone-GC/frontend/src/test/setup.ts`
- `src/III-Drone-GC/frontend/vite.config.ts`
- `src/III-Drone-GC/frontend/eslint.config.js`
- `src/III-Drone-GC/frontend/package.json`
- `src/III-Drone-GC/frontend/package-lock.json`
- `src/III-Drone-GC/frontend/tsconfig.json`

Verification:
- Vite dev server smoke on port `15174` returned `200`.
- `npm --prefix src/III-Drone-GC/frontend run lint` passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend test` - 1 passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- `npm --prefix src/III-Drone-GC/frontend run contracts:check` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P8.T4: Add GC Docker Compose Stack

Description:
Add `docker-compose` setup in `src/III-Drone-GC` for:
- frontend container.
- thin GC backend/proxy container.

Expose both frontend and proxy ports on the ground-control computer for v2.
Avoid reverse proxy complexity in frontend container. The frontend uses the
configured proxy endpoint.

Acceptance:
- [x] Compose stack builds and starts.
- [x] Frontend can call GC proxy.
- [x] GC proxy can discover or manually select runtime API.
- [x] No ROS packages are installed in GC containers unless needed for build
  tooling unrelated to runtime.

Implementation notes:
- Added `src/III-Drone-GC/docker-compose.yml` with separate ROS-free `proxy`
  and `frontend` services, exposed on configurable host ports.
- Added a Python slim proxy image that installs only `III-Drone-Contracts` and
  `III-Drone-GC` Python dependencies.
- Added a Node build plus nginx static frontend image with
  `VITE_GC_PROXY_URL` configured at image build time.
- Added CORS configuration to the GC proxy so the exposed frontend port can call
  the exposed proxy port without adding reverse-proxy behavior to the frontend
  container.
- Added an initial Vite frontend shell that checks the configured proxy
  `/health` endpoint, plus tests proving the call path.

Changed files:
- `src/III-Drone-GC/docker-compose.yml`
- `src/III-Drone-GC/docker/proxy.Dockerfile`
- `src/III-Drone-GC/frontend/Dockerfile`
- `src/III-Drone-GC/frontend/*`
- `src/III-Drone-GC/iii_drone_gc/v2_proxy/app.py`
- `src/III-Drone-GC/test/test_gc_compose_stack.py`
- `src/III-Drone-GC/test/test_v2_proxy_skeleton.py`

Verification:
- `docker compose -f src/III-Drone-GC/docker-compose.yml config` passed.
- Compose smoke with temporary host ports `18780` and `15173` built both
  images, started both containers, returned `200` from proxy `/health`, served
  the frontend HTML, and cleaned the stack down.
- `python3 -m pytest src/III-Drone-GC/test` - 28 passed.
- `npm --prefix src/III-Drone-GC/frontend run typecheck` passed.
- `npm --prefix src/III-Drone-GC/frontend test` - 1 passed.
- `npm --prefix src/III-Drone-GC/frontend run build` passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  335 tests, 0 errors, 0 failures.

#### P8.T3: Implement HTTP/WebSocket Proxying

Description:
Proxy authenticated HTTP and WebSocket communication between frontend and
selected runtime API.

Auth/session/heartbeat authority remains in `iii-runtime-api`; GC proxy passes
through login/session/heartbeat and closes upstream connections when browser
connection is lost.

Acceptance:
- [x] REST proxy forwards to selected runtime API.
- [x] WebSocket proxy forwards bidirectionally.
- [x] Upstream/downstream disconnects are handled cleanly.
- [x] Proxy does not terminate or own operator lease.

Implementation notes:
- Added selected-runtime-only `/proxy/{path:path}` REST forwarding in
  `iii_drone_gc.v2_proxy.app`; requests are rejected with `409` until a
  compatible runtime target is selected.
- REST forwarding preserves method, body, query string, and authorization
  headers while filtering hop-by-hop headers.
- Added proxied session connection tracking for successful `session/login` and
  `session/logout` calls without terminating or owning the runtime operator
  lease.
- Added `/proxy/ws/{path:path}` WebSocket bridging to the selected runtime API,
  including query/header forwarding and clean disconnect handling.
- Added injectable proxy transports in `iii_drone_gc.v2_proxy.proxy`; production
  defaults use `httpx.AsyncClient` and lazy `websockets`.

Changed files:
- `src/III-Drone-GC/iii_drone_gc/v2_proxy/app.py`
- `src/III-Drone-GC/iii_drone_gc/v2_proxy/proxy.py`
- `src/III-Drone-GC/test/test_v2_proxy_forwarding.py`

Verification:
- `python3 -m pytest src/III-Drone-GC/test` - 25 passed.
- Devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure` plus `colcon test-result --verbose` -
  332 tests, 0 errors, 0 failures.

#### P8.T2: Implement Endpoint Validation And Selected Target State

Description:
GC proxy must not become an open proxy.

Rules:
- Proxy targets limited to discovered runtime APIs or manually entered endpoints
  that pass validation probe.
- Validation confirms compatible `iii-runtime-api`.
- One selected runtime API at a time.
- Non-selected runtime APIs represented only by discovery metadata.
- Changing selected runtime requires logout/disconnect first.
- Selected endpoint is global GC proxy state in v2.

Acceptance:
- [x] Arbitrary URLs are rejected.
- [x] Endpoint validation checks identity/API compatibility.
- [x] Selected endpoint state works.
- [x] Switching target while connected is rejected.

Tests:
- [x] Open-proxy prevention tests.
- [x] Endpoint validation tests.

Implementation notes:
- Added `RuntimeTargetManager` with known-endpoint lookup, `/identity`
  validation probe, compatibility enforcement, selected target state, and
  connected-browser switch protection.
- Added HTTP identity probe client and target API endpoints:
  `/runtime/targets/validate`, `/runtime/target`, `/runtime/target/select`,
  and `DELETE /runtime/target`.
- Proxy identity now includes selected runtime summary when one is selected.
- Target selection accepts only discovered/manual endpoint ids; arbitrary URL
  strings are rejected before any proxying.
- Manual endpoints are selectable only after successful runtime identity
  validation.
- Added tests for validation, incompatible runtime rejection, manual endpoint
  selection, unknown endpoint/open-proxy prevention, and connected-state switch
  rejection.
- Verification passed: `python3 -m pytest src/III-Drone-GC/test`; docker
  devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` (328
  tests, 0 failures).

#### P8.T1: Implement mDNS/Zeroconf Discovery In GC Proxy

Description:
GC proxy discovers runtime API instances on local/operator network using
mDNS/zeroconf service such as `_iii-runtime-api._tcp.local`.

Pre-login discovery exposes only minimal identity/reachability metadata:
runtime name, address, API version, optional profile, and reachable state.

Acceptance:
- [x] Discovery results are exposed to frontend.
- [x] Manual endpoint fallback exists.
- [x] Browser does not perform mDNS directly.
- [x] Discovery does not expose telemetry/logs/health/mode state.

Tests:
- [x] Discovery tests with fake zeroconf records.
- [x] Manual endpoint tests.

Implementation notes:
- Added `iii_drone_gc.v2_proxy.discovery` with runtime endpoint summary
  contracts, static/fake discovery provider, lazy zeroconf provider, and manual
  endpoint registration.
- Added pre-login proxy endpoints `/runtime/discovery` and
  `/runtime/discovery/manual`.
- Discovery summaries expose only runtime name, address/base URL, API version,
  profile, source, and reachability metadata.
- Added tests for minimal discovery payloads, manual endpoint fallback, invalid
  manual URL rejection, and fake zeroconf service-info normalization.
- Added zeroconf package metadata for GC proxy installation.
- Verification passed: `python3 -m pytest src/III-Drone-GC/test`; docker
  devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` (322
  tests, 0 failures).

#### P8.T0: Implement Thin GC Backend/Proxy Skeleton

Description:
Create Python/FastAPI backend/proxy in `src/III-Drone-GC`.

Constraints:
- Depends only on `III-Drone-Contracts`.
- No ROS, MAVSDK, `III-Drone-Interfaces`, `III-Drone-Runtime`,
  `III-Drone-Supervision`, or MCP imports.
- Mostly pass-through for runtime API payloads.
- Schema-aware for discovery, selected endpoint state, route safety, API
  version compatibility, and WebSocket lifecycle.

Acceptance:
- [x] GC proxy starts locally.
- [x] Import tests prove no forbidden dependencies.
- [x] Basic health endpoint exists.

Tests:
- [x] `python3 -m pytest src/III-Drone-GC/test`
- [x] Dependency/import guard test.

Implementation notes:
- Added ROS-free `iii_drone_gc.v2_proxy` FastAPI app skeleton with `/health`
  and contract-shaped `/identity`.
- Added `iii-gc-proxy` console entry point and uvicorn/httpx package metadata
  needed for the proxy process.
- Added tests proving the proxy starts locally and its source does not import
  ROS/runtime/supervision/MAVSDK dependencies.
- Fixed GC test path setup so local tests can import `III-Drone-Contracts`.
- Verification passed: `python3 -m pytest src/III-Drone-GC/test`; docker
  devcontainer `colcon test --base-paths src --packages-select iii_drone_gc
  --ctest-args --output-on-failure && colcon test-result --verbose` (319
  tests, 0 failures).

#### P7.T2: Implement Runtime Map History Buffers

Description:
Add bounded in-memory history buffers in runtime API for:
- short recent drone trail.
- target-point history during operations.
- recent live conductor estimates without creating misleading drifting
  conductors.

No durable replay/database in v2.

Acceptance:
- [x] Histories are bounded and reset/expire predictably.
- [x] Target history is operation-aware.
- [x] Runtime memory usage is bounded.

Tests:
- [x] History buffer unit tests.

Implementation notes:
- Added timestamped bounded history buffers to `RuntimeMapAggregator` for
  drone trail, target history, and recent live conductor estimates.
- Target history is operation-aware through an active operation id provider:
  it resets on operation changes and clears when no operation is active.
- Live conductor history is exposed separately as `recent_live_conductors` and
  marked with `source="live_perception_recent"` so it cannot be confused with
  stored overview or current live geometry.
- History entries expire by TTL and are also hard-capped by buffer limits to
  bound runtime memory use.
- Extended `MapState` contracts and regenerated frontend TypeScript contracts.
- Verification passed: `python3 -m pytest src/III-Drone-Contracts/test`;
  `python3 src/III-Drone-Contracts/scripts/generate_typescript.py --output
  src/III-Drone-GC/frontend/src/generated/contracts.ts`; `npm --prefix
  src/III-Drone-GC/frontend run contracts:check`; `npm --prefix
  src/III-Drone-GC/frontend run typecheck`; `python3 -m pytest
  src/III-Drone-Runtime/test`; docker devcontainer `colcon test --base-paths
  src --packages-select iii_drone_runtime --ctest-args --output-on-failure &&
  colcon test-result --verbose` (307 tests, 0 failures).

#### P7.T1: Implement Powerline-Relative Frame Computation

Description:
Compute map projection reference:
- primary frame is powerline-geometry-relative, not world-origin-centered.
- stored overview defines stable reference frame when available.
- live perception defines temporary frame only if no stored overview exists.
- if neither exists, map state is degraded/no-powerline-reference.
- live perception overlays should not make conductors drift/rotate when stored
  overview is available.

Acceptance:
- [x] Stored overview stabilizes projection frame.
- [x] Live-only fallback works.
- [x] No-reference degraded state works.
- [x] Plot bounds input includes all relevant operational data.

Tests:
- [x] Projection math tests.

Implementation notes:
- Added internal powerline-relative projection frame selection to the runtime
  map aggregator.
- Stored overview conductor geometry is selected as the stable frame when
  available; live perception is projected into that frame and cannot recenter
  or rotate the map while stored overview exists.
- Live perception defines a temporary frame only when no stored overview is
  available.
- No-powerline-reference maps remain degraded even when other operational data
  such as drone pose is available.
- Drone pose, target state, trajectory, conductor layers, and drone trail all
  project through the selected frame before auto-fit bounds are calculated.
- Added projection tests for stored-frame stability, live fallback,
  no-reference degradation, and projected bounds.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test`; docker
  devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (304 tests, 0 failures).

#### P7.T0: Implement Runtime Map State Aggregator

Description:
In `III-Drone-Runtime`, aggregate TF/perception/powerline topics into contract
map state:
- drone pose/location.
- live conductor/perception estimates.
- stored powerline overview.
- target state.
- current trajectory/path.
- target history.
- short drone trail.

Visualization source of truth is runtime ROS state only. Do not use simulation
ground-truth geometry in operator map.

Acceptance:
- [x] Map state is built from ROS TF/perception/powerline topics only.
- [x] Live perception and stored overview are distinguishable.
- [x] Missing/stale sources produce degraded states.
- [x] Runtime API throttles/coalesces geometry updates.

Tests:
- [x] Aggregator unit tests with synthetic ROS-like inputs.

Implementation notes:
- Added `iii_drone_runtime.api.map_state.RuntimeMapAggregator` for ROS-like
  runtime source aggregation into the existing `MapState` contract.
- Aggregates CombinedDroneAwareness/drone pose, live Powerline, stored
  Powerline overview, Target/target pose, and trajectory Path inputs without
  any simulation ground-truth source.
- Live and stored conductor layers remain separate, with source labels and
  stale/missing source status.
- Missing sources return degraded/empty map state; stale sources preserve last
  geometry while marking state stale/degraded.
- State generation is rate-limited and coalesces rapid updates between publish
  intervals.
- Added authenticated Runtime `/map/state` endpoint.
- Added `test_map_state.py` covering empty/degraded state, source aggregation,
  stale degradation, throttling/coalescing, and endpoint response.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test`; docker
  devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (301 tests, 0 failures).

#### P6.T6: Classify And Enforce Read-Only Diagnostic Service Calls

Description:
Define which typed runtime API service calls are read-only diagnostics and may
remain available during Mission mode. Mutating service calls remain gated by
the operational permission model.

Rules:
- Mission mode may allow read-only diagnostics and visualization-supporting
  reads.
- Mission mode must reject custom-operation starts, gripper commands,
  perception/PL mapper mutations, and configuration writes.
- Custom Operation mode allows gripper/perception/config mutations only when no
  custom operation action is active, subject to each subsystem's own safety
  validation.
- Other PX4 modes allow gripper/perception/config mutations subject to typed
  validation.
- Classifications live in runtime API contracts/handler metadata and are not
  inferred from free-form names.

Acceptance:
- [x] Service/action handlers are classified as read-only or mutating.
- [x] Mission-mode read-only calls succeed where explicitly allowed.
- [x] Mission-mode mutating calls are rejected with clear reasons.
- [x] Handler classification is visible in tests and documentation.

Tests:
- [x] Permission matrix tests for Mission, Custom Operation idle/active, and
  other PX4 modes.

Implementation notes:
- Extended `DispatchRegistry` so every action/service registration carries
  `HandlerPermission` metadata plus transport/summary fields.
- Registered PX4/control transitions as `flight_critical`, runtime daemon
  mutations as `runtime_mutation`, subsystem writes as `mutating`, and
  diagnostic/list/download/validation calls as `read_only`.
- Added `/commands/handlers` so GUI/CLI clients can inspect handler metadata
  instead of inferring behavior from command names.
- Remote CLI browser-session conflict handling now uses dispatch metadata.
- Custom-operation validation remains read-only; starts are rejected in Mission
  mode with an explicit Mission-mode reason.
- Added Runtime README documentation for handler permission metadata.
- Added `test_handler_permissions.py` covering metadata visibility and the
  Mission/Custom Operation idle/active/other-mode permission matrix.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test`; docker
  devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (296 tests, 0 failures).

#### P6.T5: Implement Configuration Server Runtime API Handlers

Description:
Add structured configuration workflows backed by the configuration server:
- parameter manifest.
- current active values.
- loaded snapshot.
- default snapshot.
- per-parameter apply.
- batch apply with per-parameter results.
- reset/revert is frontend-only for pending edits but API returns canonical
  current state.
- save/load snapshots.
- download snapshot.
- set default snapshot.
- restart-required metadata for constant/static parameters.

Runtime API must not read local parameter files directly as GUI source of
truth. Configuration server is source of truth.

Acceptance:
- [x] Runtime API exposes structured manifest from configuration server.
- [x] Runtime API supports apply/save/load/snapshot/default operations.
- [x] Constant/static restart-required metadata is exposed.
- [x] Unsaved/non-default status follows agreed semantics.
- [x] Writes/load/save are disabled in Mission mode and during active custom
  operation action.

Tests:
- [x] Runtime API config tests with fake configuration server.

Implementation notes:
- Added `iii_drone_runtime.api.configuration` with a configuration server
  adapter boundary, ROS service adapter, manifest normalization, snapshot
  workflow controller, permission gate, and typed command handlers.
- Added Runtime endpoints for `/configuration/manifest`,
  `/configuration/status`, `/configuration/apply`, snapshot list/save/load,
  snapshot download, and default selection.
- Manifest normalization converts configuration-server YAML payloads into the
  shared `ConfigurationManifest` contract without reading local parameter
  files in the runtime API.
- Apply returns per-parameter results, including restart-required metadata;
  status marks runtime snapshot files as `Unsaved` and non-default loaded
  snapshots as `Non-default`.
- Configuration apply/save/load/set-default mutations are rejected in Mission
  mode and while a custom operation action is active; list/download remain
  available as read operations.
- Added `test_configuration_api.py` with fake configuration server coverage
  for manifest/status, normalization, apply, snapshot workflows, commands, and
  permission rejection.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test`; docker
  devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (293 tests, 0 failures).

#### P6.T4: Implement Rosbag Recorder Runtime API Handlers

Description:
Integrate the existing rosbag recorder node through runtime API:
- current recorder state.
- recording owner/source where known: GUI/manual, mission/runtime.
- manual start/stop recording.
- list recordings.
- download/export recordings through runtime API, proxied by GC backend.

Rules:
- Recorder controls remain available across operational modes.
- State is continuously reconciled because mission may start/stop underneath.
- Starting/stopping during mission requires press-and-hold UI.
- Stopping mission/runtime-owned recording is allowed but requires warning and
  press-and-hold UI.

Acceptance:
- [x] Rosbag domain shows current recorder state and owner/source.
- [x] Manual start/stop commands work through existing recorder node.
- [x] State changes caused by mission/runtime are reflected.
- [x] List/download/export endpoints exist where recorder storage supports it.

Tests:
- [x] Runtime API rosbag tests with fake recorder service/state.

Implementation notes:
- Added `RosbagDomainState` to shared contracts and regenerated frontend
  TypeScript contracts.
- Added Runtime `/rosbag/status`, `/rosbags`, and
  `/rosbags/{recording_id}/download` endpoints.
- Added typed command handlers for `rosbag.start`, `rosbag.stop`,
  `rosbag.list`, and `rosbag.download` with a recorder adapter abstraction.
- Start/stop reconcile state by querying the recorder adapter; mission-mode
  controls and mission/runtime-owned stops require `hold_confirmed=true`.
- Filesystem listing/download support is available for recorder storage roots
  even when ROS recorder services are unavailable.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_rosbag.py
  src/III-Drone-Contracts/test/test_domain_states.py
  src/III-Drone-Contracts/test/test_commands.py`; `python3
  src/III-Drone-Contracts/scripts/generate_typescript.py --output
  src/III-Drone-GC/frontend/src/generated/contracts.ts`; `npm --prefix
  src/III-Drone-GC/frontend run contracts:check`; `npm --prefix
  src/III-Drone-GC/frontend run typecheck`.

#### P6.T3: Implement Perception/PL Mapper Runtime API Handlers

Description:
Port current PL mapper and powerline overview controls:
- PL mapper state/status.
- PL direction computer status.
- Hough transformer status.
- start/stop/freeze/pause PL mapper commands where supported.
- update powerline overview.
- stored powerline overview status.

Permissions:
- Disabled in Mission mode.
- Enabled in Custom Operation mode only when no operation action active.
- Enabled in other PX4 modes.

Acceptance:
- [x] Perception/powerline domains include current GUI status parity.
- [x] Commands use typed handlers and ROS services.
- [x] Runtime API reconciles live perception and stored overview state.
- [x] Permission model enforced.

Tests:
- [x] Runtime API perception handler tests.

Implementation notes:
- Added `PerceptionStatusCache` for PL mapper state, PL direction computer
  status, Hough transformer status, stored overview status, and live powerline
  line count.
- Added `/perception/status` and `/powerline/status`.
- Added typed command handlers for PL mapper start/stop/freeze/pause and
  powerline overview update with fakeable service adapters for
  `/perception/pl_mapper/pl_mapper_command` and
  `/mission/powerline_overview_provider/update_powerline_overview`.
- Enforced Mission/custom-operation-active mutation permissions with explicit
  rejection reasons.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_perception.py
  src/III-Drone-Runtime/test/test_payload.py
  src/III-Drone-Contracts/test/test_commands.py`.

#### P6.T2: Implement Payload/Gripper Runtime API Handlers

Description:
Port current `IIIGCNode` gripper functionality to runtime API typed handlers:
- gripper status.
- open gripper.
- close gripper.
- charger operating mode/status.
- battery voltage.
- charging power.

Permissions:
- Disabled in Mission mode.
- Enabled in Custom Operation mode only when no operation action active.
- Enabled in other PX4 modes.

Acceptance:
- [x] Payload domain includes status fields from current GUI.
- [x] Gripper commands call typed ROS service path.
- [x] Permission model enforced by runtime API.
- [x] Disabled/rejection reasons are explicit.

Tests:
- [x] Runtime API tests with fake ROS service/status.

Implementation notes:
- Added `PayloadStatusCache` for gripper status, charger operating mode/status,
  battery voltage, and charging power.
- Added `/payload/status` and Runtime API command handlers for
  `payload.gripper.open` and `payload.gripper.close`.
- Added `RosGripperServiceAdapter` for typed
  `/payload/charger_gripper/gripper_command` service calls plus fakeable
  service adapter tests.
- Added `PayloadPermissionGate` enforcing disabled-in-Mission and
  disabled-while-custom-operation-active behavior with explicit rejection
  reasons.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_payload.py
  src/III-Drone-Runtime/test/test_operation_commands.py
  src/III-Drone-Runtime/test/test_operation_status.py
  src/III-Drone-Contracts/test/test_commands.py`.

#### P6.T1: Implement Runtime API Custom Operation Commands

Description:
Add typed runtime API command handlers for operation validate/start/cancel.

Rules:
- Operation forms are typed; raw JSON calls are not normal UI.
- Starts require CustomOperation mode and press-and-hold UI.
- Cancel is one-click and no confirmation.
- Global status bar cancel cancels custom operation only, not mission.
- Gripper/perception commands are blocked while custom operation action active.

Acceptance:
- [x] All operation helpers have command IDs and request schemas.
- [x] Runtime API handles validate/start/cancel.
- [x] Active operation state appears in `operation` domain.
- [x] WebSocket emits feedback/status/result.

Tests:
- [x] Runtime API operation command tests.

Implementation notes:
- Added contract request schemas for all CustomOperation form shapes and
  regenerated frontend TypeScript contracts.
- Added runtime command handlers for `custom_operation.validate`,
  all typed operation start command IDs, and `custom_operation.cancel`.
- Start commands require `hold_confirmed=true`, validate typed form arguments,
  and use the nonblocking operation facade; cancel only cancels the active
  CustomOperation facade goal.
- `/operations/status` now includes runtime operation record/event state when
  an operation has been started through the Runtime API.
- Operation facade events are forwarded through the Runtime WebSocket as
  `command_result` messages for started/feedback/result.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_operation_commands.py
  src/III-Drone-Runtime/test/test_custom_operations.py
  src/III-Drone-Runtime/test/test_operation_status.py
  src/III-Drone-Contracts/test/test_operation_contracts.py
  src/III-Drone-Contracts/test/test_commands.py`; `python3
  src/III-Drone-Contracts/scripts/generate_typescript.py --output
  src/III-Drone-GC/frontend/src/generated/contracts.ts`; `npm --prefix
  src/III-Drone-GC/frontend run contracts:check`; `npm --prefix
  src/III-Drone-GC/frontend run typecheck`.

#### P6.T0: Refactor Nonblocking Custom Operation Client

Description:
Refactor or add a shared operator-control module around
`src/III-Drone-Mission/iii_drone_mission/operations_client.py`.

Existing helpers to support:
- `fly_to_position`.
- `cable_aware_fly_to_position`.
- `fly_to_object`.
- `cable_landing`.
- `cable_takeoff`.
- `hover`.
- `hover_by_object`.
- `hover_on_cable`.
- `cancel_active`.

Provide nonblocking start/status/result/cancel semantics and validate-only
readiness checks. Use MCP nonblocking registry as reference only; do not import
MCP.

Acceptance:
- [x] Runtime API can start each operation without blocking request thread.
- [x] Feedback/status/result stream over WebSocket.
- [x] One active operation at a time.
- [x] Operation rejection reasons are surfaced.
- [x] Validate-only checks mode, active operation, required context, parameter
  ranges, frame availability, and target availability.

Tests:
- [x] Unit tests with fake action client.
- [x] Existing mission operation tests remain passing.

Implementation notes:
- Added `NonblockingCustomOperationClient` and `RosCustomOperationTransport`
  in Runtime with fakeable transport, nonblocking start/status/result/cancel,
  bounded operation events, and one-active-operation enforcement.
- Added operation validation for all preserved helpers:
  `fly_to_position`, `cable_aware_fly_to_position`, `fly_to_object`,
  `cable_landing`, `cable_takeoff`, `hover`, `hover_by_object`, and
  `hover_on_cable`.
- Validate-only checks cover CustomOperation mode registration/activation,
  active operation conflict, frame availability, target/cable availability,
  and numeric/range validation.
- The facade is MCP-free and does not modify the existing blocking
  mission-side `OperationsClient`.
- Verification passed: host `python3 -m pytest
  src/III-Drone-Runtime/test/test_custom_operations.py
  src/III-Drone-Runtime/test/test_dependency_boundaries.py`; devcontainer
  `colcon test --base-paths src --packages-select iii_drone_runtime
  --ctest-args --output-on-failure && colcon test-result --verbose`
  (272 tests, 0 failures).

#### P5.T3: Implement Global Hold Interruption Reconciliation

Description:
Global Hold is one-click and allowed in Mission mode or during an active custom
operation. Runtime API sends only PX4 Hold, logs control-owner interruption,
and expects the runtime system to abort active actions when owning mode changes.

If Hold succeeds but active custom operation/mission state does not clear or
reconcile within 3 seconds, runtime API raises a persistent warning.

Acceptance:
- [x] Hold command is accepted across modes when transport available.
- [x] Hold is disabled with reason when command transport unavailable/stale.
- [x] Runtime API does not also send custom-operation cancel.
- [x] 3-second reconciliation warning is emitted if active state persists.

Tests:
- [x] Hold interruption tests for mission/custom/other modes.

Implementation notes:
- Added `HoldInterruptionReconciler` to record when PX4 Hold interrupts active
  mission/custom-operation ownership and to emit a persistent warning if typed
  mission/custom state remains active after 3 seconds.
- PX4 Hold still sends only the MAVSDK Hold command; it does not call custom
  operation cancel or mission cancel paths.
- `/control/status` includes hold interruption warnings alongside command
  permissions and transition state.
- Verification passed: host `python3 -m pytest src/III-Drone-Runtime/test`
  (94 passed); devcontainer `colcon test --base-paths src --packages-select
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (267 tests, 0 failures).

#### P5.T2: Implement Flight Command Gating And Transitions

Description:
Implement runtime API command handlers and gating for:
- Arm: press-and-hold UI, standalone, no implicit takeoff.
- Takeoff: requires armed; does not arm implicitly.
- Land: press-and-hold UI.
- Hold: one-click, globally available when command transport is available.
- Mission activation: requires in flight, system running, active mission spec,
  all required modes registered.
- Custom Operation activation: requires in flight, system running, mode
  registered.

Runtime API must set `transitioning` during mode changes and use 5-second
transition timeout.
Runtime API must not automatically cancel mission/custom-operation actions as
part of Mission/Custom/Hold transitions; it requests the target mode and then
reconciles owner state from typed status.

Acceptance:
- [x] Each command has typed success/rejection.
- [x] Disabled reasons are derivable from runtime API state.
- [x] Mode/control-owner transition state includes target and start time.
- [x] Transition timeout marks degraded/rejected.
- [x] No activation path implicitly cancels another owner without explicit
  command semantics.

Tests:
- [x] Command gating matrix tests.
- [x] Transition timeout tests.

Implementation notes:
- Added `FlightCommandGate` to compute disabled reasons from fused PX4 vehicle
  state, supervision system state, mission status, and custom-operation status.
- Added `ControlTransitionTracker` with 5-second timeout semantics and exposed
  `/control/status` with command permission reasons and transition details.
- PX4 takeoff/land/hold commands now set transition targets after successful
  command dispatch; takeoff requires already armed and does not arm implicitly.
- Added typed `mission.activate` and `custom_operation.activate` handlers with
  a pluggable control-mode request adapter. The default adapter rejects
  explicitly when no concrete ROS mode request path is configured, while tests
  verify successful mode requests with a fake adapter.
- Activation handlers request only the target mode and do not issue implicit
  mission/custom-operation cancel calls.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_flight_commands.py
  src/III-Drone-Runtime/test/test_px4_state.py
  src/III-Drone-Runtime/test/test_px4_adapter.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py`.

#### P5.T1: Fuse MAVSDK And ROS/uXRCE PX4 State

Description:
Implement fused PX4 state in runtime API:
- MAVSDK is primary source for command transport state.
- ROS/uXRCE is primary source for ROS bridge/runtime PX4 state.
- Failsafe/nav state prefers richer ROS PX4 vehicle status where available.
- If safety-critical fields disagree, mark degraded and block dangerous
  commands.

Fields:
- command transport status.
- ROS bridge status.
- armed.
- in-air.
- exact PX4 mode/nav state.
- failsafe.
- disagreement fields.
- source timestamps.

Acceptance:
- [x] Dashboard can show fused status.
- [x] Diagnostics can show MAVSDK and ROS/uXRCE sources separately.
- [x] Disagreement on armed/in-air/mode/failsafe marks degraded.
- [x] Dangerous command gating fails closed on degraded/unknown state.

Tests:
- [x] Fusion unit tests covering agreement, disagreement, stale, missing sources.

Implementation notes:
- Added `RosPx4StateCache` and `FusedPx4StateProvider` in Runtime to combine
  MAVSDK command transport status with ROS/uXRCE vehicle status and land
  detection data.
- Added `/vehicle/status`; `/px4/status` now returns the same fused
  `VehicleDomainState` with separate `command_transport` and `ros_uxrce`
  diagnostic payloads.
- Fused state prefers ROS/uXRCE armed/in-air/nav/failsafe fields when present,
  records source timestamps, marks source disagreements degraded, and exposes
  `dangerous_commands_allowed`.
- PX4 arm/takeoff/land command handlers now fail closed when fused safety state
  is degraded, stale, missing, or in failsafe; hold remains command-transport
  gated for the later global-hold task.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_px4_state.py
  src/III-Drone-Runtime/test/test_px4_adapter.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py`.

#### P5.T0: Implement Persistent MAVSDK PX4 Command Adapter

Description:
Adapt MCP reference `tools/III-Drone-MCP/iii_drone_mcp/px4_command_client.py`
into `III-Drone-Runtime` without depending on MCP.

Expected real/sim config: PX4 exposes MAVLink and uXRCE-DDS over the runtime
host link. Runtime API maintains a persistent MAVSDK/MAVLink connection,
auto-reconnects after link loss, and exposes command transport status.

Commands in v2:
- arm.
- takeoff.
- land.
- hold.

No disarm or RTL in v2 GUI scope.

Acceptance:
- [x] Adapter connects persistently to configured MAVLink endpoint.
- [x] Adapter reconnects after link loss.
- [x] Adapter exposes connected/degraded state, last heartbeat/update,
  armed, flight mode/nav state where available, and in-air state.
- [x] Arm/takeoff/land/hold commands work through adapter when connected.
- [x] If MAVLink is unavailable, the adapter either uses an explicitly tested
  typed ROS/PX4 command fallback or reports unsupported/degraded state with a
  clear frontend-visible reason.
- [x] No MCP import exists.

Tests:
- [x] Unit tests with fake MAVSDK abstraction.
- Optional sim integration test when PX4 SITL is available: not required for
  this completion pass; fake MAVSDK tests cover adapter behavior.

Implementation notes:
- Added `PersistentPx4CommandAdapter` in `III-Drone-Runtime` with lazy MAVSDK
  import, injected MAVSDK-like factory for tests, background connection
  monitoring, auto-reconnect after link loss, command transport status, and
  typed telemetry snapshots.
- Added runtime API `/px4/status` and registered typed dispatch handlers for
  `px4.arm`, `px4.takeoff`, `px4.land`, and `px4.hold`.
- MAVLink/MAVSDK unavailable or disabled states now reject commands with
  frontend-visible degraded reasons instead of importing MCP or crashing.
- Added a runtime dependency-boundary test proving no `iii_drone_mcp` import.
- Verification passed: `python3 -m pytest
  src/III-Drone-Runtime/test/test_px4_adapter.py
  src/III-Drone-Runtime/test/test_dependency_boundaries.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py
  src/III-Drone-Contracts/test/test_envelopes.py
  src/III-Drone-Contracts/test/test_commands.py`.

#### P4.T4: Add Subsystem Health Publishers

Description:
Add typed health/status topics for major subsystems where missing:
perception, control, mission, payload, configuration, and supervision.

Keep domain-specific detail in subsystem publishers; supervision may publish
aggregate readiness but should not interpret every detail.

Acceptance:
- [x] Each major subsystem has typed health/status available.
- [x] Runtime API can aggregate subsystem health into domain state.
- [x] Degraded/stale/missing status is represented.

Tests:
- [x] Targeted package tests for each touched III package.

Implementation notes:
- Runtime now aggregates required subsystem health from typed
  `SubsystemHealthStatus` entries carried by supervision health.
- Added authenticated `/subsystems/health` API route for perception, control,
  mission, payload, configuration, and supervision readiness.
- Missing subsystem status is represented as unavailable/degraded with explicit
  reasons and is also reflected into the runtime domain snapshot.
- Verification passed: host `python3 -m pytest
  src/III-Drone-Runtime/test/test_supervision_health.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py`; devcontainer
  `colcon test --base-paths src --packages-select iii_drone_runtime
  --ctest-args --output-on-failure && colcon test-result --verbose` (249
  tests, 0 failures); submodule lock verification passed.

#### P4.T3: Publish Custom Operation Mode Status

Description:
Add typed custom operation status reporting for:
- CustomOperation PX4 mode registration/availability.
- CustomOperation mode active/inactive.
- active custom operation action state.
- one-active-operation policy.
- rejection/degraded reasons.

Acceptance:
- [x] Runtime API can distinguish `custom_operation_idle` and
  `custom_operation_active`.
- [x] Custom operation activation preconditions use typed status.
- [x] Operation start gating uses typed active-operation state.

Tests:
- [x] Mission/custom-operation tests.
- [x] Runtime API aggregation tests.

Implementation notes:
- Added typed `/mission/custom_operation/mode_status`
  `CustomOperationModeStatus` publishing alongside the existing string status.
- Custom operation status now surfaces PX4/offboard registration, active mode,
  active operation, cancel availability, one-active-operation rejection, and
  degraded reasons.
- Added runtime `CustomOperationStatusCache` and authenticated
  `/operations/status` API route that maps typed state to
  `custom_operation_idle` / `custom_operation_active` and exposes
  `start_allowed` / `start_rejections`.
- Verification passed: host `python3 -m pytest
  src/III-Drone-Runtime/test/test_operation_status.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py`; devcontainer
  `colcon build --base-paths src --packages-select iii_drone_mission
  iii_drone_runtime --symlink-install`; devcontainer `colcon test --base-paths
  src --packages-select iii_drone_mission iii_drone_runtime --ctest-args
  --output-on-failure && colcon test-result --verbose` (247 tests, 0
  failures); submodule lock verification passed.

#### P4.T2: Publish Mission Mode Registration And Active Spec Status

Description:
Add typed mission status reporting for:
- active mission specification identity/file.
- modes required by active spec.
- owned mission mode identity.
- per-mode registration with PX4.
- mission active/running/completed/degraded state.

Mission activation in GUI uses the owned mode from the active mission spec and
is allowed only when all required modes are registered and system is running.

Acceptance:
- [x] Runtime API can determine active mission spec.
- [x] Runtime API can determine required modes and registration state.
- [x] Mission activation preconditions use typed status.
- [x] UI can show exact rejection reasons.

Tests:
- [x] Mission package tests for status publisher.
- [x] Runtime API aggregation tests with fake mission status.

Implementation notes:
- Mission now publishes typed `/mission/status` `MissionModeStatus` with active
  mission specification path, required modes, registered modes, owned mode,
  active/degraded state, and explicit activation failure reasons.
- Added mission specification and PX4 mode-provider accessors needed to expose
  mode registration state without parsing logs.
- Added runtime `MissionStatusCache` and authenticated `/mission/status` API
  route that converts the typed topic into the mission domain, including
  `activation_allowed` and `activation_rejections`.
- Verification passed: host `python3 -m pytest
  src/III-Drone-Runtime/test/test_mission_status.py
  src/III-Drone-Runtime/test/test_runtime_api_skeleton.py
  src/III-Drone-Runtime/test/test_supervision_health.py`; devcontainer
  `colcon build --base-paths src --packages-select iii_drone_interfaces
  iii_drone_mission iii_drone_runtime --symlink-install`; devcontainer
  `colcon test --base-paths src --packages-select iii_drone_mission
  iii_drone_runtime --ctest-args --output-on-failure && colcon test-result
  --verbose` (244 tests, 0 failures); submodule lock verification passed.

#### P4.T1: Publish Supervision Aggregate Health

Description:
Add or extend supervision/runtime health publisher so the system exposes:
- III system running/ready state.
- daemon/service dependency readiness.
- system active/started state.
- entity/service status summary.
- degraded reasons.

This should live in owned runtime/supervision packages, not GC.

Acceptance:
- [x] Runtime API can subscribe to aggregate supervision health.
- [x] Health topic is typed.
- [x] Missing daemon/ROS state is represented explicitly.
- [x] No log scraping is required for GUI-critical state.

Tests:
- [x] Supervision unit tests.
- [x] Targeted colcon test for supervision/runtime package.

Implementation notes:
- `SystemManager` now exposes and publishes typed
  `/supervision/system_health` `SystemHealthStatus` messages built from
  daemon-owned lifecycle, service, process, and degraded-reason state.
- Added runtime-side `SupervisionHealthCache` with typed ROS subscription
  wiring and conversion to the API `system` domain state.
- Added authenticated `/system/health` API route and explicit unavailable
  state when the typed health topic has not been received.
- Verification passed: host `python3 -m pytest src/III-Drone-Runtime/test
  src/III-Drone-Supervision/test/test_system_manager.py
  src/III-Drone-Supervision/test/test_system_daemon.py` (82 passed);
  devcontainer `colcon test --base-paths src --packages-select
  iii_drone_supervision iii_drone_runtime --ctest-args --output-on-failure &&
  colcon test-result --verbose` (241 tests, 0 failures); submodule lock
  verification passed.

#### P4.T0: Add Typed Health Interfaces

Description:
Add typed ROS interface messages in `III-Drone-Interfaces`:
- `SystemHealthStatus`.
- `MissionModeStatus`.
- `CustomOperationModeStatus`.
- `SubsystemHealthStatus`.

Include fields needed for readiness, degraded state, status reasons,
timestamps, active mission specification, required mode registration, owned
mode, custom operation mode registration, and control owner where appropriate.

Acceptance:
- [x] Interface messages compile.
- [x] Fields cover GUI v2 health/status requirements.
- [x] Existing message generation/tests pass.

Tests:
- [x] `colcon build --base-paths src --packages-select iii_drone_interfaces --symlink-install`

Implementation notes:
- Added `SystemHealthStatus`, `MissionModeStatus`,
  `CustomOperationModeStatus`, and `SubsystemHealthStatus` ROS messages to
  `III-Drone-Interfaces`.
- Added `builtin_interfaces` to interface generation dependencies and manifest
  tests covering GUI v2 health fields.
- Verification passed: `python3 -m pytest
  src/III-Drone-Interfaces/test/test_interface_manifest.py`;
  devcontainer `colcon build --base-paths src --packages-select
  iii_drone_interfaces --symlink-install`; devcontainer `colcon test
  --base-paths src --packages-select iii_drone_interfaces --ctest-args
  --output-on-failure && colcon test-result --verbose` (172 tests, 0
  failures); submodule lock verification passed.

#### P3.T5: Implement Simulation-Profile Runtime Controls

Description:
Expose simulation-only runtime controls through `iii-runtime-api` where
supported by existing runtime tooling. Initial scope:
- PX4/Gazebo backend status.
- PX4/Gazebo backend start/stop where supported.
- simulation tool status surfaced in `simulation` domain.

Rules:
- Available only in simulation profile.
- Hidden or disabled with explicit profile reason in real profile.
- Do not launch/control QGroundControl.
- Controls should call owned simulation/runtime tooling, not MCP.

Relevant existing files:
- `tools/simulation/launch_simulation_tools.sh`
- `tools/simulation/managed_px4_gazebo_config.yaml`
- `docs/adr/0005-simulation-control-in-simulation-package.md`

Acceptance:
- [x] Runtime API exposes simulation profile/status.
- [x] PX4/Gazebo status is visible in simulation profile.
- [x] Start/stop commands are available only in simulation profile where
  implemented.
- [x] Real profile returns disabled/unsupported reasons.
- [x] QGroundControl is not launched or controlled.

Tests:
- [x] Runtime API simulation-control tests with fake profile/tool adapter.

Implementation notes:
- Added runtime-owned `SimulationRuntimeController` and
  `SubprocessSimulationToolAdapter` around existing
  `tools/simulation/launch_simulation_tools.sh`.
- Exposed authenticated `/simulation/status`, `/simulation/backend/start`, and
  `/simulation/backend/stop` runtime API routes; status updates the typed
  `simulation` domain payload.
- Backend start uses `--headless --no-attach`, preserving the no-QGroundControl
  v2 scope while still surfacing observed QGC status from the existing tool.
- Real/non-simulation profiles return disabled results with explicit reasons
  and do not call simulation tooling.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test` (64
  passed); submodule lock verification passed.

#### P3.T4: Migrate Remote CLI Runtime-Control Commands To Runtime API

Description:
Update `tools/III-Drone-CLI/iii/system.py` and related client code so remote
runtime-control commands use `iii-runtime-api` instead of SSH/container command
forwarding.

Keep local CLI daemon-socket path initially. Remove SSH command forwarding for:
boot/start/stop/restart/shutdown/status/list/service/log runtime-control paths
when `CLI_CONFIGURATION=remote`.

Acceptance:
- [x] Remote runtime-control CLI uses runtime API client/token.
- [x] Remote runtime-control CLI no longer shells into host for runtime
  commands.
- [x] If runtime API is unavailable, CLI reports a clear error.
- [x] Read-only remote CLI commands work during active GUI session.
- [x] Mutating remote CLI commands are blocked during active GUI session.
- [x] SSH remains for deploy/file transfer/sync and explicit shell workflows.

Tests:
- [x] `tools/III-Drone-CLI/test` with fake runtime API.
- [x] Existing local CLI tests remain passing.

Implementation notes:
- Added `iii.runtime_api_client.RuntimeApiClient` using CLI-token HTTP calls to
  `iii-runtime-api`.
- Remote `iii system boot/start/stop/restart/shutdown/status/list-nodes/
  list-services/service/logs` now calls runtime API endpoints instead of
  host/container command forwarding.
- Updated `/cli/commands` to execute the daemon-backed runtime dispatcher after
  enforcing CLI token auth and active-browser conflict policy; added CLI-token
  log tail endpoints.
- Remote tmux attach/session-control paths no longer silently fall back through
  runtime-control forwarding and point operators to explicit SSH workflows.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test
  tools/III-Drone-CLI/test` (82 passed); submodule lock verification passed.

#### P3.T3: Implement Live Log Follow Over WebSocket

Description:
Add live/follow log streaming via WebSocket:
- source selection.
- follow toggle.
- all-logs stream.
- cancellation/cleanup when client disconnects.
- throttling/backpressure protection.

Acceptance:
- [x] Follow stream emits log lines with source metadata.
- [x] All-logs stream includes source labels.
- [x] Disconnect cleans up file/journal tail tasks.
- [x] Large log streams do not block runtime API event bus.

Tests:
- [x] WebSocket log follow tests with temp files/fake sources.

Implementation notes:
- Added authenticated `/logs/follow/{source_id}` WebSocket streaming backed by
  `LogSourceProvider.follow`.
- Follow streams now include source id, label, kind, and line data, support the
  all-logs aggregate, stream appended file lines, and bound each poll batch.
- Verification passed: `python3 -m pytest src/III-Drone-Runtime/test` (59
  passed).

#### P3.T2: Implement Runtime API Log Source Model

Description:
Expose log sources through runtime API:
- daemon logs.
- runtime API service logs.
- daemon-managed service logs.
- managed entity/ROS node logs.
- all-logs view.

Use existing daemon log-dir support where available and system journal logs for
services. Define source IDs in contracts.

Acceptance:
- [x] Runtime API lists available log sources.
- [x] Runtime API can fetch/tail historical chunks over REST.
- [x] Runtime API can download/export current view/source where practical.
- [x] Runtime API exposes all-logs source.

Tests:
- [x] Fake log source tests.
- [x] REST tail/download tests.

Implementation notes:
- Added `LogSourceProvider` with file/journal/aggregate source metadata,
  historical tail chunks, all-logs aggregation, and download serialization.
- Exposed authenticated `/logs/sources`, `/logs/{source_id}/tail`, and
  `/logs/{source_id}/download` runtime API routes.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P3.T1: Implement Armed/In-Flight Fail-Closed Runtime Control Gating

Description:
Runtime mutations must be blocked while vehicle is armed, in flight, or vehicle
state is unknown/stale.

Rules:
- Broad boot/start blocked while armed/in-flight.
- Stop/shutdown/restart blocked while armed/in-flight.
- Service mutations blocked by default while armed/in-flight.
- Unknown/stale vehicle state blocks dangerous runtime mutations.
- Read-only status/log operations remain available.
- No emergency force-stop override in v2.

Acceptance:
- [x] Gating works from fused PX4 vehicle state.
- [x] Unknown/stale state fails closed.
- [x] Rejection reason is explicit and contract-serialized.
- [x] Event log records rejected mutating attempts.

Tests:
- [x] Gating matrix tests for disarmed, armed, in-flight, stale, unknown.

Implementation notes:
- Added `VehicleSafetyState` and `RuntimeMutationGate`; runtime mutating
  commands now fail closed on unknown, stale, degraded, armed, or in-flight
  state while read-only runtime commands remain available.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P3.T0: Implement Runtime Control Commands In Runtime API

Description:
Expose typed runtime-control operations through runtime API:
- boot.
- start.
- stop.
- restart.
- shutdown.
- list entities/nodes.
- list daemon-managed services.
- service start/stop/restart.
- status.

Use daemon Unix socket for actual runtime operations. Mutating commands require
browser session or non-conflicting remote CLI token auth. Press-and-hold is UI
only, but API must enforce command type and safety policy.

Acceptance:
- [x] REST command endpoint can execute runtime commands.
- [x] Mutating runtime commands are classified.
- [x] Read-only runtime commands are classified.
- [x] Command results are serialized through contracts.
- [x] Runtime API emits event log entries for accepted/rejected mutating
  commands.

Tests:
- [x] Runtime command tests using fake daemon client.

Implementation notes:
- Added daemon-backed runtime control handlers for boot/start/stop/restart/
  shutdown/status/list/service mutation command IDs.
- Handlers classify read-only versus mutating commands, serialize daemon
  results through action responses, and emit event-log request/decision entries.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P2.T8: Implement Generic Typed REST Dispatch Semantics

Description:
Implement the runtime API dispatch layer for the agreed generic boundary:
- one generic action-start endpoint with command/action type and typed
  parameters.
- immediate HTTP response for validation/acceptance/rejection.
- action feedback/status/final results delivered over WebSocket command-result
  and event messages.
- one generic service-call endpoint with service type/name and typed
  parameters.
- service-call response returned directly in the HTTP response.

The endpoints are generic only at the frontend/runtime API boundary. Runtime
execution must route through a hardcoded typed registry of allowed handlers. The
frontend must not be able to call arbitrary ROS actions, services, or topics by
string.

Acceptance:
- [x] Action-start endpoint returns immediate accepted/rejected response.
- [x] Accepted action lifecycle updates stream over WebSocket.
- [x] Service-call endpoint returns serialized response in HTTP response.
- [x] Unknown/unregistered action or service types are rejected.
- [x] Handler registry is explicit and typed, not an arbitrary ROS bridge.

Tests:
- [x] Runtime API dispatch tests with fake typed action/service handlers.
- [x] Rejection tests for unknown or disallowed handler names.

Implementation notes:
- Added explicit `DispatchRegistry` for action/service handlers and wired
  generic runtime API command endpoints through it.
- Accepted action starts emit immediate command-result updates; unknown
  actions/services return contract-typed handler-unavailable rejections.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P2.T7: Implement Runtime ROS Executor And Client Lifecycle

Description:
Implement the ROS integration layer inside `III-Drone-Runtime` for
`iii-runtime-api`.

Architecture:
- Use normal `rclpy` clients/subscriptions/actions in the runtime-host process.
- Run the `rclpy` executor in a dedicated background thread when ROS is
  available.
- Bridge ROS callbacks into the async FastAPI/WebSocket state bus through
  thread-safe queues or explicit loop handoff.
- Avoid depending on experimental ROS asyncio executor APIs.
- Represent ROS unavailable, executor down, or graph disconnected as explicit
  degraded state instead of crashing the API.

Acceptance:
- [x] Runtime API starts and serves non-ROS routes when ROS is unavailable.
- [x] ROS executor starts/stops cleanly when ROS is available.
- [x] Callback-to-WebSocket handoff is thread-safe.
- [x] Service/action availability changes produce state updates/events.
- [x] Shutdown cleans up executor thread, clients, subscriptions, and actions.

Tests:
- [x] Runtime ROS lifecycle unit tests with fakes/mocks where possible.
- Integration smoke test in devcontainer when ROS is available: not run from
  this host shell during this task; covered by fakeable lifecycle tests and the
  full devcontainer suite.

Implementation notes:
- Added optional `RuntimeRosExecutor` with dedicated rclpy thread lifecycle,
  fakeable rclpy integration, thread-safe callback handoff queue, availability
  events, and explicit degraded state when ROS is unavailable.
- Verification passed: Runtime pytest suite and submodule lock verification.
  Devcontainer ROS smoke was not run from this host shell.

#### P2.T6: Implement Runtime Event Log Model

Description:
Implement the runtime-side in-memory event log and event emission rules.

Runtime events include:
- command requests.
- accepted/rejected actions.
- runtime API validation failures.
- ROS service/action availability changes.
- action/service results.
- remote CLI read-only requests with client metadata when available.
- remote CLI mutating requests rejected because GUI session is active.
- global Hold interruption events.
- reconnect/degraded/health changes relevant to operator safety.

Storage is in-memory for GUI event stream in v2. Accepted/rejected mutating
runtime API commands should also be written to normal service logs for
operational debugging. Events must be source-labelled so the frontend can merge
runtime events with local GC proxy/frontend events.

Acceptance:
- [x] Runtime API emits contract-typed runtime events.
- [x] Event history is bounded in memory.
- [x] Accepted/rejected mutating commands are written to service logs.
- [x] Remote CLI request events include client metadata when available.
- [x] Event source labels distinguish runtime events from local GC events.

Tests:
- [x] Runtime event log unit tests.
- [x] Mutating command logging tests.

Implementation notes:
- Expanded `RuntimeEventLog` with bounded event history, command request/
  decision/result, availability, validation failure, and CLI conflict events.
- Mutating command decisions are written through the runtime event logger for
  normal service logs.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P2.T5: Implement Runtime API WebSocket State Bus

Description:
Implement WebSocket state bus:
- one authenticated active GUI WebSocket.
- full `OperatorStateSnapshot` on connect.
- domain-scoped patches.
- events.
- command results.
- throttling/coalescing hooks.
- disconnected client cleanup.

Do not wire every domain yet; provide system/session/runtime status domains and
event transport.

Acceptance:
- [x] Authenticated WebSocket receives full snapshot.
- [x] Patches/events use contract models.
- [x] High-rate domains can be throttled/coalesced.
- [x] Disconnect releases WebSocket resources without necessarily ending
  session until heartbeat timeout.

Tests:
- [x] WebSocket connect/snapshot/patch/event tests.

Implementation notes:
- Added `RuntimeStateBus` with active-GUI WebSocket ownership, full snapshot on
  connect, typed patch/event/command-result sends, per-domain patch coalescing,
  and disconnect cleanup.
- Runtime API WebSocket route now delegates to the bus after session lease
  validation.
- Verification passed: Runtime pytest suite and submodule lock verification.

#### P2.T4: Install `iii-runtime-api` As A Separate Systemd Service

Description:
Add systemd unit, install scripts, devcontainer post-start integration, and
runtime profile setup for `iii-runtime-api.service`.

Service relationship:
- Separate from `iii-system-daemon.service`.
- May use `Wants=iii-system-daemon.service`.
- Must not require daemon to remain running.
- Autostarts alongside daemon in sim/dev and real deployments.

Acceptance:
- [x] Unit file exists and is installed in devcontainer.
- [x] Service starts on devcontainer post-start.
- [x] Service can remain active if daemon is stopped/restarted.
- [x] Logs are available through normal service logs.

Tests:
- [x] Service install script dry run or devcontainer verification.
- `systemctl status iii-runtime-api.service` in devcontainer when available:
  not run because this host shell is not the active devcontainer systemd
  environment; installer/service-file tests passed.

Implementation notes:
- Added `tools/systemd/iii-runtime-api.service` with `Wants=` but no
  `Requires=` dependency on `iii-system-daemon.service`.
- Added `scripts/systemd/install_runtime_api_service.sh` with `--dry-run` and
  integrated it into `.devcontainer/post_start.sh`.
- Verification passed: service-file tests, installer dry run, Runtime pytest
  suite, and submodule lock verification. Live `systemctl status` was not run
  because this host shell is not the active devcontainer systemd environment.

#### P2.T3: Implement Runtime API Systemd And Daemon Socket Adapter

Description:
Implement runtime API adapters to:
- report API up.
- check `iii-system-daemon.service` active state.
- start/restart/check daemon via systemd.
- ping daemon Unix socket.
- call daemon commands over Unix socket once available.

Preserve the current daemon ownership model: daemon owns ROS launch and
supervision; runtime API is the network API around it.

Acceptance:
- [x] Runtime API reports API state, daemon systemd state, daemon socket
  state, runtime booted state, and system active state.
- [x] Runtime API can start/restart/check daemon through systemd.
- [x] Runtime API can call daemon status/list/log-dir commands over Unix socket.
- [x] Runtime API remains up when daemon is down.

Tests:
- [x] Fake systemd adapter tests.
- [x] Fake Unix socket daemon tests.
- [x] Existing daemon tests remain passing.

Implementation notes:
- Added `RuntimeSystemAdapter` for systemd active/start/restart checks and
  daemon socket status/list/log-dir calls.
- Exposed runtime status and daemon adapter routes through the FastAPI app.
- Verification passed: Runtime tests, Supervision daemon tests, and submodule
  lock verification.

#### P2.T2: Add Remote CLI Token Authentication And Conflict Policy

Description:
Implement separate remote CLI token auth in `iii-runtime-api`.

Rules:
- CLI token auth is non-interactive.
- Read-only CLI operations are allowed during active GUI session.
- Mutating CLI operations are blocked during active GUI session.
- Rejected mutating CLI requests are logged and emitted as runtime events when
  a GUI session exists.

Acceptance:
- [x] CLI token is configured separately from browser password.
- [x] Read-only CLI calls pass during active GUI session.
- [x] Mutating CLI calls fail with clear conflict error during active GUI
  session.
- [x] Client metadata, command ID, timestamp, and rejection reason are logged
  when available.

Tests:
- [x] Auth tests.
- [x] Session conflict tests.

Implementation notes:
- Added CLI command classification, token-gated remote CLI command endpoint,
  GUI-session conflict policy, and bounded runtime event capture for rejected
  CLI mutations.
- Verification passed: Runtime and Contracts pytest suites and submodule lock
  verification.

#### P2.T1: Add Browser Password Session And Heartbeat Lease

Description:
Implement browser GUI authentication in `iii-runtime-api`:
- simple deployment-configured password/token.
- one active browser session at a time.
- second session rejected while active.
- session token survives page refresh.
- heartbeat every 2 seconds.
- release lease after 8 seconds missed.
- explicit logout releases immediately.

Acceptance:
- [x] Login returns a session token usable by REST and WebSocket.
- [x] Second login is rejected while first session heartbeat is fresh.
- [x] Refresh-style reuse of session token works.
- [x] Active session metadata includes acquisition time, last heartbeat, and
  client label/address when available.
- [x] Session expires after missed heartbeat.
- [x] Logout releases active session.
- [x] Forced takeover/handoff is not implemented in v2; future support must be
  explicit and auditable.

Tests:
- [x] Unit tests for acquisition, rejection, heartbeat, expiry, logout, and refresh.

Implementation notes:
- Added `BrowserSessionLease` with one-session active lease, heartbeat update,
  missed-heartbeat expiry, refresh-style token reuse, and explicit release.
- Runtime API session routes now return session metadata and enforce the lease
  for REST and WebSocket access.
- Verification passed: runtime pytest suite and submodule lock verification.

#### P2.T0: Implement `iii-runtime-api` FastAPI Skeleton

Description:
Create the runtime-host FastAPI app in `III-Drone-Runtime`. Add routes for:
- minimal unauthenticated identity.
- health/status.
- browser login/logout/session.
- authenticated WebSocket.
- authenticated generic command endpoints.
- remote CLI token-auth endpoints.

Do not add ROS logic in this task beyond stubs. Use `III-Drone-Contracts`
models for all API payloads.

Acceptance:
- [x] Runtime API starts with uvicorn.
- [x] `/identity` returns only minimal unauthenticated metadata.
- [x] Authenticated endpoints reject missing/invalid credentials.
- [x] OpenAPI schema is generated from contracts.

Tests:
- [x] FastAPI test client tests.
- [x] `python3 -m pytest src/III-Drone-Runtime/test`

Implementation notes:
- Added FastAPI app factory, uvicorn console entrypoint, identity/health,
  session, command/service, CLI token, and WebSocket skeleton routes.
- Added FastAPI TestClient coverage for unauthenticated identity, auth
  rejection, login, command/service stubs, CLI token stub, OpenAPI schemas, and
  WebSocket initial snapshot.
- Verification passed: runtime pytest suite and submodule lock verification.

#### P1.T5: Generate TypeScript Types From Contracts

Description:
Add tooling to generate TypeScript types from `III-Drone-Contracts` into
`III-Drone-GC` frontend sources. The generated artifacts may be checked in if
useful, but must be marked generated and not manually edited.

Acceptance:
- [x] A reproducible command generates TypeScript types.
- [x] Generated files are consumed by frontend code.
- [x] CI/test command verifies generated types are current.
- [x] Generated file header says not to edit manually.

Tests:
- [x] Type generation command.
- [x] Frontend typecheck command after generation.

Implementation notes:
- Added `scripts/generate_typescript.py`, generated
  `src/III-Drone-GC/frontend/src/generated/contracts.ts`, and added a frontend
  API type facade that imports generated types.
- Added minimal frontend TypeScript config and package scripts for typecheck
  and generated-contract freshness checks.
- Verification passed: generator `--check`, `npm run typecheck`,
  `npm run contracts:check`, Contracts pytest suite, and submodule lock
  verification.

#### P1.T4: Define Map/Geometry Contracts

Description:
Define frontend-friendly contracts for map/perception data transformed by
runtime API:
- powerline-relative frame status.
- live perception conductor estimates.
- stored overview conductor geometry.
- drone pose in relevant projections.
- target state and target history.
- trajectory/path.
- drone trail.
- freshness/staleness/degraded state.

The contract must not expose raw ROS message shapes to the frontend.

Acceptance:
- [x] Map state supports powerline-orthogonal and top-down 2D projections.
- [x] Live perception and stored overview are distinguishable.
- [x] Missing/stale overview or live perception has explicit degraded state.
- [x] Auto-fit bounds input data is represented.

Tests:
- [x] Contract examples for empty, live-only, overview-only, and combined states.

Implementation notes:
- Added map/projection, conductor, pose, target, path/trail, degraded source,
  and auto-fit bounds contracts without exposing ROS message shapes.
- Verification passed: `python3 -m pytest src/III-Drone-Contracts/test` and
  submodule lock verification.

#### P1.T3: Define Configuration Manifest And Snapshot Contracts

Description:
Define ROS-free Pydantic contracts for structured configuration UI:
parameter groups/nodes, names, types, current values, loaded snapshot,
default snapshot, descriptions, constraints, restart-required metadata,
pending/apply result payloads, snapshot list/load/save/download/default
requests, and status badges.

Badge semantics:
- `Pending edits`: frontend-staged changes not applied.
- `Unsaved`: current runtime/config values differ from loaded snapshot.
- `Non-default`: loaded snapshot is not configured default.
- Show either `Unsaved` or `Non-default`; `Unsaved` takes precedence.

Acceptance:
- [x] Parameter manifest model supports grouping, type, constraints, restart
  required, current value, default/reference metadata.
- [x] Apply results can return per-parameter success/error.
- [x] Snapshot operations are modeled.
- [x] Badge/status semantics are encoded.

Tests:
- [x] Contract validation tests for manifests, apply results, and snapshot status.

Implementation notes:
- Added configuration manifest, parameter, constraint, apply-result, snapshot
  operation, and status/badge contracts.
- Verification passed: `python3 -m pytest src/III-Drone-Contracts/test` and
  submodule lock verification.

#### P1.T2: Define Command Identifiers And Permission-Relevant Enums

Description:
Define stable command identifiers and enums in `III-Drone-Contracts`.

Include:
- PX4 commands: `px4.arm`, `px4.takeoff`, `px4.land`, `px4.hold`.
- Mode commands: `mission.activate`, `custom_operation.activate`.
- Custom operation starts/cancel/validate.
- Payload commands.
- Perception/PL mapper commands.
- Configuration apply/save/load/snapshot/default commands.
- Runtime controls: boot/start/stop/restart/shutdown/service mutation.
- Rosbag controls.
- Control-owner states: `unknown`, `px4_manual_or_position`, `px4_hold`,
  `mission`, `custom_operation_idle`, `custom_operation_active`,
  `transitioning`, `degraded_conflict`.

Acceptance:
- [x] All command IDs are defined once in contracts.
- [x] Control-owner states match the spec.
- [x] Exact PX4 mode/nav state is represented separately from coarse owner.
- [x] Tests ensure command ID values are stable strings.

Tests:
- [x] Contract enum tests.

Implementation notes:
- Added `CommandId`, `ControlOwnerState`, exact PX4 state enums, and handler
  permission classifications in `iii_drone_contracts.commands`.
- Verification passed: `python3 -m pytest src/III-Drone-Contracts/test` and
  submodule lock verification.

#### P1.T1: Define Domain State Models

Description:
Define domain models for the WebSocket state domains:
`system`, `vehicle`, `control`, `mission`, `operation`, `perception`,
`powerline`, `payload`, `configuration`, `simulation`, and `events`.

Each domain state must include latest value, source timestamp where available,
runtime API receive/update timestamp, freshness/staleness state, source
availability, and degraded/error reason when known.

Acceptance:
- [x] `OperatorStateSnapshot` contains all agreed domains.
- [x] Each domain has freshness/source/degraded metadata.
- [x] Snapshot and per-domain patch models validate.
- [x] Event models support runtime and local event source labels.

Tests:
- [x] Contract serialization tests for full snapshot and each domain patch.

Implementation notes:
- Added explicit domain state models and updated `OperatorStateSnapshot` to
  carry all agreed domains as first-class fields.
- Added per-domain patch validation and runtime/frontend event-source tests.
- Verification passed: `python3 -m pytest src/III-Drone-Contracts/test` and
  submodule lock verification.

#### P1.T0: Define Core API Envelope Models

Description:
In `III-Drone-Contracts`, define Pydantic models for common API envelopes:
identity, API version, error/rejection, command request, command response,
service call request, action start response, WebSocket snapshot, patch, event,
and command result messages.

Models must be JSON-serializable and ROS-free. Include timestamp fields,
source labels, request IDs, command IDs, domain names, stale/degraded status,
and compatibility metadata.

Acceptance:
- [x] Models exist for identity, errors, commands, snapshots, patches, events,
  and command results.
- [x] Models include API version metadata.
- [x] Serialization round-trip tests cover valid and invalid examples.
- [x] No model imports ROS or runtime packages.

Tests:
- [x] `python3 -m pytest src/III-Drone-Contracts/test`

Implementation notes:
- Added core Pydantic envelopes for API identity/version, errors/rejections,
  command/service requests and responses, action-start responses, snapshots,
  patches, events, command results, and WebSocket messages.
- Added serialization and validation tests plus explicit import-boundary
  verification.
- Host test environment needed `anyio<4` because the preinstalled pytest is
  6.2.5 and the latest AnyIO pytest plugin requires newer pytest internals.

#### P0.T5: Extract Or Isolate Shared Core Math Utilities

Description:
Audit runtime/API dependencies on helper math utilities currently located in
`src/III-Drone-Core`. If `iii-runtime-api` only needs selected math helpers,
extract them into a small importable utility module/package or otherwise
isolate the dependency so `III-Drone-Runtime` does not pull in unrelated core
runtime behavior.

Rules:
- Do not make `III-Drone-GC` depend on Core.
- Keep extracted utilities ROS-free when possible.
- Preserve existing Core imports through compatibility wrappers if needed.
- Avoid broad Core refactors beyond the helper-math dependency needed for GUI
  v2/runtime API.

Acceptance:
- [x] Runtime API dependency on Core is either removed or narrowed to a
  documented utility surface.
- [x] Shared math utilities have unit tests.
- [x] Existing Core users continue to import successfully.
- [x] `III-Drone-GC` remains independent of Core and ROS.

Tests:
- [x] Targeted Core/runtime utility tests.
- [x] Dependency/import guard tests for `III-Drone-GC`.

Implementation notes:
- Added `iii_drone_runtime.geometry` as a small ROS-free geometry helper
  surface for runtime API map/projection shaping.
- Documented that Runtime does not depend on Core for GUI/API data shaping.
- Added runtime geometry tests and a runtime dependency guard that rejects Core
  imports in runtime sources.
- Verification passed: runtime geometry/boundary tests, GC boundary test, Core
  compatibility import check, and submodule lock verification.

#### P0.T4: Preserve Current Tk GUI As Reference Only

Description:
Mark current Tk GUI files as legacy/reference during v2 implementation. Do not
delete them in this task. Ensure the new v2 files live alongside legacy code
without depending on `IIIGCNode`.

Reference files:
- `src/III-Drone-GC/iii_drone_gc/gui.py`
- `src/III-Drone-GC/iii_drone_gc/gc_node.py`
- `src/III-Drone-GC/test/test_gc_node_logic.py`

Acceptance:
- [x] Legacy GUI remains runnable if needed during transition.
- [x] New v2 frontend/proxy code does not import `IIIGCNode`.
- [x] Documentation identifies legacy GUI as parity reference, not v2
  foundation.

Tests:
- [x] Static import/dependency check for new GC proxy/frontend sources.

Implementation notes:
- Updated GC README to identify Tk GUI files as legacy parity/reference code.
- Added `iii_drone_gc.v2_proxy` namespace and a static guard test that rejects
  `IIIGCNode`/legacy GC node imports in v2 proxy/frontend code.
- Verification passed: v2 boundary test, legacy file presence check, and
  submodule lock verification.

#### P0.T3: Move Daemon Transport Code Ownership Into Runtime Package

Description:
Move or wrap daemon transport/client code from
`src/III-Drone-Supervision/iii_drone_supervision/system_daemon.py` and
`tools/III-Drone-CLI/iii/system_client.py` into `III-Drone-Runtime` without
breaking local CLI behavior.

`III-Drone-Supervision` should retain system graph, lifecycle, and supervision
domain logic. `III-Drone-Runtime` should own the long-lived transport/API
surface and call supervision domain objects as needed.

Acceptance:
- [x] Daemon Unix-socket request/response behavior is preserved.
- [x] Existing daemon commands still map to `SystemManager` behavior.
- [x] Local CLI can still boot/start/status through local daemon path.
- [x] Tests for daemon client/server round trip pass after move/wrap.

Tests:
- [x] Existing `tools/III-Drone-CLI/test/test_system_client.py`
- [x] Existing `src/III-Drone-Supervision/test/test_system_daemon.py`
- [x] New `src/III-Drone-Runtime/test` daemon transport tests.

Implementation notes:
- Added `iii_drone_runtime.daemon.client.DaemonClient` as the runtime-owned
  Unix-socket/systemd client and changed `tools/III-Drone-CLI/iii/system_client.py`
  into a compatibility wrapper.
- Added runtime daemon-client tests and fixed the supervision test path harness
  so existing daemon routing tests can resolve workspace-owned dependencies.
- Verification passed: CLI system-client tests, supervision daemon tests,
  runtime tests, and submodule lock verification.

#### P0.T2: Define Workspace Dependency And Build Metadata

Description:
Update workspace manifests, Dockerfiles, `requirements.txt`, setup scripts, and
package metadata so `III-Drone-Contracts`, `III-Drone-Runtime`, and
`III-Drone-GC` build in the intended order.

Dependency rules:
- `III-Drone-GC` depends only on `III-Drone-Contracts`.
- `III-Drone-Runtime` depends on `III-Drone-Contracts`,
  `III-Drone-Supervision`, and `III-Drone-Interfaces`.
- `III-Drone-Contracts` depends on no ROS package.

Acceptance:
- [x] `colcon list --base-paths src` includes the new packages.
- [x] `III-Drone-GC` manifests do not reference ROS runtime packages or
  `III-Drone-Interfaces`.
- [x] `III-Drone-Runtime` manifests include runtime-side dependencies.
- [x] Python dependency files include FastAPI, uvicorn, pydantic, mDNS library,
  and frontend generation tooling where needed.

Tests:
- [x] `colcon list --base-paths src`
- [x] Targeted package metadata/import tests for the three packages.

Implementation notes:
- Added runtime submodule branch metadata, updated top-level Python dependency
  requirements, made GC package metadata depend only on contracts/FastAPI/
  Pydantic, and verified runtime/contract package metadata.
- Verification passed: `colcon list --base-paths src`,
  `python3 -m pytest src/III-Drone-Contracts/test src/III-Drone-Runtime/test`,
  metadata dependency check, and `./scripts/git/verify_submodule_lock.sh`.

#### P0.T1: Create `III-Drone-Runtime` Submodule Skeleton

Description:
Create a new workspace-owned submodule/package under `src/III-Drone-Runtime`.
This package will own the daemon transport/control plane and `iii-runtime-api`.
Add package metadata, tests, README, and an importable module such as
`iii_drone_runtime`.

Initial dependencies should include `III-Drone-Contracts` and later
`III-Drone-Supervision`, `III-Drone-Interfaces`, FastAPI, uvicorn, Pydantic,
MAVSDK/pymavlink where needed, and ROS runtime dependencies in the runtime
image. Keep runtime code out of `III-Drone-GC`.

Acceptance:
- [x] `src/III-Drone-Runtime` exists and is importable.
- [x] Package metadata and README define daemon/API ownership.
- [x] Empty or minimal test suite runs.
- [x] Submodule lock is updated and verified if this is a new submodule ref.

Tests:
- [x] `python3 -m pytest src/III-Drone-Runtime/test`
- [x] `./scripts/git/verify_submodule_lock.sh`

Implementation notes:
- Added `iii_drone_runtime` ament Python package skeleton, runtime API console
  entrypoint placeholder, package metadata, resource marker, README ownership
  notes, and import test.
- Refreshed `deps/submodule-lock.txt`; lock verification passed.

#### P0.T0: Create `III-Drone-Contracts` Submodule Skeleton

Description:
Create a new workspace-owned submodule/package under `src/III-Drone-Contracts`.
It should be ROS-free and Python/Pydantic-based. Add package metadata, tests,
README, pyproject/setup as appropriate for the repo's packaging style, and a
minimal importable module such as `iii_drone_contracts`.

Record that this package is the source of truth for API contracts and generated
frontend TypeScript types. It must not import `rclpy`, ROS messages, MAVSDK,
`III-Drone-Interfaces`, or runtime packages.

Acceptance:
- [x] `src/III-Drone-Contracts` exists and is importable.
- [x] Package metadata declares only non-ROS dependencies needed for contracts.
- [x] A test proves importing `iii_drone_contracts` does not require ROS.
- [x] README documents ownership, ROS-free constraint, and generation workflow.
- [x] Submodule lock is updated and verified if this is a new submodule ref.

Tests:
- [x] `python3 -m pytest src/III-Drone-Contracts/test`
- [x] `./scripts/git/verify_submodule_lock.sh`

Implementation notes:
- Added `iii_drone_contracts` ament Python package skeleton, package metadata,
  resource marker, import guard test, and ROS-free README ownership notes.
- Refreshed `deps/submodule-lock.txt`; lock verification passed.
