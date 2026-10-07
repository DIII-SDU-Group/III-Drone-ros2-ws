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
runtime, opens a session (no password), reads every operator state domain, and writes artifacts
under `log/gui-v2-sim-e2e-smoke/`. Mutating sim-only workflow and flight command
extensions are documented in
`src/III-Drone-GC/docs/gui-v2-sim-e2e-smoke.md`.

## Canonical Inspection Endurance (SIM and HIL)

Purpose: prove that the canonical `inspection-production` mission cycles
Inspection → Reach Cable → Charge → Leave Cable → Inspection automatically
for a sustained window, with clean node logs, on the current source. The
same tool judges SIM (all processes in the workspace devcontainer) and HIL
(mission/control on the Pi, SITL and adapters on the workstation).

Authority boundary: the runner starts only passive observers (lifecycle
observer and perception probe) and the automatic-cycle driver
`scripts/workspace/hil_inspection_cycle_driver.py`. The driver arms, takes
off, flies the mission and performs its own landed/disarmed cleanup; it never
writes PX4 firmware or parameters. HIL runs without the propulsion battery
(see `AGENTS.md`/`CLAUDE.md`); no physical confirmation is requested.

Prerequisites: a ready stack for the target profile.

```bash
./iii-dev stack status
```

```bash
./iii-dev hil status
```

Run (default 1800 s window, at least 4 in-window cycles). `--fresh-start`
first recreates the simulation epoch (`stack stop` + `stack start --recreate-sim`, or
`hil restart`) so the vehicle starts from its spawn pose; always use it after
an interrupted or failed run:

```bash
python3 scripts/workspace/run_inspection_endurance.py --target sim --fresh-start
```

```bash
python3 scripts/workspace/run_inspection_endurance.py --target hil --fresh-start
```

Evidence lands in `runtime/endurance/<target>-<UTC>/`: `run_plan.json`
(source identity of the superproject and every submodule),
`driver_events.json`, `mission_lifecycle_observation.json`,
`execution_result.json`, `log_findings.json` (grouped ERROR/WARN node log
lines written during the run) and `acceptance_report.json`. Success is exit
code 0 with `"accepted": true`; every failed check is listed under
`failures`. Only cycles whose Inspection resumption was received inside the
window count. Any ERROR/FATAL node log line or Core continuity fault fails the
run; add `--strict-warnings` to also fail on WARN lines.

Every cable release in the run is also judged from the PX4 SITL flight logs
(`scripts/workspace/cable_release_ulog.py`, written to
`cable_release_report.json`). Each counted cycle needs a release, and each
release must keep PX4's land detector clear until CableTakeoff, push with
thrust above hover and below saturation, stay pressed against the conductor
(at most 0.1 m/s vertical speed and 5 cm of estimated vertical drift), and let CableTakeoff sag at most
5 cm below its reference.

Before flying, the runner proves that the target runs the installed tracked
parameter defaults (`scripts/workspace/check_parameter_provenance.py`, written
to `parameter_provenance.json`). It refuses to run when the profile selector
(`profiles/<scope>.yaml`, scope `sim` or `hil`) points at a saved snapshot, or
when the living `tracked/default.yaml` holds locally preserved values that
differ from the installed default. Either way the run would qualify parameters
that no commit describes. In SIM, restore the tracked defaults recoverably with
`iii config sim reset`, which seals a checkpoint of the old state first.

In HIL the runner also starts `scripts/workspace/physical_px4_disarm_monitor.py`
on the Pi. It passively listens to the physical PX4's MAVLink heartbeats on the
HIL port (UDP `14542`, from `10.41.10.2`) for the whole run and writes
`physical_px4_heartbeats.jsonl` and `physical_px4_disarm.json`. HIL acceptance
fails if that record is missing, has a gap over 5 s, comes from another peer,
or shows the physical PX4 armed at any point.

For isolated checks, `--fast-cycles` commands each recharge after 20 s of
healthy Inspection instead of waiting for the simulated battery to drain. Every
mode transition and its evidence is still judged, including full-charge proof,
but the report is labelled `"fast_cycles": true` because the automatic
low-battery trigger is not exercised. The qualification campaign stays
battery-driven.

```bash
python3 scripts/workspace/run_inspection_endurance.py --target sim --fresh-start --strict-warnings --fast-cycles --duration-sec 900 --required-cycles 3
```

Re-judge existing evidence without flying:

```bash
python3 scripts/workspace/run_inspection_endurance.py --evaluate-only runtime/endurance/<run-dir>
```

Isolated operator-Hold handover check (verify mode-exit behavior before a full
campaign): the driver flies the mission to the chosen phase, takes PX4 Hold as
an operator would after `--hold-after-sec`, proves Runtime API Hold ownership,
every mission mode inactive with no tree running and PX4 staying in Hold for
the observation window, then lands. Acceptance additionally requires zero
ERROR and zero WARN node log lines for the whole run (recorder transport loss
on best-effort streams is reported separately as `recording_lost_messages`):

