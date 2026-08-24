# Deployment Infrastructure Redesign Backlog

## Context

The legacy repository at `~/Workspace/repos/III-Drone-deployment` is retired as
the design baseline. Its useful udev mappings and historical behavior may be
migrated, but its Docker Compose-per-node runtime, mutable branch checkout,
`latest` image publication, password SSH, and direct `rsync --delete` workflow
must not survive as the production deployment architecture.

The replacement deployment infrastructure belongs directly in this workspace
under `deployment/`. It is workspace-owned integration glue and must evolve
atomically with the III CLI, runtime setup, canonical supervision graph,
configuration schema, dependency lock, and release format. A nested deployment
submodule is rejected. A source-independent deployment repository is deferred
until field operators can work entirely from published release artifacts.

### Settled decisions

- The onboard Raspberry Pi never compiles III code.
- The first and only initially supported aircraft hardware class is a Raspberry
  Pi 5 Model B booting from an SD card. RAM capacity is deliberately omitted
  from inventory, release compatibility, and provisioning variation.
- The target platform baseline is Ubuntu Server 24.04 LTS ARM64 with ROS 2
  Jazzy. Ubuntu 24.04 is the first Ubuntu LTS supporting Raspberry Pi 5 and the
  matching Jazzy ARM64 Tier 1 platform.
- All III builds happen on a separate development computer or CI builder using
  a target-compatible ARM64 build environment.
- The normal near-term workflow starts from a local clone of this workspace.
- A dirty workspace, including modified submodules and relevant untracked
  source files, must be deployable without first committing to Git.
- Dirty deployments are immutable **field-development releases**, not anonymous
  file synchronization. Their provenance includes the base commit, submodule
  revisions, dirty patch/content hash, builder, toolchain, target, configuration
  schema, and build time.
- Clean, locked, tagged workspaces produce **qualified releases**. Both release
  classes use the same artifact and onboard activation path.
- Host systemd remains the top-level onboard process owner. Whether it starts
  the III daemon and independently supervised runtime API natively or as a
  small number of release-image containers is unresolved by Q3. In either
  case, the III daemon owns the canonical ROS launch graph and daemon-managed
  processes; deployment must not recreate per-node ownership.
- Production does not depend on a Git checkout, `/home/iii/ws`, or build trees
  on the aircraft. Docker/Compose use is pending Q3; the legacy per-node Compose
  graph remains rejected.
- Releases are immutable and installed side by side. Activation and rollback
  are atomic; persistent aircraft data lives outside release directories.
- Deployment and self-update transport uses SSH. Remote runtime operation uses
  `iii-runtime-api`; the deployment path must remain usable while that process
  is being updated or restarted.
- The III CLI remains the operator-facing deployment surface.
- GUI parameter tuning persists onboard. Every field test has an identifiable
  baseline and release, accepted changes are journaled, and the final state can
  be captured back to the development computer.
- Captured tuning is not automatically merged into tracked defaults. Promotion
  is deliberate and classifies changes as global defaults, hardware-class
  defaults, aircraft-specific configuration, retained experiments, or rejected
  changes.
- Code rollback and configuration rollback are related but independently
  tracked. Schema compatibility must be checked before either activation or
  rollback.
- No implementation changes begin until this backlog's grilling phase resolves
  the full design scope.
- Grilling questions are numbered sequentially (`Q1`, `Q2`, ...) and their
  answers are retained in this backlog so decisions remain traceable.

### Current code findings

- `tools/III-Drone-CLI/iii/build.py` contains the existing cross-compilation
  entry point, but it mixes mutable `cc_ws` synchronization, emulated package
  builds, custom sysroot construction, and `latest` Docker images.
- `tools/III-Drone-CLI/iii/deploy.py` and `ssh_manager.py` expose the legacy
  branch checkout, password SSH, disabled host-key checking, source/build/install
  synchronization, and container publication workflows.
- `setup/setup_real.bash` still sources ROS Humble and workspace paths through
  `/arm64-sysroot`, while the development baseline and workspace documentation
  target ROS Jazzy.
- `tools/systemd/iii-system-daemon.service` and
  `tools/systemd/iii-runtime-api.service` are development units. They source
  `setup_dev.bash`, use `/home/iii/ws`, and embed simulation/development runtime
  API defaults.
