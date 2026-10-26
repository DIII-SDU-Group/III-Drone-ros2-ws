# HIL Pi hostname defaults and command overrides

Status: **Completed for code and documentation; live HIL remains unverified.** The three material decisions are recorded below. Execution evidence is in `hil-pi-hostname-defaults.execution.md`.

## Objective, authority, and boundaries

The original request is reproduced verbatim in the appendix. Its operational aim is to remove the fixed `10.42.0.15` *target default*, use `iii.local` for the Pi everywhere the HIL/operator flow chooses a target, and allow an explicit argument to override the target in relevant commands. The old address remains only where it represents intentional static fallback provisioning, immutable historical evidence, or an explicit caller/test value.

Repository authority: `AGENTS.md` §§2, 4–7, 10; `.agents/skills/backlog-planning/SKILL.md`; `docs/README.md`; `docs/runtime-launch-and-node-graph.md`; `docs/build-and-environments.md`; `docs/dependency-governance.md`; `docs/simulation-and-px4-integration.md`; `docs/host-provisioning.md`; `docs/deployment-hardware-roles.md`; `docs/ground-computer-installation.md`; `docs/automation-ready-authoring-contract.md`. The HIL topology and safety rules remain in force: the Pi owns ROS/runtime; workstation owns Gazebo/PX4 SITL; launcher start/stop must retain its endpoint/owner checks; deployment is the editable checkout workflow and never arms the vehicle or writes PX4 firmware/parameters. Only III package tests may run. Submodule refs are lock governed. There is extensive pre-existing dirty work across these files and submodules; implementation must preserve it.

## Current code findings

| Target selection surface | Current behavior | Planning implication |
| --- | --- | --- |
| `setup/setup_hil.bash:6–7,44` | Exports `III_HIL_PI_ADDRESS=10.42.0.15`, uses it for Runtime API URL and CycloneDDS peer; `setup/setup_field.bash:22` and `setup/remote_runtime.bash:6` propagate the host to SSH/API aliases. | A hostname default must flow consistently through all aliases. Current profile default defeats the launcher's `iii.local` endpoint default. Merely setting `III_HIL_PI_ADDRESS=iii.local` is also insufficient: `launch_hil_workstation.sh:510` requires that variable's effective value to be an IPv4 literal for its peer record. |
| `tools/simulation/launch_hil_workstation.sh:30–33,112–133,450–468,519–545` | `III_HIL_PI_ENDPOINT` defaults to `iii.local`, but host-side `host_resolve_pi_address` maps that exact name to `10.42.0.15`. `WORKSTATION_ADDRESS` defaults to `10.42.0.1`, and `link_probe` refuses a routed Pi if the selected source address differs. PX4 MAVLink/XRCE, ownership probes, and owner records require a concrete IPv4 address. | Remove both fixed target and fixed workstation-route assumptions. Resolve the selected hostname and derive the matching workstation source address/interface from the chosen route before host/container handoff; preserve that identity across lifecycle/owner checks. An unreachable name should fail clearly. |
| `scripts/workspace/iii_dev.sh:742–966` and `scripts/workspace/coordinate_hil_restart.py:690–730` | `iii-dev hil` start/restart/status/stop accept mode/status flags but no Pi host argument; coordinator inherits environment and uses Runtime API plus launcher. | Add explicit user-facing host argument and pass one selected target through API, SSH CLI, launcher, GC proxy, status and rollback paths. Keep `logs` host-independent. |
| `tools/simulation/launch_hil_workstation.sh:69–95` | Direct launcher accepts `--headless`/`--rendered` for start and no host argument for status/stop. | Direct invocation needs a host argument with equivalent override semantics, including stop/status ownership checks. |
| `tools/simulation/run_hil_perception_seam_probe.sh:12,18–32` | Probe defaults to fixed IP, rejects all arguments; uses address for SSH/SCP, DDS, and launcher handoff. | Make hostname the default and add target argument; preserve stationary/probe safety and immutable attempt artifacts. |
| `scripts/workspace/resolve_sim_fixture.py:28–41,102–103` and `scripts/workspace/gui_v2_sim_e2e_smoke.py:784–785,1529–1590` | HIL-only DDS helper branches fall back to fixed Pi and workstation IPs; no Pi-host argument, although the smoke tool has a separate Runtime API URL argument. | Make HIL target hostname default, derive the workstation source address from its route, add explicit argument where tools can run independently, and pass the same selected target to nested helper calls. SIM defaults remain local. |
| `tools/III-Drone-CLI/iii/{deploy,host,px4}.py` | `--host` already defaults to `iii.local` for deploy/status/provision/inspect; `runtime_routing.py:176–226` rejects conflicting SSH/API aliases. | Preserve existing override interface and conflict guard; add regression coverage for old-IP explicit override and clean HIL profile default. |