```bash
python3 scripts/workspace/run_inspection_endurance.py --target sim --fresh-start --scenario hold --hold-phase reach_cable
```

Phases: `inspection_demo`, `reach_cable`, `leave_cable`.

### OptiTrack rehearsal (SIM)

The OptiTrack lab profile is rehearsed in the SIM devcontainer before a lab
session. The rehearsal runs the `opti_track` runtime profile against PX4 SITL
with a vision-only estimator and the simulated lab gateway, which republishes
Gazebo's ground truth as the lab's rigid-body pose. Stop the SIM stack first
(`./iii-dev stack stop`); the devcontainer needs `chrony` installed, because
the profile gates on a settled clock as on the aircraft.

```bash
scripts/workspace/run_opti_track_rehearsal.py
```

It starts the environment (`tools/simulation/opti_track_rehearsal.sh start`),
runs the scenarios below in order, stops the environment, and writes
`runtime/rehearsal/sim-<UTC>/report.json`. The exit status is 0 only when
every scenario passed, the aircraft ended landed and disarmed, and the node
logs hold no WARN, ERROR or FATAL line other than the designed ones (the
EXPERIMENTAL-mission notice and the relay reporting the injected outage).
Samples the recorder lost are reported separately.

| Scenario | What it proves |
| --- | --- |
| Profile restrictions | the canonical mission and cable intents are refused |
| Motion-capture readiness gate | an 8 s pose outage on the ground refuses a mission start and never arms; readiness returns by itself |
| M3 cycle with Proceed | ground start (the mission arms), takeoff, Proceed, shuttle, landing, disarm |
| M3 cycle without Proceed | the 60 s Proceed timeout lands the aircraft |
| M1 hover, M2 maneuvers | takeover from a pilot hover and handback to PX4 Hold |
| M4 mode loop | ground start and two rounds of the four-mode loop, ended by the operator's Hold |

The rehearsal flies the installed tracked default of the `real` parameter
family, which `opti_track` uses, and the missions' placeholder geometry. For
the run it sets the host's own living `opti_track` parameters aside and puts
them back when the environment stops. It does not replace the lab acceptance in
[the session checklist](opti-track-lab-session-checklist.md): axes, the
rigid-body ID, the cage geometry and hover thrust are only known at the lab.

### OptiTrack rehearsal (HIL)

The same scenarios run with the Pi in the `opti_track` profile against the
workstation's PX4 SITL. Deploy first (`iii deploy dev --host HOST --build
--restart`) and stop any running HIL session (`./iii-dev hil stop`).

```bash
scripts/workspace/run_opti_track_rehearsal.py --target hil --host 192.168.1.251
```

`tools/simulation/opti_track_hil_rehearsal.sh start --host HOST` starts the HIL
workstation simulation with the vision-only estimator and the simulated lab
gateway, whose pose publisher is in lab ROS domain 0, as the lab's gateway is.
On the Pi it adds a systemd drop-in that switches the daemon and the Runtime
API to `opti_track` while keeping the HIL transport. `stop` removes the
drop-in and returns the Pi to its provisioned HIL profile. The report is
`runtime/rehearsal/hil-<UTC>/report.json`, judged as in SIM from the Pi's node
logs; rcl's start-up notice about the Pi's provisioned `ROS_LOCALHOST_ONLY=0`
is also a designed line there. The physical flight controller is not involved and stays disarmed.

### Qualification campaign (SIM, then deploy, then HIL)

A change is qualified when one commit passes the strict SIM run, is deployed,
and then passes the strict HIL run. The campaign runs that sequence and stops
at the first failure. The stages are:

1. SIM endurance with `--fresh-start --strict-warnings`.
2. `./iii-dev stack stop`.
3. `iii deploy dev --host HOST --build --restart`.
4. HIL endurance with `--fresh-start --strict-warnings`, including the physical PX4 disarm record.
5. `./iii-dev hil stop`. This runs whenever HIL may have started, even after a failure.

```bash
python3 scripts/workspace/run_qualification_campaign.py --host 192.168.1.251
```

The campaign refuses tracked uncommitted changes in the superproject or any
submodule unless you pass `--allow-dirty`; the dirty repositories are then
recorded. Evidence lands in `runtime/qualification/<UTC>-<sha>/`, with
`sim/` and `hil/` run directories, one log per stage and
`campaign_report.json`. That report holds the source identity, each stage's
exit code, elapsed time and acceptance summary, and `"qualified": true` only
when every stage passed. The command takes about 75 minutes with the default
1800 s windows. Run it detached, for example under `setsid nohup`, so a closed
terminal does not interrupt it.

On failure, keep the run directory, inspect `failures` and `log_findings.json`,
and bring the stack to a known state with `./iii-dev stack status` or
`./iii-dev hil status` before the next attempt. The evaluator tests run
offline with `python3 -m pytest scripts/workspace/test_run_inspection_endurance.py
scripts/workspace/test_physical_px4_disarm_monitor.py
scripts/workspace/test_run_qualification_campaign.py`.