- `src/III-Drone-Supervision/iii_drone_supervision/system_spec.py` is the
  canonical runtime graph and must remain the source of runtime process and
  lifecycle ownership. Deployment must not recreate that graph in Ansible,
  systemd, or Compose.
- `src/III-Drone-Configuration/iii_drone_configuration/schema_utils.py` already
  seeds writable runtime configuration from packaged defaults while preserving
  operator-selected parameter sets. It does not yet define release/schema
  compatibility, transactional migrations, provenance-rich tuning sessions, or
  reversible promotion.
- `src/III-Drone-Runtime` already places the runtime API onboard, separates it
  from the ground-control computer, enforces real-profile identity/secrets, and
  exposes runtime and configuration operations used by GUI v2.
- The workspace dependency lock freezes top-level submodule revisions and is a
  required input to every qualified or field-development release identity.

### Provisional onboard layout

The exact paths remain subject to grilling, but the design starts from:

```text
/opt/iii/releases/<release-id>/    immutable releases
/opt/iii/current                   active release selector
/opt/iii/previous                  rollback selector
/etc/iii/                          host identity, environment, secrets
/var/lib/iii/                      configuration, calibration, snapshots, state
/var/log/iii/                      retained runtime and deployment logs
/run/iii/                          sockets, locks, transient state
```

### Open decision register

- Raspberry Pi boot firmware policy, attached hardware declaration, and when a
  second hardware class becomes justified.
- Whether attached hardware introduces a driver constraint beyond the stock
  Ubuntu 24.04 Raspberry Pi kernel.
- Whether immutable releases are native install bundles or OCI images, and the
  exact split between host systemd and container process ownership.
- Factory installation medium and whether image creation must work offline.
- Expected fleet size and whether aircraft inventory is local-only or shared.
- Whether most tuned parameters are global, hardware-class, or aircraft-specific.
- Whether the full accepted parameter-change journal or only checkpoints must
  survive power loss.
- Required field-update transfer time, downtime, free-storage reserve, and
  retention count for old releases.
- Whether deployment is allowed only while fresh PX4 telemetry proves landed
  and disarmed, and what offline maintenance override is acceptable.
- Artifact signing, key custody, SSH trust bootstrap, secrets storage, and
  operator authorization policy.
- Whether PX4 firmware participates in III releases or remains independently
  commissioned.
- OS update policy: controlled package maintenance and physical reimage versus
  a future A/B root-filesystem updater.
- Required recovery behavior after power loss during staging, migration,
  activation, first boot, and rollback.

### Grilling decision log

- **Q1 — Initial aircraft hardware class:** Settled. Raspberry Pi 5 Model B
  booting from an SD card. RAM is intentionally not inventoried or used as a
  compatibility discriminator.
- **Q2 — Target OS and ROS baseline:** Settled. Ubuntu Server 24.04 LTS ARM64
  with ROS 2 Jazzy. Remaining work must validate attached hardware drivers and
  remove Humble assumptions.
- **Q3 — Onboard Docker role:** Unanswered. Decide whether releases are native
  install bundles or immutable OCI images, while preserving host-systemd
  ownership and the daemon's canonical ROS graph.

## Incomplete

### P0: Resolve Architecture And Contracts

Phase acceptance:

- [ ] Every open decision that affects implementation has an agreed answer.
- [ ] Domain terms and architecture decisions are recorded without duplicating
      runtime ownership already defined by the existing ADRs.
- [ ] Release, configuration, safety, and recovery contracts are concrete enough
      for later tasks to be implemented independently.

#### P0.T0: Complete The Deployment Design Interview

Description:
Use the `grill-me` decision tree to resolve target hardware, platform, image,
release, transport, safety, configuration, security, fleet, recovery, and
operational policy. Update this context and downstream tasks after every answer.

Acceptance:

- [ ] The open decision register contains no implementation-blocking ambiguity.
- [ ] Rejected alternatives and their load-bearing reasons are retained.
- [ ] Dependencies between decisions are reflected in phase/task ordering.

Tests:

- Manual backlog review against the settled decision record.

#### P0.T1: Record Deployment Domain Language And ADRs

Description:
Add deployment terms to the workspace domain documentation and record decisions
whose rationale must survive future architecture reviews. At minimum define
qualified release, field-development release, aircraft configuration, tuning
session, activation, acceptance, and rollback.