Non-operational occurrences: `tools/III-Drone-CLI/iii/host.py:58` seeds the direct-link static fallback `10.42.0.15/24`; `deployment/tests/test_developer_host.py:92` and `tools/III-Drone-CLI/test/test_host_direct.py:71` assert that provisioning contract. `docs/host-provisioning.md` and `docs/deployment-hardware-roles.md` describe it; changing that address would alter network provisioning, beyond a client default change. `HIL_CANONICAL_MISSION_RESULT_2026-09-23.md` reports a completed attempt with its actual address and must retain historical truth. Existing test fixtures may deliberately use `10.42.0.15` to test an explicit override, owner identity, or mDNS advertisement; only expectations that encode a *default* should change. `docs/ground-computer-installation.md:79` and `docs/simulation-and-px4-integration.md:31–52` currently teach the fixed address as an operational default and need revision.

## Decided implementation contract

- The public target argument is `--host <hostname-or-IPv4>` (or `--pi-host` where an existing `--host` has another meaning) on `iii-dev hil` start/restart/status/stop, direct launcher, coordinator if invoked directly, the standalone probe, and independent HIL fixture/smoke helpers. Existing `iii host`, `iii deploy`, and `iii px4` keep their `--host` behavior. `hil logs` has no Pi target to override. For each invocation, an explicit argument wins over inherited endpoint/SSH/API defaults; derive all API/SSH/DDS/PX4/GC target aliases from that argument and make the selected host visible in status/diagnostics and tests.
- Default identity is `iii.local`. Accept a reachable LAN or direct-link Pi address. Resolve it where a literal IPv4 is required (PX4 integer parameter, MAVLink peer, UDP owner matching); derive the matching workstation source address/interface dynamically from the route to that resolved peer. Preserve the concrete peer and source address per operation and across host/container handoff (the devcontainer uses host networking in `.devcontainer/devcontainer.json:59–66`). A live owner record and its concrete peer remain authoritative for safe stop/status even if DNS later changes. Fail clearly if the selected host is unresolvable or unreachable; never silently fall back to `10.42.0.15`.
- `III_HIL_PI_ENDPOINT` represents a hostname or explicit target; `III_HIL_PI_ADDRESS` remains an optional concrete IPv4 override/derived peer for code that requires one. A clean profile selects endpoint `iii.local` and leaves the address override unset until resolution, while deriving API/SSH/DDS hostname references coherently. Legacy environment overrides remain supported for scripted callers when no target argument is supplied. `III_HIL_WORKSTATION_ADDRESS` remains an optional explicit source-address override; absent it, route detection supplies the address instead of fixed `10.42.0.1`.
- No task below alters Pi static fallback provisioning, immutable HIL evidence, PX4 firmware/parameters, unrelated simulations, or generated build/install/log artifacts.

## Execution backlog

### HN-01 — Normalize the HIL profile's target default