Acceptance:

- [ ] `CONTEXT.md` or an indexed deployment `CONTEXT.md` defines stable terms.
- [ ] ADRs record repository ownership, offboard-only builds, immutable release
      activation, and persistent configuration separation where warranted.
- [ ] Existing Operations Interface and runtime ownership ADRs are not reopened.

Tests:

- `rg -n "Qualified Release|Field-Development Release|Tuning Session" CONTEXT.md deployment docs`

#### P0.T2: Specify Release And Compatibility Manifests

Description:
Define versioned schemas for release identity, source provenance, target
platform, dependency lock, toolchain, included packages, checksums, configuration
schema compatibility, PX4 compatibility, and qualification evidence. Define
which fields are required for qualified versus field-development releases.

Acceptance:

- [ ] Manifests identify clean, dirty, modified-submodule, and untracked-source states.
- [ ] A target can reject an incompatible artifact before runtime shutdown.
- [ ] A release and exported tuning capture can be correlated unambiguously.
- [ ] Manifest schemas reject unknown incompatible versions.

Tests:

- Schema validation fixtures for clean, dirty, incompatible, incomplete, and tampered manifests.

#### P0.T3: Specify Filesystem, Ownership, And Persistence Contracts

Description:
Finalize release, persistent data, secret, log, and transient paths; ownership;
permissions; retention; disk-space reservations; and behavior across activation,
rollback, OS reboot, and physical reimage.

Acceptance:

- [ ] Production runtime has no dependency on `/home/iii/ws` or source paths.
- [ ] Release activation cannot overwrite persistent aircraft configuration.
- [ ] Secrets never enter release bundles or Git.
- [ ] Disk exhaustion behavior is explicit and testable.

Tests:

- Filesystem contract test against a temporary target root.

#### P0.T4: Plan Legacy Deployment Retirement

Description:
Inventory any remaining authoritative data in `III-Drone-deployment`, migrate
the udev mappings and relevant historical knowledge, mark the repository
retired, and define when it can be archived. Do not carry forward its Compose
runtime or mutable workspace synchronization.

Acceptance:

- [ ] Every retained legacy behavior has an explicit destination and rationale.
- [ ] No current documentation directs operators to the legacy repository.
- [ ] Archival occurs only after replacement provisioning and field update paths pass acceptance.

Tests:

- `rg -n "III-Drone-deployment|docker-compose.yml" README.md docs setup scripts tools deployment`

### P1: Build Reproducible Offboard Releases

Phase acceptance:

- [ ] Neither qualified nor field-development release construction executes on an aircraft.
- [ ] The same target contract drives laptop and CI builders.
- [ ] Built output is immutable, complete, attributable, and target-compatible.

#### P1.T0: Establish The Canonical ARM64 Target Environment

Description:
Replace the Humble/Jazzy and host/sysroot ambiguity with one versioned target
definition covering Ubuntu, ROS, architecture, Python ABI, compiler, system
libraries, and hardware-specific dependencies. Generate or acquire the sysroot
without copying mutable state from an aircraft.

Acceptance:

- [ ] `setup_real.bash`, build images, and manifest target metadata agree.
- [ ] Builder inputs are digest-pinned and reproducible.
- [ ] Target incompatibility is detected before transfer or activation.

Tests:

- Build and run a target ABI probe in an ARM64 target-equivalent environment.

#### P1.T1: Capture Dirty Workspace Provenance

Description:
Implement a source snapshot module that accounts for the top-level repository,
all governed submodules, modified tracked content, relevant untracked files,
ignored/generated exclusions, and the dependency lock. It must fail when source
classification is ambiguous rather than silently omit files.

Acceptance:

- [ ] Identical source content yields the same content identity.
- [ ] Modified submodules and untracked source change the identity.
- [ ] Secrets, build trees, logs, datasets, and unrelated artifacts are excluded.
- [ ] A human-readable provenance report accompanies every field-development release.

Tests:

- Automated fixtures for clean, tracked-dirty, submodule-dirty, untracked-source,
  ignored-secret, and generated-output workspaces.

#### P1.T2: Implement Cached Cross-Compilation

Description:
Build III packages offboard using persistent compiler/build caches, package
change detection, and targeted colcon rebuilds while producing a complete
non-symlinked install tree suitable for immutable deployment. Preserve the
III-only test policy.

Acceptance:

- [ ] No build action is sent to the aircraft.
- [ ] Incremental field builds reuse caches without contaminating release output.
- [ ] The output contains no absolute developer-workspace dependencies.
- [ ] Failed partial builds cannot be packaged as complete releases.

Tests:

- Clean build, no-change rebuild, single-package change, interface change with
  downstream rebuild, and deliberate compile failure.

#### P1.T3: Package And Verify Release Bundles

Description:
Package the install tree, release manifest, installed runtime assets, migration
metadata, and checksums into a transportable artifact. Add signing after the
key policy is settled. Keep generated bundles outside Git.

Acceptance:

- [ ] Extraction recreates a complete release without source/build trees.
- [ ] Corruption or manifest/content disagreement is detected.
- [ ] Qualified and field-development release classes share one artifact format.
- [ ] Bundle contents and compressed size are inspectable before deployment.

Tests:

- Round-trip package/extract/verify and tampered-content rejection.

### P2: Implement Transactional Onboard Release Management

Phase acceptance:

- [ ] A staged release never mutates the active release.
- [ ] Activation, health acceptance, and rollback survive command interruption and power loss.
- [ ] Runtime safety gates prevent deployment during aircraft operation.

#### P2.T0: Implement Release Staging And Retention

Description:
Install verified bundles into release-ID directories, enforce storage reserves,
track current/previous/candidate state, and garbage-collect only releases that
are neither active nor required for rollback or evidence retention.

Acceptance:

- [ ] Re-staging the same release is idempotent.
- [ ] An active or rollback release cannot be garbage-collected.
- [ ] Insufficient storage fails before modifying runtime state.

Tests:

- Temporary-root tests for first install, duplicate install, low disk, retention,
  interrupted extraction, and corrupt staging.

#### P2.T1: Implement Safety-Gated Activation

Description:
Before stopping runtime, verify aircraft identity, release compatibility, fresh
PX4 state, control ownership, Mission Execution state, and configuration
migration readiness according to the settled maintenance policy. Persist the
transaction before each irreversible step.

Acceptance:

- [ ] Activation is rejected while safety state is stale or operational gates fail.
- [ ] Any maintenance override is explicit, audited, narrowly authorized, and cannot be accidental.
- [ ] Activation never starts Mission Execution or a Direct Operation.

Tests:

- State-matrix tests for landed/disarmed, armed, airborne, Mission-owned,
  Custom Operation, stale PX4, unavailable runtime API, and maintenance override.

#### P2.T2: Implement Activation Health And Automatic Rollback

Description:
Atomically select the candidate, restart required systemd/runtime processes,
verify daemon/runtime/configuration/ROS/hardware readiness, mark acceptance, and
restore code plus configuration checkpoints on failure.

Acceptance:

- [ ] Success is reported only after defined health gates pass.
- [ ] Failed health restores a known previous release without activating autonomy.
- [ ] Recovery resumes correctly after power loss at every transaction stage.
- [ ] Diagnostic evidence is retained for failed activation and rollback.

Tests:

- Fault injection at each persisted transaction stage and each health gate.

#### P2.T3: Replace The SSH Deployment Adapter

Description:
Replace password files, `sshpass`, disabled host-key checks, agent forwarding,
and shell-interpolated commands with key-based SSH, strict host identity,
structured command execution, explicit transfer destinations, and least-privilege
elevation.

Acceptance:

- [ ] Unknown or changed host keys fail closed.
- [ ] Secrets are never printed, placed in release artifacts, or stored in world-readable files.
- [ ] Aircraft IDs are verified independently of hostname/address.
- [ ] User-controlled values cannot alter remote command structure.

Tests:

- SSH adapter tests for trusted, unknown, changed-key, unreachable, unauthorized,
  interrupted-transfer, and hostile-argument cases.

#### P2.T4: Rebuild The III CLI Deployment Surface

Description:
Replace legacy `install`, `container`, and raw synchronization behavior with
build, inspect, stage, activate, deploy-development, rollback, status, and
configuration-capture workflows backed by the release modules. Keep runtime
operation on the existing `iii system ...` path.

Acceptance:

- [ ] Every mutation supports a useful dry-run or preflight report.
- [ ] CLI output always names target aircraft and release identity.
- [ ] Commands return machine-meaningful failure status and retain diagnostics.
- [ ] Legacy destructive synchronization is unavailable.

Tests:

- CLI parser and orchestration tests plus a local fake-target end-to-end test.

### P3: Provision Raw Ubuntu Into A Converged Aircraft Host

Phase acceptance:

- [ ] A documented raw-image workflow creates an SSH-reachable target.
- [ ] Ansible converges that target into the complete III host baseline.
- [ ] A second convergence run reports no unintended changes.

#### P3.T0: Create Autoinstall And First-Boot Profiles

Description:
Define the minimal image/bootstrap layer for storage, boot, host identity,
networking, initial key trust, and Ansible reachability. Keep application
installation out of cloud-init beyond what is necessary to establish the host.

Acceptance:

- [ ] Profiles are validated before writing media.
- [ ] No production password or aircraft secret is embedded in committed files.
- [ ] Failed first boot leaves diagnosable local evidence.

Tests:

- Automated image/VM boot where Raspberry Pi hardware permits, plus physical-media acceptance.

#### P3.T1: Implement Idempotent Ansible Host Roles

Description:
Create roles for OS baseline, ROS installation, III user/groups, directories,
udev, hardware dependencies, network/time, firewall, log retention, systemd,
release installer prerequisites, and health inspection. Do not encode the ROS
runtime graph outside `system_spec.py`.

Acceptance:

- [ ] Roles support check/diff mode where technically possible.
- [ ] Second application is idempotent.
- [ ] Hardware-class variation is data-driven rather than copied playbooks.
- [ ] OS package changes are pinned/auditable and separated from application deployment.

Tests:

- Ansible syntax/lint, check mode, first convergence, second-run idempotence,
  and drift-repair tests on a target-equivalent host.

#### P3.T2: Install Real-Profile Systemd Units

Description:
Create production units and stable launcher/environment files for the III daemon
and runtime API. Use the active immutable release, real profile, persistent
paths, unique identity, external secret files, restart policy, and correct
ordering without sourcing development shell profiles.

Acceptance:

- [ ] Units contain no dev credentials, sim profile, or `/home/iii/ws` dependency.
- [ ] Runtime Stop can leave the independently supervised runtime API online.
- [ ] A broken active release fails visibly and remains recoverable through SSH.

Tests:

- `systemd-analyze verify` plus boot, restart, failure, and release-switch tests.

#### P3.T3: Implement Inventory, Identity, And Secret Provisioning

Description:
Define aircraft and hardware-class inventory data while keeping real secrets and
private local inventory outside Git. Provision stable aircraft/runtime IDs,
runtime API credentials, SSH trust, and operator-network policy.

Acceptance:

- [ ] Example inventory is safe to commit and sufficient to document all fields.
- [ ] Real aircraft identity is stable across release changes and physical reboot.
- [ ] Missing/generic identity or development credentials fail real-profile startup.

Tests:

- Inventory schema tests and converged-host identity/security acceptance.

### P4: Make Field Tuning Durable And Traceable

Phase acceptance:

- [ ] GUI tuning survives runtime restart and release deployment as intended.
- [ ] Every captured value can be traced to aircraft, release, schema, baseline,
      session, and operator action.
- [ ] Promotion to tracked configuration is deliberate and reviewable.

#### P4.T0: Version Configuration Compatibility And Migrations

Description:
Add explicit schema versions, supported upgrade/downgrade ranges, migration
planning, staged-copy validation, backups, and reversible activation behavior
around the existing configuration seeding model.

Acceptance:

- [ ] Compatibility is checked before runtime shutdown.
- [ ] Migration never edits the only copy of aircraft configuration.
- [ ] Rollback either proves compatibility or restores the paired checkpoint.

Tests:

- N-1 to N, N to N-1, incompatible, failed migration, interrupted migration,
  and preserved-snapshot fixtures.

#### P4.T1: Implement Tuning Sessions And Change Journaling

Description:
Introduce an explicit session identity and baseline. Journal accepted and
rejected GUI parameter edits with old/new values, restart semantics, aircraft,
release, schema, operator/request identity, timestamp, and result. Define
power-loss durability after the interview settles the required guarantee.

Acceptance:

- [ ] Session baseline cannot be confused with current mutable state.
- [ ] Constant/restart-required values and pending boot state are represented.
- [ ] A test may restore its baseline without changing code release.