- **Status:** Completed. **Dependencies:** none.
- **Ownership:** `setup/setup_hil.bash`, `setup/setup_field.bash`, `setup/remote_runtime.bash`; related profile tests in `scripts/workspace/test_hil_transport_profiles.py`, `tools/III-Drone-CLI/test/test_runtime_routing.py`, and `tests/integration/test_field_shell_setup.py`. These profile files are shared with HN-03; schedule sequentially.
- **Outcome/invariants:** source a clean HIL profile with `iii.local` as the Pi endpoint, leaving the optional IPv4 address override unset; derive Runtime API, SSH routing and DDS peer consistently, preserve explicit overrides and existing runtime profile/domain/transport choices. Derive the workstation DDS source address from the selected Pi's route when `III_HIL_WORKSTATION_ADDRESS` is unset. Do not mix aliases from different Pis.
- **Acceptance:** clean-shell profile assertions show `iii.local` for target aliases and API URL, with no fake IPv4 literal stuffed into `III_HIL_PI_ADDRESS`; an explicit target selects coherent aliases despite inherited defaults, and an explicit workstation source-address override is honored. SIM/real/OptiTrack profile behavior remains unchanged.
- **Focused verification:** bash profile tests with isolated environment and fake executables; targeted III CLI routing tests. No live Pi connection.

### HN-02 — Resolve the selected Pi once in the workstation launcher

- **Status:** Completed after final review corrections to status route and multiple-address selection. **Dependencies:** HN-01.
- **Ownership:** `tools/simulation/launch_hil_workstation.sh`, `scripts/workspace/test_launch_hil_workstation.py`, `tests/integration/test_hil_workstation_launcher.py`.
- **Outcome/invariants:** remove the special `iii.local → 10.42.0.15` mapping and the fixed `10.42.0.1` source-address default. Accept `--host` for relevant direct actions; select one reachable IPv4 result for hostname targets, derive its workstation source address/interface from the actual route, and carry the concrete peer/source pair through host/container handoff. Preserve start/status/stop ownership checks and fail-closed behavior. Do not let DNS drift or a different peer claim another process.
- **Acceptance:** fake resolver/route tests cover `iii.local` on LAN `192.168.1.251` with no direct-link route, direct-link Pi, explicit hostname/IP, multiple/address-change cases, route or SSH unreachability, host/container agreement, PX4 address conversion, and refusal to stop unowned or ambiguous endpoints. `III_HIL_WORKSTATION_ADDRESS` override is checked against the selected route rather than silently used for another link. No default execution path embeds `10.42.0.15` or `10.42.0.1`.
- **Focused verification:** launcher fake-command suite and shell syntax; no HIL launch.

### HN-03 — Expose one Pi-host argument through HIL lifecycle orchestration

- **Status:** Completed. **Dependencies:** HN-01, HN-02.
- **Ownership:** `scripts/workspace/iii_dev.sh`, `scripts/workspace/coordinate_hil_restart.py`, `scripts/workspace/test_iii_dev.py`, `scripts/workspace/test_coordinate_hil_restart.py`; shared `setup/setup_hil.bash` integration follows HN-01.
- **Outcome/invariants:** `./iii-dev hil start|restart|status|stop --host X` selects one Pi across coordinator Runtime API, native CLI/SSH, launcher and HIL GC proxy. Direct coordinator use gets the same argument. Keep headless/json/verbose handling, source identity checks, preflight, staged rollback, and exact owner selection.
- **Acceptance:** fake API/SSH/launcher/GC captures all point to X even when different inherited endpoint/SSH/API defaults were present; no-argument use points to `iii.local`. Failure and rollback tests verify no cross-host stop. `hil logs` stays a local artifact command.
- **Focused verification:** focused III workspace tests with fake processes; shell syntax and `--help` checks.

### HN-04 — Update standalone HIL helpers and nested calls

- **Status:** Completed. **Dependencies:** HN-01–03.
- **Ownership:** `tools/simulation/run_hil_perception_seam_probe.sh`, `scripts/workspace/resolve_sim_fixture.py`, `scripts/workspace/gui_v2_sim_e2e_smoke.py`, and their focused `scripts/workspace/test_*` files. These helper files do not overlap HN-02/03 ownership.
- **Outcome/invariants:** default each HIL-only Pi target to `iii.local`, provide `--host` or `--pi-host` on independently invoked commands, derive matching workstation route/source address, and propagate selection into SSH/SCP, DDS, launcher, fixture and smoke child calls. Keep SIM local defaults and existing probe no-flight/single-attempt safeguards.
- **Acceptance:** argument/no-argument tests capture the same selected host and matching workstation source address on every HIL call path, including LAN and direct-link routes; explicit old IP still works; SIM paths never acquire a Pi target. Probe artifact records the actual selected host/resolved peer without rewriting past artifacts.
- **Focused verification:** helper/parser/fake-command III tests, shell syntax and dry-run/help where available. Do not run a live probe or smoke mission for this planning change.

### HN-05 — Reconcile operator docs and finish bounded verification

- **Status:** Completed for code and documentation; live HIL unverified. **Dependencies:** HN-01–04.
- **Ownership:** `docs/simulation-and-px4-integration.md`, `docs/ground-computer-installation.md`, `docs/host-provisioning.md`, `docs/deployment-hardware-roles.md`, relevant CLI/help text and the first-draft backlog as decision record. Preserve `HIL_CANONICAL_MISSION_RESULT_2026-09-23.md` unchanged. Integrator owns shared documentation edits after code tasks.
- **Outcome/invariants:** show no-argument `iii.local` operation on reachable LAN or direct-link routes, explicit host argument examples and argument precedence over inherited defaults; distinguish the Pi's static `10.42.0.15/24` rescue link from the client default. Preserve physical safety and deployment instructions.
- **Acceptance:** exact source search classifies every remaining `10.42.0.15` occurrence as provisioning, historical evidence, or intentional explicit-IP test/example; no HIL operational target default remains. Complete focused III tests after each task, then the applicable full III regression once for this phase; verify submodule lock only if a governed submodule ref is intentionally changed. An actual hostname route/installed host check, if available and safely owned, is recorded separately from fake-command evidence; lack of access is reported as unverified.
- **Focused verification:** `rg` audit, documentation command checks, III test suite for touched surfaces, `git diff --check`, and final review against the verbatim request. No third-party package tests.

## Decision record and remaining uncertainty

The parent consolidated the original request with current HIL evidence and resolved the first draft's three material questions for this plan:

1. **Resolved hostname route:** Accept `iii.local` resolving to the reachable LAN or direct-link Pi address. Select the matching workstation route/interface dynamically for HIL transport and fail clearly if unreachable. The parent reports Pi reachability on LAN `192.168.1.251` while the direct-link route is absent, so requiring `10.42.0.1/24` would reject a valid HIL route. The launcher still must pin a concrete peer for PX4 and owner checks (`tools/simulation/launch_hil_workstation.sh:519–545,1248–1258`). This clears HN-02, HN-04, and HN-05.
2. **Resolved argument precedence:** An explicit `--host`/`--pi-host` argument wins over inherited endpoint, SSH, and Runtime API defaults for that invocation. Derive those aliases consistently from the selected host; verify precedence in tests. The current profile and runtime router (`setup/setup_hil.bash:6–7`, `tools/III-Drone-CLI/iii/runtime_routing.py:176–226`) explain why coordinated alias updates are necessary. This clears HN-01–05.
3. **Resolved static fallback:** Preserve `10.42.0.15/24` only as deliberate Pi static fallback provisioning and historical evidence (`tools/III-Drone-CLI/iii/host.py:58`, `docs/host-provisioning.md:8–13`, `HIL_CANONICAL_MISSION_RESULT_2026-09-23.md:5,22`); remove operational target defaults. This clears HN-05's documentation scope.

No material planning question remains. Execution still must verify its selected host and source route before HIL mutation; this ready backlog grants no independent authority to launch HIL, deploy to the Pi, arm a vehicle, or change PX4 firmware/parameters. This plan came from a loose chat instruction, so the parent determined that a pre-question independent document verifier was not required.

## Verbatim original request

> Investigate all places where the old IP is fixed, then change it to iii.local as default everywhere, but allow argument overwrite in all relevant commands.