Tests:

- Runtime, constant, rejected, repeated, concurrent, restart, rollback, and
  power-loss journal tests.

#### P4.T2: Export Provenance-Rich Tuning Captures

Description:
Create an immutable capture containing manifest, baseline, final values, ordered
changes, pending boot values, relevant runtime events, and operator notes. Pull
it through the III CLI to a local field-capture directory without mutating Git.

Acceptance:

- [ ] Capture integrity and release/schema correlation are verifiable offline.
- [ ] Repeating capture does not overwrite prior evidence.
- [ ] Partial or interrupted captures are distinguishable from complete captures.

Tests:

- Capture/export/verify round trip, collision handling, interrupted transfer,
  and tampered capture rejection.

#### P4.T3: Implement Configuration Comparison And Promotion

Description:
Compare a capture with its recorded baseline and current tracked configuration.
Support explicit classification into global default, hardware-class default,
aircraft-specific override, retained experiment, or rejection. Detect source
changes since the field baseline and require reconciliation instead of silently
overwriting files.

Acceptance:

- [ ] Promotion produces a reviewable minimal change.
- [ ] Per-aircraft calibration cannot silently become a fleet default.
- [ ] Wrong schema, baseline, aircraft, or release provenance fails closed.

Tests:

- Clean promotion, concurrent source change, schema mismatch, aircraft mismatch,
  partial promotion, and rejection fixtures.

### P5: Validate, Document, Commission, And Retire Legacy Paths

Phase acceptance:

- [ ] Fresh provisioning, repeated field-development deployment, tuning capture,
      qualified release deployment, rollback, and recovery pass end to end.
- [ ] Operators can perform normal workflows through the III CLI without source
      knowledge or direct filesystem mutation on the aircraft.
- [ ] Legacy deployment paths are removed or clearly blocked.

#### P5.T0: Build The Deployment Verification Matrix

Description:
Create layered tests for manifest/schema logic, source capture, builder output,
temporary-root release management, fake SSH targets, target-equivalent ARM64
hosts, first-boot images, and physical Raspberry Pi/hardware acceptance.

Acceptance:

- [ ] CI runs all hardware-independent tests.
- [ ] Hardware-required tests are scripted and produce retained evidence.
- [ ] Upgrade and rollback cover clean and dirty releases plus tuned configuration.

Tests:

- Repository deployment test runner documented by this task.

#### P5.T1: Commission The First Aircraft From Raw Image

Description:
Exercise the complete factory path on the intended Raspberry Pi and hardware:
image, first boot, Ansible convergence, identity/secrets, release install,
hardware readiness, field update, tuning capture, rollback, reboot, and recovery.

Acceptance:

- [ ] A wiped target reaches a healthy inactive real-profile runtime using only documented inputs.
- [ ] PX4, mmWave, camera, charger/gripper, runtime API, logs, and configuration pass acceptance.
- [ ] Recovery from deliberately interrupted activation is demonstrated.

Tests:

- Signed commissioning record with artifact IDs and captured command output.

#### P5.T2: Publish Operator And Maintainer Runbooks

Description:
Document factory provisioning, builder setup, dirty field deployment, qualified
release creation, target inspection, tuning sessions, capture/promotion,
rollback, log retrieval, OS maintenance, lost credentials, disk pressure, and
physical recovery.

Acceptance:

- [ ] Commands match tested CLI behavior.
- [ ] Safety-critical steps state prerequisites and stop conditions.
- [ ] Runbooks identify which workflows require source checkout and which do not.

Tests:

- Independent walkthrough against a clean development computer and aircraft.

#### P5.T3: Remove Legacy CLI And Deployment Behavior

Description:
After replacement acceptance, remove branch-based remote install, Docker image
deployment, password SSH, source/build/log synchronization, stale remote branch
variables, Humble/sysroot runtime assumptions, and documentation references.
Archive the old deployment repository according to P0.T4.

Acceptance:

- [ ] No supported CLI path can perform the retired destructive workflow.
- [ ] Dependency and documentation searches find no accidental old entry point.
- [ ] Historical migration notes identify the last legacy version and replacement commands.

Tests:

- CLI regression tests and repository-wide retired-pattern audit.

## In-Progress

## Completed
