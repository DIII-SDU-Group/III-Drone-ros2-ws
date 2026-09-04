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
- Raw SD-card provisioning starts from a checksum-pinned official Ubuntu Server
  24.04 ARM64 Raspberry Pi image. Deployment generates first-boot cloud-init
  data and then applies Ansible convergence; III does not maintain a custom
  disk-image fork initially.
- Field deployment, rollback, target inspection, log retrieval, tuning capture,
  and repeated Ansible convergence work without internet access when the laptop
  and aircraft have a local connection. Initial dependency-cache population and
  fresh SD provisioning may require internet access in the first implementation.
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
- Qualified/full releases may be produced only from the currently uncreated
  release branch, with a clean committed top-level workspace and clean governed
  submodules, verified dependency lock, complete required build/tests, explicit
  version tag, signed evidence-bearing manifest, and explicit qualified deploy
  action. Any other branch or workspace state produces only a field-development
  release, regardless of whether it builds or tests successfully.
- Host systemd is the top-level onboard process owner and runs the III daemon
  and independently supervised runtime API natively. The III daemon owns the
  canonical ROS launch graph and daemon-managed processes; deployment must not
  recreate per-node ownership.
- Production does not depend on a Git checkout, `/home/iii/ws`, Docker,
  Docker Compose, or build trees on the aircraft.
- Releases are immutable and installed side by side. Activation and rollback
  are atomic; persistent aircraft data lives outside release directories.
- Deployment and self-update transport uses SSH. Remote runtime operation uses
  `iii-runtime-api`; the deployment path must remain usable while that process
  is being updated or restarted.
- A host-installed onboard deployment receiver, independently supervised by
  systemd and outside replaceable release directories, owns staging state,
  activation, health deadlines, acceptance, boot reconciliation, and rollback.
  Once an activation request is accepted, loss of the CLI, SSH, network, or
  operator computer cannot prevent timeout handling or rollback.
- Initial Ansible convergence installs the trusted deployment receiver and its
  host recovery substrate. Normal signed deployment bundles may update the
  receiver through a separately transactional self-update path; application
  release switching must never directly overwrite the running receiver.
- The III CLI remains the operator-facing deployment surface.
- GUI parameter tuning persists onboard. Every field test has an identifiable
  baseline and release, accepted changes are journaled, and the final state can
  be captured back to the development computer.
- Captured tuning is not automatically merged into tracked defaults. Promotion
  is deliberate and classifies changes as tracked defaults, retained
  experiments, or rejected changes.
- Deployment never overwrites an existing onboard value for a parameter that
  remains in the release manifest and never discards a removed parameter value.
  It reconciles every applicable active onboard parameter set to the current
  manifest: newly introduced keys are inserted with release defaults, existing
  current-schema values are preserved, and removed/legacy keys are atomically
  moved with their last active values and provenance into a persistent onboard
  shadow store outside the active configuration tree. Active files contain the
  current schema; the shadow survives releases and supports compatible rollback.
  Reconciliation is transactional, journaled, and independent of release files.
- Pulling tuned parameters from either `iii.local` or simulation into source is
  a first-class III CLI workflow. Captures can be promoted only as changes to
  the tracked `real` or `sim` profile default in `III-Drone-Configuration`,
  validated against the source manifest, committed through the governed
  feature/stacked-PR workflow, and thereby included in a later release. Git
  commits provide default history; a qualified release tag fixes the exact
  defaults packaged by that release. Deployment never performs source promotion.
- Deployment models one shared logical aircraft target. Physically different
  Raspberry Pis are not distinguished in inventory: they use the same hardware
  class, setup, runtime identity, credentials, and configuration, and at most
  one is connected to the operator at a time. No central or per-aircraft fleet
  inventory is in scope.
- SSH deployment trusts that the single `iii.local` endpoint on the local
  operator network is the intended target. Physical Pi host keys are not
  inventoried or authenticated. This explicitly accepts local DNS/mDNS spoofing
  and man-in-the-middle risk; server identity is outside the initial threat model.
- SSH client authentication is key-based with one keypair per authorized
  operator computer. Cloud-init installs the first public key; an authenticated,
  audited deployment workflow can add and revoke public keys later. Private keys
  are never copied between the development workstation and ground-control computer.
- The `iii` SSH account is the key-only human development and field-research
  identity. It has an interactive shell and explicit unrestricted passwordless
  sudo and is never used by Ansible or the deployment receiver. The separate
  `iii-deploy` identity is unprivileged, forced-command-only automation with no
  sudo path. Password and root login remain disabled; forwarding and tunneling
  remain disabled; the firewall limits SSH to the operator network.
  Broader one-time bootstrap authority used by initial Ansible provisioning is
  still removed after convergence.
- Every qualified and field-development release bundle is cryptographically
  signed. The root deployment helper verifies a trusted signer and all content
  checksums before staging; unsigned, unknown-signer, or tampered bundles are rejected.
- Each build computer owns an independent release-signing keypair. Targets trust
  revocable public signing keys provisioned through the deployment helper;
  private signing keys are never shared between computers. Computers that only
  operate the runtime do not require a signing key.
- Normal release activation requires fresh PX4 telemetry proving landed and
  disarmed, with no Mission Execution, Custom Operation, or active Reference
  Owner. If a broken runtime cannot report safety state, recovery requires an
  explicit interactive maintenance override that first stops all III units,
  requires physical-safety confirmation, and is audit logged. Unattended use of
  the override is prohibited by default.
- Release retention has two tracks. The latest qualified/full release is a
  protected recovery anchor that field-development deployment cannot replace or
  garbage-collect. During field development the target also retains the active
  field-development release and the immediately previous field-development
  release, plus one temporary staged candidate. Failed candidate contents may
  be removed after retaining their manifest and diagnostics.
- The expected workflow is: build and deploy a qualified/full release from the
  home workstation; update the same workspace branch on the ground-control
  laptop; then make, cross-compile, sign, and deploy field-development releases
  from that laptop while preserving the qualified recovery anchor.
- Code rollback and configuration rollback are related but independently
  tracked. Schema compatibility must be checked before either activation or
  rollback.
- Repository governance assumes a solo maintainer. Pull requests and required
  machine checks remain mandatory for `develop`, `main`, and `release`, but no
  human approval count is required. Promotion intent, evidence, and resulting
  mutations must be explicit and auditable; the design must not depend on a
  second reviewer or a broad maintainer bypass.
- Release and pull-request tooling is designed as a deterministic CI/CD and
  AI-agent interface as well as a human interface: non-interactive operation,
  structured input/output, stable exit codes, preflight/dry-run support,
  idempotent retries, resumable state, bounded permissions, and retained
  evidence are first-class contracts. Human-readable summaries remain required.
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
- The current Configuration GUI stages edits only in browser memory until the
  operator presses Apply. Apply sends each edited key sequentially rather than
  as one atomic multi-key transaction. Each successful key is validated, pushed
  live to every declaring node, read back, and immediately persisted onboard as
  a full automatically named runtime snapshot; constants are persisted for the
  next boot. There is no tuning-session identity or per-change history.
- Onboard persistence updates the active-profile selector, so accepted values
  are intended to survive an ordinary reboot. Snapshot YAML and selector writes
  are direct file writes rather than a fsync/atomic-rename transaction, however,
  so power-loss durability is not guaranteed. Automatic snapshot replacement is
  tracked only in server memory; after a server restart, older automatic
  snapshots may accumulate.
- The GUI can explicitly save, list, load, and select named onboard snapshots.
  The backend's download operation returns YAML only for the currently active
  snapshot. The frontend currently discards that returned content and merely
  shows a success notification, so it does not create a ground-control file.
- Configuration state is hydrated when the GUI websocket connects, but normal
  periodic state publication and generic configuration-command handling do not
  publish or merge a refreshed configuration state. The displayed table and
  snapshot list can therefore remain stale after apply/save/load/default/list
  commands until a full rehydration. Existing frontend tests verify command
  dispatch but do not cover local download or post-command state refresh.
- There is currently no continuous or accepted-change mirror on the ground-
  control computer, no immutable tuning capture, no operator/session provenance,
  and no direct path from GUI tuning into the repository promotion workflow.
- `src/III-Drone-Runtime` already places the runtime API onboard, separates it
  from the ground-control computer, enforces real-profile identity/secrets, and
  exposes runtime and configuration operations used by GUI v2.
- Supervision currently accepts `opti_track`, but treats every non-`sim` profile
  through the real-entity branch and filters entities by profile membership. There
  is no dedicated OptiTrack setup script, configuration profile/default, PX4
  manifest, mocap service/readiness contract, target descriptor, or deployment
  flow. The current name is therefore a partial profile placeholder, not an
  operationally commissioned integration.
- `src/III-Drone-GC` currently defines a ROS-free React frontend and thin FastAPI
  discovery/proxy. Production operation is Docker-Compose-based, configured by a
  manually copied `~/.config/iii-ground-control.env`, and launched through the
  workspace `iii_ground_control.sh` helper rather than a converged host installer
  or transactional GC application manager.
- Current GC discovery uses mDNS/manual runtime endpoints and intentionally keeps
  ROS, DDS, MAVSDK, daemon sockets, and runtime-host systemd access off the GC.
  QGroundControl configuration, III CLI/operator setup, cross-build tooling,
  signing keys, offline caches, clock synchronization, and tuning capture are not
  yet one provisioned GC-computer baseline.
- Simulation tooling currently launches `/home/iii/QGroundControl.AppImage` from
  inside the devcontainer/tmux simulation session and carries container-specific
  QGroundControl paths and settings. This must move to the host-native GC installer
  and launcher used by field operation; the devcontainer should launch only the
  simulator/backend and communicate with host QGroundControl over explicit LAN/
  host networking.
- PX4 configuration is not currently represented by a canonical real-FMU
  parameter manifest in this workspace. Simulation defaults are embedded as
  `param set-default` statements in the custom airframe under the editable
  simulation submodule. The PX4 source tree contains unrelated example parameter
  files but is not an operational parameter inventory.
- `tools/QGroundControl.org/QGroundControl.ini` is a raw mutable user/machine
  snapshot, not a safe release baseline: it contains `/home/iii`, window geometry,
  map position, and public automatic telemetry-upload preferences. Its ParamCache
  is generated/version-coupled data. Release ownership needs a sanitized managed-
  key manifest plus explicit preservation of non-managed user state.
- Behavior trees and mission specifications currently live as mutable source-tree
  files in `III-Drone-Mission`, selected through `MISSION_SPECIFICATION_DIR` and
  `BEHAVIOR_TREES_DIR` environment-expanded paths. The package install rules do
  not install these asset directories. The directory mixes the default inspection
  mission, alternate/test/legacy trees, FTP/up-down specifications, and an
  OptiTrack-specific test specification without a release catalog, content IDs,
  dependency closure, compatibility metadata, or qualification classification.
- Mission tooling currently has three related representations that can drift:
  mission YAML uses `$BEHAVIOR_TREES_DIR` file paths, behavior-tree XML names node
  IDs/subtrees, and `models.xml` plus `Project.btproj` manually describe subsets of
  custom nodes for the Groot editor. Repository search finds no runtime/build
  consumer of `models.xml`; actual runtime builders/ports are registered separately
  in `TreeExecutor::registerNodes()`. The mission executor can already export a
  BehaviorTree.CPP `TreeNodesModel` from its live factory, but this is exposed as a
  runtime file-writing service rather than a deterministic build artifact. There
  is no build-time proof that registered builders/ports and every catalog tree agree.
- The active mission executor exposes a live override service that accepts an
  arbitrary mission-specification file path. It rejects changes while a mission is
  active, constructs and validates a replacement, rebuilds the executor, and tries
  to restore the previous specification if rebuilding fails. It does not enforce
  catalog identity/profile allowlists or the full landed/disarmed maintenance-safe
  gate, and selection is reported as a mutable filesystem path.
- `III-Drone-Configuration` already uses `ament_cmake_python` and installs its
  complete immutable `config/` tree under the package share directory. Its newer
  Python seeding code can resolve that installed copy, but deliberately prefers a
  workspace source tree when present and overwrites installed schema/manifest
  files into writable runtime state on every seed. A parallel legacy
  `scripts/install.sh` plus `update_installed_parameters.py` path mutates files
  non-transactionally, invokes an unquoted shell copy, and has no shadow-store,
  reintroduction-review, release identity, or power-loss transaction semantics.
  Static package installation and live-state reconciliation therefore need one
  package-native source of truth with a strict immutable-source/writable-state
  boundary.
- Development already has a living, profile-separated runtime configuration tree:
  `setup/paths.bash` points `CONFIG_BASE_DIR` at workspace-local `.config`, making
  sim sets live under `.config/iii_drone/parameter_sets/sim/` alongside a separate
  `real/` tree. Supervision seeds and selects from this tree before launch, and the
  configuration server/GUI reads, updates, and snapshots the selected profile there.
  The redesign preserves this runtime tree while changing its seed source from the
  checkout-preferred package source to validated installed package data.
- The workspace dependency lock freezes top-level submodule revisions and is a
  required input to every qualified or field-development release identity.
- GitHub currently enforces an active ruleset only on the workspace `main`
  branch. It requires a pull request and resolved review threads but requires
  zero approvals and no status checks. Workspace `develop` is unprotected.
- The ten editable III submodule repositories currently have no GitHub rulesets
  or classic branch protection on `main` or `develop`.
- `.github/workflows/dependency-governance.yml` implements useful lock, III
  branch-stack, exact target-head, and linked-submodule-PR checks, but those
  checks are not currently required by a ruleset. It references `staging`,
  which does not exist, and has no `release` behavior.
- `scripts/git/create_develop_to_main_prs.sh` deliberately uses coordinated
  temporary promotion branches. After submodule PRs merge into `main`, the
  workspace promotion branch must refresh gitlinks to the resulting submodule
  `main` merge commits before the exact-target-head gate can pass. A literal
  workspace PR whose head remains `develop` cannot represent those refreshed
  pointers without adding commits to `develop` or weakening that gate.
- Existing promotion automation is split across stateful local shell helpers
  and a manually dispatched, write-capable pointer-refresh workflow. The local
  helpers have useful dry-run defaults, but the combined workflow has no single
  structured plan/result schema, persisted operation identity, resume contract,
  or machine-readable recovery instructions.
- Hardware mapping is inconsistent across generations. The retired deployment
  udev file binds an mmWave device with serial `00DEEC69` and a generic charger,
  while `src/III-Drone-Core/udev/99-diii-usb.rules` binds mmWave serial
  `00E241A0`, exact FMU/charger serials, and no stable cable-camera role despite
  legacy runtime use of `/dev/video0`. These literals must not silently become
  the new shared hardware-class contract.
- `.github/workflows/dependency-governance.yml` mixes independent policy checks
  and PR-comment side effects in one workflow and currently has no qualified
  build, release evidence, immutable artifact publication, deployment handoff,
  or reusable local/CI command boundary.
- Repository documentation currently contradicts the settled governance and
  deployment direction in operationally significant places. Root `README.md`,
  `docs/dependency-governance.md`, `docs/repo-boundary-map.md`, workflow triggers,
  and Git helpers still describe `staging` or temporary
  `release/develop-to-main-*` branches. GC and testing docs still prescribe Docker
  Compose, simulation docs launch QGroundControl from a container-owned path, and
  `setup/remote.bash` still points directly at the retired deployment repository.
- Root `AGENTS.md` correctly protects strict submodule governance but its editable
  III-repository list predates `src/III-Drone-Contracts` and
  `src/III-Drone-Runtime`. Documentation migration must derive and validate this
  inventory from governed repository policy rather than maintain another drifting
  hand-written list.
- Maintained documentation is distributed across root/workspace docs and editable
  III submodules alongside large generated and vendored documentation trees. The
  migration must classify ownership first and exclude generated, vendored, third-
  party, archived-evidence, build, install, log, dataset, and artifact content; a
  blind repository-wide rewrite would be unsafe and infeasible.

### Settled onboard layout

```text
/opt/iii/releases/<release-id>/    immutable releases
/opt/iii/current                   active release selector
/opt/iii/receiver/                 receiver A/B slots and recovery bootstrap
/etc/iii/                          host identity, environment, secrets
/var/lib/iii/                      configuration, calibration, snapshots, state
/var/log/iii/                      retained runtime and deployment logs
/run/iii/                          sockets, locks, transient state
```

### Settled workspace implementation boundary

The implementation starts as one ROS-independent Python distribution rooted at
`deployment/`, with distribution name `iii-deployment` and import package
`iii_deployment`, plus declarative assets. It
is installed on supported operator hosts and installs only the receiver-side subset
on the Pi. This gives the CLI, Ansible filters/modules, CI, and receiver one tested
contract library without placing deployment logic in a ROS package or copying it
between shell scripts. The intended ownership is:

```text
deployment/
  pyproject.toml                 package, pinned host dependencies, entry points
  src/iii_deployment/            manifests, policy, archive, transport, receiver
  schemas/                       versioned JSON Schemas and compatibility policy
  profiles/                      target, runtime-profile, hardware, impact policy
  builder/                       pinned ARM64 builder definitions and cache policy
  packaging/                     deterministic bundle and install-layout rules
  ansible/                       aircraft and GC inventories, roles, playbooks
  image/                         Ubuntu image metadata and cloud-init templates
  systemd/                       production receiver/daemon/API unit templates
  qgc/ and px4/                  managed integration manifests, not mutable caches
  tests/                         host-independent contract/integration fixtures
```

`tools/III-Drone-CLI` remains the user-facing command parser and thin orchestration
client; it consumes the installed `iii_deployment` API and schemas rather than
reimplementing policy. Editable domain repositories continue to own their runtime
code and source artifacts: Configuration owns III parameter manifests/defaults,
Mission owns CMake-registered mission/tree assets, Runtime owns the LAN runtime API,
Supervision owns the ROS process graph, and GC owns frontend/proxy application code.
`.github/workflows/` invokes the same package APIs. Top-level `scripts/` may retain
thin compatibility launchers during migration but cannot become a second policy
implementation. Keep one source distribution and one contract/schema implementation.
Privilege and host-role boundaries use separate entry points, system packages,
service users, and narrowly selected installed assets; they do not authorize a
second policy library or divergent receiver/CLI schema implementation.

### Feasibility and integrity sweep

The final design was rechecked against the repository and external platform
contracts on 2026-08-25:

- ROS REP-2000 lists Jazzy (supported through May 2029) on Ubuntu Noble 24.04
  `arm64` as Tier 1 with Debian packages, archives, and source support. Canonical's
  Raspberry Pi support documentation still publishes Ubuntu 24.04 preinstalled
  server ARM64 Raspberry Pi images and Raspberry Pi 5 guidance. The native Pi 5 +
  Ubuntu 24.04 + Jazzy baseline is therefore supported; the implementation must pin
  one exact still-published image/checksum rather than follow the Ubuntu download
  page's moving “latest” link.
- Live GitHub inspection confirms workspace branches `develop` and `main`, no
  `release` branch, one active `main` ruleset, and only the two currently checked-in
  workflows. All ten governed editable III repositories have `develop` and `main`
  branches and no active rulesets. P0's governance work is required and its current-
  state assumptions are not speculative.
- The legacy deployment repository is present on branch `v2.2-staging` and contains
  only the Compose file, setup/install scripts, README, and udev rules already
  inventoried here. No undiscovered database, package registry, image source, or
  fleet inventory needs migration. Preserve its Git history and exact final commit.
- A real AArch64 cross-toolchain against a pinned Noble/Jazzy sysroot is feasible on
  supported x86_64 hosts. QEMU remains a bounded fallback for target-native build
  steps, not the normal compiler and never runs onboard. Native ARM64 CI is an
  optimization, not a different target contract.
- Stock Raspberry Pi images plus cloud-init and Ansible can establish the baseline
  without a custom image factory. Physical SD readback, EEPROM/boot behavior, USB
  role matching, real driver closure, field-WLAN transfer budget, and power-loss
  behavior cannot be proven from GitHub CI; they are explicit commissioning/matrix
  evidence, not hidden assumptions.
- GitHub-hosted CI cannot run the authoritative III Gazebo/physical acceptance
  environment. Q118–Q122's signed local-evidence handoff is the feasible merge gate:
  CI verifies source identity, policy, signature, and result contracts while local
  pinned tooling owns simulation/bench/flight execution.
- The scope is large but deliverable as six ordered phases with one shared contract
  package, explicit phase DAGs, and failure-injection acceptance. No task requires
  future HIL, fleet identity, TLS, per-aircraft state, onboard compilation, onboard
  Docker, or source-independent field operation to complete this sweep.

Authoritative platform references:

- <https://www.ros.org/reps/rep-2000.html#jazzy-jalisco-may-2024-may-2029>
- <https://canonical-ubuntu-hardware-support.readthedocs-hosted.com/boards/how-to/ubuntu_supported/raspberry-pi/>

Architecture choices formerly tracked here are resolved in the numbered decision
log. Physical Raspberry Pi driver/role validation remains commissioning evidence
to collect, not an unresolved architecture choice.

### Grilling decision log

- **Q1 — Initial aircraft hardware class:** Settled. Raspberry Pi 5 Model B
  booting from an SD card. RAM is intentionally not inventoried or used as a
  compatibility discriminator.
- **Q2 — Target OS and ROS baseline:** Settled. Ubuntu Server 24.04 LTS ARM64
  with ROS 2 Jazzy. Remaining work must validate attached hardware drivers and
  remove Humble assumptions.
- **Q3 — Onboard Docker role:** Settled. Do not install or use Docker onboard.
  Run the real runtime natively under host systemd and deploy immutable ARM64
  install bundles. Docker may remain an offboard development/builder
  implementation only.
- **Q4 — Raw SD-card provisioning:** Settled. Use a checksum-pinned official
  Ubuntu Server 24.04 ARM64 Raspberry Pi image, generated cloud-init first-boot
  data, and Ansible convergence. Do not maintain a custom III disk image yet.
- **Q5 — Offline operation:** Settled. Field deployment, rollback, status,
  logs, tuning capture, and repeated convergence must work offline over a local
  laptop-aircraft connection. Initial cache population and fresh SD provisioning
  may depend on internet access for now.
- **Q6 — Aircraft inventory scope:** Settled. Model one shared logical aircraft
  target. Do not distinguish physical aircraft in deployment inventory; all use
  the same setup, runtime identity, credentials, and configuration, with at most
  one connected at a time.
- **Q7 — SSH host trust:** Settled. Assume the sole `iii.local` host on the
  local network is correct. Do not inventory or authenticate individual Pi host
  keys. Record local endpoint spoofing/MITM as an accepted initial risk.
- **Q8 — SSH client authentication:** Settled. Use key-only SSH with a distinct
  keypair per authorized computer. Provision the first public key via cloud-init
  and provide authenticated add/revoke/list workflows for later workstation or
  ground-control keys. Disable password login and never transfer private keys.
- **Q9 — Deployment and human maintenance privilege:** Settled. Keep the
  `iii-deploy` deployment identity forced-command-only, unprivileged, and without
  sudo. Use the key-only `iii` account for attended development and field
  research with an interactive shell and explicit `NOPASSWD: ALL` authority.
  Give it an independently generated owner-controlled Ed25519 key, record that
  public-key identity in provisioning evidence, and never select it as the
  Ansible or receiver transport. Disable password/root login, SSH forwarding,
  agent forwarding, X11, and tunnels; permit PTY; limit port 22 to the operator
  CIDR. Remove the one-time `iii-bootstrap` account after convergence without
  removing or weakening either permanent identity.
- **Q10 — Release signing:** Settled. Require cryptographic signatures and
  content checksums for every qualified and dirty field-development bundle.
  Verify both before privileged staging and reject unsigned, unknown, or
  tampered input.
- **Q11 — Signing-key ownership:** Settled. Use one independently revocable
  signing keypair per build computer. Provision public signer trust through the
  deployment helper and never copy private signing keys. A ground-control
  computer needs signing authority only if it builds or deploys bundles.
- **Q12 — Deployment safety gate:** Settled. Normal activation requires fresh
  landed/disarmed PX4 state and no Mission Execution, Custom Operation, or
  active Reference Owner. Broken-runtime recovery uses an interactive,
  audit-logged maintenance override that stops III units and requires physical
  safety confirmation; unattended override is prohibited by default.
- **Q13 — Onboard release retention:** Settled. Always retain the latest
  qualified/full release as a protected recovery anchor. During field work also
  retain the active and immediately previous field-development releases, with
  one additional temporary candidate while staging. Experimental deployment
  cannot replace or garbage-collect the qualified anchor.
- **Q14 — Qualification rule:** Settled. Only the future release branch can
  produce a qualified/full release. It must be clean and committed with clean
  governed submodules, pass dependency-lock verification and all required
  ARM64 build/tests, carry an explicit version tag and signed evidence manifest,
  and be deployed through an explicit qualified action. Every other branch and
  every dirty state remains a field-development release.
- **Q15 — Release branch governance:** Settled. Use the protected promotion
  chain feature/work-sweep branch -> `develop` -> verified mechanical
  `promote/develop-to-main/*` branch -> `main` -> workspace-only `release` ->
  immutable `vX.Y.Z` tag. The mechanical promotion branch may contain only the
  expected develop-derived state and post-submodule-merge gitlink/lock refresh.
  Individual III submodule repositories stop at protected `main`; they do not
  gain `release` branches. Qualified builds require a tag whose commit belongs
  to workspace `release`.
- **Q16 — Human review requirements:** Settled. This is a solo-maintainer
  project. Require PRs, resolved conversations, and all applicable machine
  checks, but require zero human approvals on `develop`, `main`, and `release`.
  Record explicit promotion intent and evidence, permit AI-authored PRs, and do
  not rely on a second reviewer or undocumented bypass.
- **Q17 — Qualified artifact producer:** Settled. CI is the sole producer and
  publisher of qualified release bundles. A `vX.Y.Z` tag on workspace `release`
  triggers an independent ARM64 rebuild, required tests, evidence assembly,
  signing, and immutable publication. Workstations retrieve and deploy that
  artifact through the III CLI. Local builders may produce signed field-
  development releases from any branch or dirty state, but can never classify
  them as qualified. CI availability is required to create a new qualified
  release; previously cached qualified releases remain deployable offline.
- **Q18 — Qualified artifact publication:** Settled. Publish each qualified
  `vX.Y.Z` bundle, signed manifest/checksums, qualification evidence, compatibility
  metadata, and release notes as immutable assets on a GitHub Release in this
  workspace repository. GitHub Actions artifacts are temporary pipeline
  intermediates, not the authoritative registry. `iii release fetch` downloads,
  verifies, and caches a selected version for later offline deployment. Field-
  development bundles remain local unless an operator explicitly exports them;
  they are not published as GitHub Releases by default.
- **Q19 — Qualified-release signing mechanism:** Settled. Use a dedicated
  Ed25519 CI signing key stored as a protected GitHub Actions environment secret
  and exposed only to the qualified-release signing job for protected
  `vX.Y.Z` tags on workspace `release`. Provision its public key through the
  target signer-trust workflow and support independent rotation/revocation.
  Workstation field-release keys remain separate. GitHub artifact attestations
  may supplement provenance but are not the onboard trust root because bundle
  verification must work offline.
- **Q20 — PX4 firmware ownership:** Settled. PX4 remains independently
  commissioned flight-controller firmware. III release manifests declare the
  supported PX4 version/commit range, required message/interface compatibility,
  and relevant airframe/parameter assumptions. Activation performs a read-only
  compatibility check and fails closed on mismatch. The III CLI may inspect and
  report PX4 state and may perform the separately confirmed, backup-first
  parameter workflow in Q65, but firmware flashing remains a separate explicit
  maintenance workflow until flight-controller recovery and rollback are designed.
- **Q21 — Host OS update policy:** Settled. Use controlled hybrid maintenance.
  Disable unattended upgrades that could alter runtime behavior. Apply security,
  kernel, ROS, and system package updates only through an explicit III CLI/
  Ansible host-maintenance workflow that records package changes and reboot
  requirements and validates the protected qualified recovery release
  afterward. Normal III release deployment never changes host packages. Use a
  fresh SD-card reprovision for major Ubuntu/ROS baseline changes or substantial
  host drift. Defer A/B root-filesystem updates.
- **Q22 — Power-loss transaction policy:** Settled. Use a durable deployment
  transaction journal and fail back to the last accepted release/configuration
  pair unless acceptance was durably committed. Interrupted transfer/staging is
  resumable or safely discarded without touching active state. Interrupted
  migration restores its pre-migration checkpoint. Power loss after selector
  switch but before health acceptance boots the previous accepted pair; power
  loss after durable acceptance boots the new pair. Boot reconciliation never
  resumes an incomplete activation or autonomously starts operation. Preserve
  the failed candidate manifest and diagnostics for explicit inspection/retry.
- **Q23 — Tuning-journal durability:** Settled. A GUI parameter transaction is
  acknowledged only after its requested values, previous values, release/schema,
  session, timestamp, and result are durably journaled and the active parameter
  set is atomically updated. Multi-parameter requests are atomic. Persistence
  failure is reported as failure, not apparent GUI success. Older history may
  be compacted into checkpoints to bound SD-card growth, but retain the complete
  current field-test session.
- **Q24 — Removed/legacy parameter storage:** Settled. Keep every active onboard
  parameter-set file aligned with the current release manifest. When a key is
  removed from the manifest, atomically move its last active value and provenance
  into a persistent onboard legacy shadow store rather than retaining it in the
  active file or discarding it. Captures include both active and shadow state;
  current-schema source promotion excludes shadow keys while preserving them in
  capture evidence. Compatible rollback can rehydrate required old keys.
- **Q25 — Reintroduced parameter behavior:** Settled. Never restore a legacy
  value automatically. If a current manifest reintroduces a retired canonical
  key, reconciliation stops before active-state mutation and emits a blocking
  warning plus a review file. For each key the review shows the canonical old
  value captured from the selected active parameter file at retirement, the new
  release default, type/constraint validation, and an explicit unresolved
  decision. The operator must mark `use_old` or `use_new_default` for every key.
  If the old value is invalid under the new manifest it remains visible but only
  `use_new_default` is permitted. Values found in arbitrary inactive, historical,
  snapshot, or scattered files are never candidates for restoration.
- **Q26 — Reintroduction review location:** Settled. `iii deploy plan` writes a
  local review under the repository-local, Git-ignored
  `.iii/operations/<operation-id>/` directory. Immutable fields bind it to the
  target release/manifest, onboard pre-reconciliation configuration hash,
  canonical active-at-retirement record, selected parameter set, and operation
  ID; only explicit per-key decisions are editable. `iii deploy continue`
  validates the resolved file before mutation. Stale/mismatched reviews fail
  closed. Retain immutable copies in the local operation record and onboard
  deployment audit, but never track review/operation files in Git.
- **Q27 — Parameter-promotion Git workflow:** Settled. Provide explicit staged
  operations: `iii config pull` captures without Git mutation; `iii config
  promote` plans and shows a diff by default; `--apply` writes validated source
  files and creates focused `III-Drone-Configuration` plus workspace-gitlink
  commits on the current feature branch; `--pr` creates or updates the coordinated
  PR stack into `develop`. Never merge automatically or write protected branches.
  Reject overlap with dirty configuration paths while permitting unrelated dirty
  workspace state only when it can be safely isolated and left untouched.
- **Q28 — Tracked parameter-set identity:** Settled for deployment scope. Maintain
  exactly one release-selected default for each profile: `real` and `sim`. Git
  commits version each default, and a qualified release tag fixes the exact
  default files packaged by that release. Packaged defaults seed missing
  configuration and supply defaults for newly introduced keys but never overwrite
  existing onboard current-schema values or change the selected onboard state.
  The repository must eventually support multiple tracked non-default parameter
  versions, but their catalog, naming, selection, and release semantics are
  explicitly deferred outside this deployment redesign. Do not make the capture
  format or promotion interfaces depend on there being only one tracked set.
- **Q29 — Parameter promotion granularity:** Settled. Capture the complete
  parameter state, compare it with the current corresponding `real` or `sim`
  tracked default, and generate a per-key promotion review. Only explicitly
  accepted differences update source; rejected/unselected values remain only in
  immutable capture evidence. An explicit accept-all-valid action may exist but
  is never the default. Promotion remains bound to the capture, manifest, source
  baseline, profile, and generated diff so stale reviews fail closed.
- **Q30 — Field activation downtime:** Settled. Build, transfer, verification,
  and staging occur while the current runtime remains available. Stop runtime
  only after candidate and configuration plan are ready. Target candidate health
  acceptance within 60 seconds; after a hard 120-second deadline, initiate
  automatic rollback onboard. Target restoration of the previous accepted pair
  within another 60 seconds. Record actual stop/start/health/rollback timings so
  budgets can be tightened from evidence.
- **Q31 — Deployment receiver command transport:** Settled. Expose no receiver
  TCP listener. The CLI transfers bundles over SFTP/SSH into an unprivileged
  incoming area and invokes a fixed `iii-deploymentctl` command over key-
  authenticated SSH. That client submits structured requests through a
  permission-controlled Unix-domain socket. The receiver verifies authorization,
  signatures, operation hashes, paths, compatibility, and safety state before
  privileged mutation. Stable operation IDs support disconnect and later status/
  log reattachment.
- **Q32 — Receiver self-update recovery:** Settled. Use versioned A/B receiver
  slots plus a minimal stable host recovery bootstrap installed by Ansible.
  The current receiver verifies and stages a bundled receiver update into the
  inactive slot before application activation. The bootstrap switches the
  selector and requires the new receiver to start, pass self-tests, reopen its
  Unix socket, understand/resume the durable journal, and report ready within a
  bounded deadline. Failure restores the previous receiver and aborts application
  deployment. Always retain the previous working receiver. Update the stable
  bootstrap, systemd unit, trust root, and recovery selector only through explicit
  Ansible/host maintenance, never ordinary application deployment.
- **Q33 — Onboard storage reserve:** Settled. Before transfer/staging, calculate
  projected peak use for the incoming compressed bundle, extracted candidate,
  optional receiver slot, configuration/shadow checkpoints, transaction and
  diagnostic allowance, and every protected retained application/receiver
  version. After projected peak, require free capacity of at least the greater
  of 2 GiB or 10% of the deployment filesystem. Garbage-collect only eligible
  artifacts; never remove active/previous/protected-qualified releases or active/
  fallback receivers. Fail before runtime mutation with an occupancy report if
  the reserve cannot be met.
- **Q34 — Physical device role matching:** Settled. Define one committed,
  ambiguity-aware shared USB hardware-role manifest for mmWave CLI/data,
  charger/gripper, and cable camera, while PX4 remains an independent fail-closed
  Ethernet transport and telemetry gate. Prefer vendor/product/interface and stable
  device properties; add exact serial allowlists only where needed to distinguish
  ambiguous or safety-critical devices. Generate stable `/dev/iii/*` role paths
  and provide `iii host inspect`. Missing or ambiguous required roles block real-
  runtime health acceptance. Do not create per-aircraft inventory. Resolve the
  conflicting legacy/current serial literals through physical commissioning.
- **Q35 — Raspberry Pi boot firmware policy:** Settled. Maintain one source-
  controlled Raspberry Pi 5 boot profile under `deployment/`, based on stock
  Ubuntu defaults with only documented required overlays/options and no initial
  overclocking. Inventory firmware, kernel, command line, overlays, and boot
  configuration. Normal III releases cannot modify boot files; only explicit
  Ansible/host maintenance may do so, with pre-change backups and reboot
  validation. Retain previous boot config/kernel where supported. Defer A/B
  boot/root filesystems and accept physical SD repair/reprovisioning for an
  unbootable host.
- **Q36 — Network bootstrap and field connectivity:** Settled. Always support
  Ethernet DHCP as the physical recovery path. SD preparation generates cloud-
  init from untracked secret input containing one or more Wi-Fi profiles; store
  resulting credentials only in root-readable host configuration. Advertise
  `iii.local` with mDNS on the active interface. Provide transactional III CLI
  plan/apply operations for network profile changes; if the CLI cannot reconnect
  and confirm before an onboard deadline, restore the prior network config. Do
  not initially host an aircraft access point; use a router, laptop hotspot, or
  Ethernet. Never place network credentials in Git, bundles, logs, or reviews.
- **Q37 — Runtime API network exposure:** Settled. Keep `iii-runtime-api`
  directly reachable on the operator LAN for GUI and remote CLI use. Do not make
  SSH tunneling the normal runtime-operation path. Deployment control remains
  separate over SSH plus receiver Unix-socket IPC.
- **Q38 — Runtime API TLS trust:** Settled for the initial scope. Serve plain
  HTTP/WS on the trusted operator LAN and defer TLS/HTTPS/WSS and private-CA
  lifecycle work. This explicitly accepts that a party with local-network access
  may observe or alter runtime API credentials, telemetry, and commands. Keep
  the deployment receiver and all privileged deployment mutations on SSH; do
  not let plaintext runtime API authentication authorize deployment operations.
- **Q39 — Onboard log retention:** Settled. Segment logs by boot/runtime session
  because the aircraft is power-cycled and continuous operation is not assumed.
  Retain runtime/host logs for up to 14 days while always preserving the current
  and a bounded set of newest sessions, capped at the lesser of 1 GiB or 5% of
  the filesystem. Retain the latest 50 deployment audits plus every record
  referenced by retained releases. Protect failed activation/rollback diagnostics
  until pulled/acknowledged or 30 days. Treat current tuning journals and required
  configuration/shadow checkpoints as persistent state, not ordinary logs.
  Rosbags/datasets have separate operator-managed retention. Idle healthy runtime
  must log state transitions rather than repetitive polling/no-op messages;
  verbose/debug logging is explicitly enabled and bounded per session.
- **Q40 — Pulled-log pruning trigger:** Settled. Pull never deletes by default.
  After every local file is hash-verified, record exact content receipts onboard.
  `iii logs prune --pulled` previews only receipt-backed, non-protected data and
  requires explicit apply; `iii logs pull --prune` may provide an equally explicit
  convenience path. Interrupted/unverified pulls create no receipts. Pruning
  cannot remove current-session logs, active transaction diagnostics, protected
  release evidence, configuration/tuning state, or other protected records.
- **Q41 — Normal power-on runtime state:** Settled. On host boot, start the
  deployment receiver first and reconcile interrupted transactions. Then start
  the minimal `iii-runtime-api`/III-daemon control plane in `DEGRADED_CLOCK` until
  the post-boot clock gate in Q59 succeeds; do not boot the canonical real-profile
  ROS graph or permit runtime operations while that gate is closed. After clock
  synchronization and buffered-log flush, automatically boot the real graph into
  operational standby with required sensors/health monitoring active. Never
  automatically start Mission Execution, Custom Operation, Direct Operation, or
  a Reference Owner; never arm PX4 or initiate movement. Configuration/hardware/
  release health failure leaves a diagnosable non-operational state. Keep
  `iii system boot` idempotent as a manual recovery/bringup command rather than
  requiring it after every normal power cycle.
- **Q42 — Future `boot` versus `start` semantics:** Settled. Preserve the current
  safe split. `iii system boot` ensures the daemon, loads the selected canonical
  profile, instantiates process topology/supervision, prepares daemon-owned
  services, and creates the operator view without configuring/activating managed
  nodes. `iii system start` starts required services and configures/activates
  managed nodes. Normal host startup may invoke both after receiver reconciliation,
  but manual recovery can stop after non-activating `boot`.
- **Q43 — Candidate health-acceptance gate:** Settled. Before durable acceptance,
  require healthy receiver/bootstrap; daemon and runtime API responses with the
  expected release/profile identity; durable schema-valid configuration
  reconciliation; all required hardware roles present/unambiguous; every required
  daemon service alive/ready; every required managed ROS node at its declared
  operational lifecycle state; compatible PX4 interface with fresh landed/
  disarmed state; and no Mission Execution, Custom Operation, Direct Operation,
  or Reference Owner. Conditions must remain stable for 10 continuous seconds
  within the 120-second activation deadline. Only profile-declared optional
  entities may be absent. Persist the acceptance snapshot before committing the
  selector; any failure/timeout triggers onboard rollback.
- **Q44 — One-command field iteration:** Settled. Provide `iii deploy field` as
  a complete plan and `iii deploy field --apply` as the explicit mutation. It
  snapshots the current clean/dirty workspace including governed submodules and
  relevant untracked source, infers GC/drone impact, builds only the dependency-
  complete component set, packages/signs paired field-development artifacts when
  needed, updates/health-checks GC first, transfers/stages the ARM64 bundle, pauses
  for required parameter review, requests onboard activation, and reattaches by
  operation ID for acceptance/rollback diagnostics. Keep every phase separately
  callable. Never commit, push, or modify the developer working tree.
- **Q45 — Canonical offboard ARM64 build strategy:** Settled. Use a digest-
  pinned offboard container with a reproducible Ubuntu 24.04/ROS Jazzy target
  sysroot and real AArch64 cross-toolchain for normal C/C++ packages. Install
  architecture-independent Python/assets into the target layout and use QEMU
  only for unavoidable target-native configure, code-generation, or validation
  steps. Prefer pinned prebuilt third-party target dependencies; never derive the
  sysroot from an aircraft. Laptop and CI builders share the same recipe/manifest
  contract, with native ARM64 CI allowed to omit the crossing layer. Cache
  toolchain, sysroot, dependencies, and colcon outputs for offline field use.
- **Q46 — Dirty source snapshot boundary:** Settled. Version the policy in
  `deployment/source-snapshot.yaml`. Include working-tree content for required
  tracked files, tracked deletions, governed-submodule state, and non-ignored
  untracked files under declared III source/build-input roots. Exclude `.git`,
  `.iii`, build/install/log trees, caches, datasets, rosbags, editor state,
  credentials, private keys, and generated artifacts. Ignored build inputs need
  an explicit reviewed allowlist. Reject escaping symlinks, special files,
  unexpected large files, suspected secrets, and ambiguous dependencies. Show
  complete include/exclude provenance before building and never mutate the tree.
- **Q47 — Workstation signing-key storage:** Settled. Generate one Ed25519 key
  per build computer outside the repository with owner-only permissions and
  encrypted-at-rest storage. Unlock through the OS keyring or interactive
  passphrase into a short-lived host signing agent with configurable field-
  session timeout. Builder containers receive only unsigned bundle hashes; the
  agent signs schema-valid III manifests, not arbitrary data. Do not share/back
  up keys between computers. Replace a lost key by authorizing a new machine-
  specific signer over SSH and revoking the lost signer. CI retains its separate
  protected qualified-release key.
- **Q48 — Qualified version and tag publication:** Settled. Use final strict
  SemVer tags `vMAJOR.MINOR.PATCH`; initially test release candidates as field-
  development bundles rather than qualified prerelease tags. `iii release
  prepare` validates progression and creates/updates the workspace-only
  `main -> release` PR with generated notes. After merge, `iii release publish`
  plans by default and `--apply` requires the exact clean `origin/release` head,
  verified lock/governance/checks, and a nonexistent version/tag before creating
  the protected immutable tag. The tag triggers qualified CI. Qualification
  failure publishes nothing and requires explicit investigation; never reuse a
  failed version silently.
- **Q49 — Receiver/application rollback coupling:** Settled. Keep a successfully
  updated receiver active when only the application candidate fails. Before a
  receiver switch, prove it can manage every retained application release, read
  existing journals/audits, activate/rollback retained manifest formats, restore
  compatible configuration checkpoints, and communicate with installed bootstrap/
  CLI protocols. Reject incompatible receiver updates before switching. Restore
  the prior receiver only for receiver startup, self-test, journal, socket, or
  compatibility failure—not merely application rollback.
- **Q50 — Supported operator-computer platform:** Settled. Initially support
  Ubuntu 22.04 and 24.04 x86_64 workstation/ground-control computers. Containerize
  and pin the ARM64 builder; install CLI/Ansible dependencies through a repository-
  managed environment rather than global packages. Linux block-device access is
  required for SD preparation. Keep signing keys and `.iii` operation/cache state
  in host user storage outside containers. Treat macOS, Windows/WSL, and ARM64
  operator computers as unsupported initially.
- **Q51 — Field bundle transfer strategy:** Settled. Initially transfer a complete
  compressed immutable bundle, not source synchronization or binary deltas. Use
  resumable SFTP to a release-ID-specific unprivileged `.partial` path, resuming
  only when local/remote identity and size agree. Preserve partial uploads across
  temporary network loss with bounded-age cleanup. The receiver trusts/moves
  nothing until full signature/checksum verification. Target complete transfer
  within 120 seconds on representative field WLAN and benchmark commissioning
  bundles. Add content-addressed chunk reuse only if measurements repeatedly
  miss the target without changing signed manifests/final release semantics.
- **Q52 — Live tuning capture and handoff:** Settled. The original close-time
  pull versus manual-pull choice was rejected as insufficiently live. Current
  behavior has been inspected: accepted edits persist immediately onboard, but
  there is no ground-control mirror, session journal, functional GUI download,
  or reliable live GUI refresh. Recommended implementation:
  1. Build one profile-parameterized tuning subsystem and protocol used unchanged
     by `sim` and `real`; profile, target kind, release/workspace identity, and
     manifest hash are metadata, not separate implementations.
  2. Make the target runtime the canonical source of truth. Internally open or
     resume a capture session on the first accepted Apply, with a UUID, immutable
     baseline, monotonic revision, manifest/release identity, operator identity,
     and timestamps. Reconnection resumes that internal session instead of
     creating an implicit new baseline.
  3. Replace sequential Apply with one idempotent transaction carrying session
     ID, request ID, expected revision, and all edits. Validate the complete set
     first; durably append intent; apply and read back all nodes; durably commit
     the active config and journal before ACK. Compensate all prior node changes
     on failure. If compensation/readback fails, enter an explicit configuration-
     divergent fault instead of reporting success or pretending full atomicity.
  4. Use atomic replace plus file and parent-directory sync for checkpoints and
     selectors, and a checksummed append-only journal/WAL for power-loss recovery.
     Record accepted and rejected requests, old/new values, validation, restart
     semantics, readback, and outcome. Derive rolling snapshots from committed
     revisions rather than process-memory cleanup state.
  5. Publish every committed configuration revision over the existing runtime
     event stream. The GUI merges authoritative patches, detects revision gaps,
     rehydrates automatically, and shows current, edited/unsaved, persisted, and
     pending-next-cold-restart values without stale success-only notifications.
  6. Add a host-side III CLI tuning-mirror companion, automatically started by
     the GUI/field and simulation workflows. It subscribes to the same event and
     backfill API, verifies sequence and hashes, and incrementally checkpoints
     the session under Git-ignored `.iii/operations/<session-id>/`. The browser
     never owns filesystem persistence. The GUI displays the mirror's last
     acknowledged revision.
  7. Keep onboard/simulation-target persistence authoritative when the mirror is
     absent: show a prominent degraded warning but allow necessary tuning, retain
     the complete journal, and automatically backfill all missing revisions on
     reconnection. Never silently claim a revision is mirrored.
  8. Sealing through the CLI/capture workflow creates an immutable hash-addressed
     capture on the target and automatically syncs/verifies it locally when
     connected. Disconnect, GUI close, process restart, and target reboot leave
     the internal session resumable; an abandoned session can later be explicitly
     resumed or sealed. Keep explicit `iii config capture pull/verify` recovery
     operations.
  9. Replace the GUI's fake download with a real export of a sealed capture (or
     remove the button in favor of the local mirrored-capture action), and feed
     the same capture format into the reviewed `real`/`sim` default-promotion
     workflow already specified below.
  Adopt this target-canonical, live-resumable mirrored-session design, including
  allowing parameter application with a visible degraded warning during temporary
  mirror loss. Do not expose "GUI tuning", start-session, or end-session concepts
  in the operator UI. Preserve the current interaction model: edit values, show
  edited/unsaved state, Apply, and identify values pending application on the next
  cold restart. Session/capture machinery remains internal and CLI-facing.
- **Q53 — Meaning of cold restart for pending parameters:** Settled. A cold
  restart is cleanup/unconfiguration followed by fresh configuration and
  activation of the III runtime graph, including a fresh configuration-server
  lifecycle, while the system daemon remains booted. `iii system stop` followed
  by `iii system start` at the configuration lifecycle boundary performs it;
  `iii system restart --cold` is the direct shorthand. `iii system shutdown` and
  a subsequent `iii system boot`, an OS reboot, or a power cycle are not required.
  A warm restart or restart of an unrelated individual node does not apply pending
  values. The GUI remains an indicator/editor and does not add a configuration-
  specific restart control. It keeps pending values visible until the freshly
  configured runtime reports matching active readbacks, then clears them.
- **Q54 — Arbitrary named parameter-set capture:** Settled. A single end-of-test
  session capture is insufficient. During a
  sim or real test, the operator edits and applies values and may save any number
  of onboard named parameter sets such as A, B, and C. The continuous background
  journal preserves provenance but is not the operator-facing artifact. At the
  end, the operator selects any subset of saved sets and downloads each as an
  immutable local capture with a short operator-provided name and description,
  exact full values, profile, source snapshot/revision, manifest and release/
  workspace identity, pending-boot state, timestamps, and integrity hash. Sets
  remain independently downloadable later; downloading is non-destructive and
  does not change the active/default set. The same workflow and format apply to
  simulation and real targets. Later review compares any captured set with the
  corresponding tracked `sim` or `real` default and selectively promotes values.
- **Q55 — Onboard retention and deletion of named parameter sets:** Settled.
  Operator-named sets are durable and never automatically pruned
  by deployment, journal compaction, restart, or storage cleanup. System-generated
  checkpoints may be compacted only when no active/default/pending state, named
  set, or unexported capture references them. Deleting a named set is explicit,
  blocked while it is active/default/pending, and normally requires a verified
  local capture receipt; provide a separately confirmed force-delete path for a
  deliberately unwanted set.
- **Q56 — Portable capture storage between operator computers:** Settled. Store
  local captures content-addressed under Git-ignored
  `.iii/captures/<capture-id>/`, with mutable human-facing name/description kept
  separately from immutable source identity and payload. Provide `list`, `show`,
  `diff`, `export`, `import`, and `verify` CLI operations. Export a self-contained
  signed/checksummed archive suitable for USB or ordinary file transfer between
  the ground-control laptop and workstation; import verifies before accepting,
  deduplicates identical capture IDs, permits duplicate display names, and never
  mutates Git or parameter defaults. Exclude credentials and secrets.
- **Q57 — Fail-safe sim/real target selection:** Settled with a deferred HIL
  extension. Current setup derives
  `III_SYSTEM_PROFILE=sim|real` from sourced environment scripts, while transport
  selection is separately encoded by `CLI_CONFIGURATION`; this is too implicit
  for a unified local/remote configuration workflow. Retain
  convenient profile defaults (`setup_dev` selects local simulation;
  `setup_field` selects `real` at `iii.local`) and permit an explicit per-command
  `--target sim|real` override, but never keep a hidden mutable global target.
  Every target advertises its actual profile and identity during preflight, and
  every mutating CLI/GUI request carries the expected profile; mismatch fails
  closed before editing, applying, snapshot loading, capture, or deployment. Show
  a persistent active-profile target badge and endpoint in the GUI (initially SIM,
  REAL, and only when commissioned OPTITRACK). Do not encode an
  invariant that `sim` is always local or `real` is always remote: future HIL
  runs III on the Pi while Gazebo runs on the workstation over LAN.
- **Q58 — Deployment preparation for future split-host HIL:** Settled.
  Current supervision accepts only hard-coded `sim`, `real`, and `opti_track`
  profiles; development Gazebo transport defaults to loopback; CycloneDDS files
  hard-code workstation/Pi interface names. Do not implement HIL
  launch or behavior now, but decouple target endpoint, runtime profile, execution
  host, and simulator provider in deployment/CLI manifests; make release manifests
  declare extensible supported-profile/capability data rather than a closed
  sim/local versus real/remote mapping; parameterize middleware interface/peer
  configuration from detected stable LAN interfaces; and reserve an opt-in,
  disabled-by-default network-policy extension for future workstation simulator
  peers. Keep Gazebo and its assets off the Pi. Add contract tests proving a future
  split-host profile can be represented and provisioned without changing bundle,
  receiver, target-selection, or configuration-capture formats. A provisioned
  drone defaults to the `real` runtime profile. Future HIL may select a separate
  profile through an explicit III runtime boot/profile argument; defining or
  implementing that profile remains outside this deployment scope.
- **Q59 — Clock handling on a power-cycled, sometimes-offline Pi:** Settled.
  On every Pi boot, start only the receiver and minimal daemon/runtime-API control
  plane in an explicit `DEGRADED_CLOCK` state. Do not boot/start the ROS graph or
  permit flight, mission, operation, configuration mutation, capture mutation, or
  other runtime operations until clock synchronization commits. Permit only
  read-only identity/status/health/diagnostics plus authenticated clock-sync and
  deployment/recovery control needed to repair the gate. Buffer III daemon/API
  logs in a bounded in-memory ring tagged with boot ID and monotonic timestamps;
  do not persist ordinary III runtime logs while degraded. Keep only the minimal
  receiver/host audit required for recovery, explicitly marked time-untrusted.
  An authorized ground-control companion automatically invokes the same clock-
  sync operation when it discovers `iii.local`, independent of whether the GUI is
  open. Any computer authorized by its SSH key can invoke `iii system clock sync`
  manually. The receiver owns the narrow privileged clock-set operation over its
  authenticated SSH/local-socket control path; never expose clock setting through
  the unauthenticated plain-HTTP LAN API. At synchronization, anchor operator UTC
  to target monotonic time, set the clock, reconstruct UTC for buffered entries,
  flush them in order with reconstruction/uncertainty metadata, durably record
  the clock-gate transition, and only then automatically boot the `real` graph to
  operational standby and enable operations. Correctness-critical identities and
  ordering still use boot ID plus monotonic revisions rather than wall time.
- **Q60 — Clock validity after initial synchronization:** Settled. After a
  successful boot-time synchronization, loss of the GC
  connection or internet/NTP does not re-enter `DEGRADED_CLOCK`; autonomous
  operation must continue using the Pi's monotonic clock. Re-enter the gate only
  on the next Pi boot or if the kernel reports a real-time clock discontinuity/
  invalidation beyond a defined safety threshold. A later manual sync while the
  runtime is active may measure/report offset but must not step wall time; a large
  correction requires stopping the graph, re-entering the clock gate, syncing,
  flushing, and then explicitly starting again. A discontinuity detected while the
  aircraft is armed, airborne, or has an active Reference Owner must never stop the
  graph or interrupt the current safety-critical action merely to repair wall time:
  enter `CLOCK_FAULT_ACTIVE`, continue existing monotonic-time control, buffer
  time-uncertain logs, and reject new mission/direct/configuration/deployment work.
  Once landed, disarmed, and owner-free, stop the graph and transition to
  `DEGRADED_CLOCK` for synchronization. If already maintenance-safe, transition
  immediately.
- **Q61 — Ground-control computer deployment scope:** Settled. Current GC v2
  is a ROS-free React frontend plus thin FastAPI discovery/proxy, packaged through
  Docker Compose and started by a workspace script with a manually provisioned
  environment file. QGroundControl assets/configuration and CLI setup are separate,
  and there is no converged GC-host installer, service baseline, offline update
  transaction, or integrated tuning-mirror/clock-sync companion. Make GC
  provisioning a first-class target of this same in-repository deployment
  system. For the current source-repository field workflow, converge Ubuntu 22.04/
  24.04 x86_64 with Docker/Compose, the pinned production frontend/proxy, III CLI,
  tuning mirror and automatic clock-sync companion, QGroundControl, mDNS/network
  policy, browser launcher, operator keys, logs, offline caches, and the pinned
  ARM64 build environment. Keep ROS/DDS/MAVSDK off the GC proxy host boundary.
  Separate this into an operational GC base role and an optional development/
  cross-build role, but install both on the current GC laptop; this preserves a
  future source-repo-independent field computer without requiring it now. Manage
  GC application versions transactionally from the same release metadata, with
  local rollback and offline installation, while keeping GC host OS maintenance
  an explicit Ansible workflow. For this sweep, assume every GC computer has this
  workspace cloned locally. Install and run QGroundControl natively on the host
  through the same in-repository GC provisioning path for field and simulation;
  remove the devcontainer-owned QGroundControl AppImage/path and have simulation
  interoperate with the host process.
- **Q62 — Coupling GC and drone application releases:** Settled. One workspace
  release produces a paired, signed GC artifact and
  ARM64 drone artifact with one release ID and explicit API compatibility range,
  but each remains independently installable and rollbackable. The GC updater is
  host-native and outside the frontend/proxy containers. During a coordinated
  update, install/health-check the new GC first while it proves compatibility with
  both current and candidate drone APIs, then activate the drone. If GC update
  fails, never touch the drone; if drone activation fails, roll back the drone and
  retain the new GC only if it declares compatibility with the restored release,
  otherwise roll back GC too. Apply the same ordering to dirty field builds.
- **Q63 — GC boot/login lifecycle:** Settled. Install the GC
  proxy/static frontend, discovery, tuning mirror, and clock-sync companion as
  managed user-session services that start automatically when the operator logs
  into the graphical Ubuntu session, restart on failure, and remain available
  without opening a browser. Automatically discover only `iii.local`, perform the
  Q59 clock handshake for a `real` target, and backfill captures. A `sim` target
  explicitly skips clock alignment and never enters the Pi-specific clock gate.
  Do not automatically launch a browser
  window or QGroundControl. Expose an III GUI desktop launcher plus `iii gc
  open/start/stop/restart/status` for the GC stack, and a separate QGroundControl
  launcher plus `iii qgc start/stop/restart/status` for host-native QGC. Logout
  cleanly stops graphical/user services without affecting the drone.
- **Q64 — QGroundControl version and update ownership:** Settled. Treat the host-
  native QGroundControl AppImage as a pinned,
  checksum-verified GC dependency owned by the in-repository deployment path, not
  as an independently self-updating application. Cache/install versioned binaries,
  select one active version atomically, retain the previous known-good version,
  and update only through a tested GC release or explicit GC host-maintenance
  operation. Preserve user settings/logs outside the binary slot, remove hard-coded
  `/home/iii` paths, back up settings before migrations, and use the same active
  binary for simulation and real operation.
- **Q65 — Release-owned PX4 parameters and QGroundControl configuration:**
  Settled. Add versioned declarative manifests for the complete
  expected real and sim PX4 parameter sets and a sanitized QGroundControl managed-
  settings baseline. Each release records their content hashes, compatible PX4/
  QGC versions, and generated inventories. Classify PX4 keys as release-required,
  operator-tunable, or hardware calibration/identity. Activation reads and diffs
  the FMU but never silently writes it: required mismatch blocks real health;
  tunable mismatch is reported; calibration/identity is captured and preserved.
  Provide explicit disarmed `iii px4 params pull/plan/apply/verify` with full FMU
  backup, per-key review, readback, and recovery, while retaining Q20's separate
  firmware-flashing boundary. For QGroundControl, manage only declared stable
  keys, merge them transactionally into user settings with backup, preserve
  window/layout/local preferences unless explicitly managed, disable public log
  upload by default, and regenerate/cache version-coupled parameter metadata rather
  than treating ParamCache as hand-maintained configuration. Use the same release-
  owned inventory for sim and real, with profile-specific values.
- **Q66 — Capturing PX4 changes made through QGroundControl:** Settled. After real-
  target clock sync and while disarmed, the GC companion
  automatically downloads a complete PX4 baseline with firmware/schema identity.
  Monitor MAVLink parameter updates and periodically reconcile the complete set;
  when its content hash changes, mark PX4 configuration modified and mirror an
  immutable revision locally without claiming QGC-originated edits are III GUI
  transactions. Allow the operator to save/download arbitrary named PX4 sets with
  short descriptions through the same untracked capture store used for III sets.
  At test end, `iii px4 params capture/pull/diff/promote` performs per-key review
  into the tracked real or sim PX4 manifest, classifies required/tunable/calibration
  changes, and creates source changes only on the current feature branch. Never
  auto-commit, auto-promote, or overwrite the FMU during deployment. Use the same
  workflow against simulated PX4, skipping the real-target clock gate.
- **Q67 — Capturing and promoting QGroundControl settings changes:** Settled.
  Maintain a schema-versioned allowlist of release-managed QGC
  keys plus explicit classes for local preference, generated/cache, sensitive,
  and prohibited keys. On QGC clean exit and on explicit request, snapshot its
  settings, redact/exclude non-exportable data, and produce a local immutable diff
  against the active release baseline. Provide `iii qgc config
  capture/diff/promote`;
  promotion reviews each changed key, rejects machine geometry/paths, credentials,
  generated ParamCache, and unsafe public-upload changes, and writes only accepted
  managed keys on the current feature branch. Runtime user settings remain
  untouched until a later GC release transactionally merges the promoted baseline.
  Use one shared managed baseline for sim and real, with separately declared
  profile-specific connection settings only where necessary. All QGroundControl-
  specific installation, lifecycle, status, configuration, and maintenance
  commands live under `iii qgc`; `iii gc` is reserved for the III frontend/proxy,
  discovery, mirror, clock companion, and browser-facing ground-control stack.
- **Q68 — Starting point for GC host provisioning:** Settled. Support a clean,
  normally installed Ubuntu 22.04 or 24.04 x86_64 system with a
  graphical user account and this repository clone as the GC provisioning boundary.
  `iii gc provision` bootstraps a repository-managed environment and runs local
  Ansible to converge everything above it, with online and prepared-offline modes.
  Do not automate workstation disk partitioning, Ubuntu installation, full-disk
  encryption, proprietary hardware drivers, or vendor firmware in this sweep;
  inspect and report those prerequisites instead.
- **Q69 — Enrolling credentials on additional GC/operator computers:** Settled.
  Current real runtime uses one shared browser password and one shared CLI token,
  while the settled deployment model already requires unique SSH and signing keys
  per computer. Extend enrollment so every authorized computer has
  its own revocable SSH key, field signer, and runtime API client credential stored
  locally with user-only permissions and hashed/identified in the onboard trust
  store. An already authorized computer adds the new SSH public key; the new
  computer then completes authenticated `iii access enroll` over SSH to receive
  its own runtime credential. Keep one human browser-login secret for the solo
  operator initially, provisioned separately from releases, while the local GC
  proxy uses its machine credential for companion/CLI traffic. `iii access list`
  and `revoke` act per computer and never transfer private keys. The accepted
  plain-HTTP LAN risk remains explicit. Replace the single shared CLI token with
  this per-computer enrollment model.
- **Q70 — Component selection for dirty field deployment:** Settled. Make `iii
  deploy field` analyze the workspace/submodule change
  graph and produce only affected artifacts: GC-only for frontend/proxy/GC/QGC-
  baseline changes, drone-only for runtime-only code, and paired GC-then-drone for
  contracts/interfaces/shared configuration or changes crossing the API boundary.
  Show the inferred component set and reasons in the default plan. Permit explicit
  `--component gc|drone|both`, but fail closed if the override omits a dependency-
  affected component; never let an override create an API-incompatible pair. Build,
  sign, install, health-check, and rollback GC locally; cross-build and activate
  the drone through the receiver. PX4 parameter-manifest changes update release
  expectations and drift reports but never write the FMU implicitly. Preserve Q44's
  no-commit/no-working-tree-mutation contract.
- **Q71 — Installed GC release retention:** Settled. Mirror the operational drone
  policy on the GC: protect the latest qualified GC release as
  rollback anchor, retain the active GC release and previous field-development GC
  release, and allow one staged candidate. Keep these identities paired with their
  compatible drone release metadata, but do not require the GC and drone to occupy
  the same active version when the compatibility contract permits otherwise.
  Separately retain downloaded artifact archives in a size-bounded local cache;
  pruning cache entries never removes installed/protected slots, captures, keys,
  QGC settings, or offline provisioning dependencies.
- **Q72 — Safety gate for GC/QGroundControl updates:** Settled. Any GC frontend/
  proxy/companion, CLI, QGroundControl, or GC host-maintenance
  update that can restart operator tooling must fail closed while a connected real
  target is armed, in air, owns a mission/custom/direct operation/reference, or is
  otherwise not in the settled maintenance-safe state. Drain/reject new browser
  commands, verify landed/disarmed stability, then transactionally update. Permit
  updates when no real target is connected, and do not impose the real-flight gate
  on simulation. Keep an explicit maintenance override only for recovery, with a
  prominent loss-of-operator-surface warning and retained audit.
- **Q73 — Offline field-readiness preparation:** Settled. Add `iii field prepare`
  as an online, non-deploying home workflow that verifies the
  workspace/submodule policy and populates a content-addressed GC-local cache with
  the selected paired qualified release, protected previous release metadata,
  pinned ARM64 builder/toolchain/sysroot, build/test dependencies, GC images and
  updater, QGroundControl binary, Ansible collections/packages, recovery tooling,
  and required source/dependency mirrors for dirty incremental builds. Follow it
  with `iii field verify --offline`, which disables network access for the check,
  performs representative GC-only/drone-only/paired build and artifact verification
  probes without deployment, validates credentials and disk reserves, and emits a
  concise readiness report with exact missing items. Never update or install
  automatically merely because newer content exists.
- **Q74 — Deployment preparation for pre-field OptiTrack testing:** Settled. Do
  not implement mocap transport, frames, estimator tuning, or
  OptiTrack-server provisioning in this sweep. Make one GC/drone release bundle
  capable of declaring extensible supported runtime profiles and later booting the
  same deployed drone release as `opti_track` through an explicit cold profile
  switch, while `real` remains the installed/power-on default. Treat OptiTrack as a
  physical-real target for clock sync, credentials, deployment safety, PX4/QGC
  inventory, logging, rosbag, parameter capture, and rollback. Let a profile
  descriptor later declare external mocap readiness hooks, network peers, required
  PX4/III configuration overlays, and an allowlist of limited mission specifications.
  Initially declare `opti_track` explicitly as an alias of `real`, inheriting its
  readiness and allowlists by intentional manifest policy rather than accidental
  non-sim branching; later changing that alias requires a versioned profile-contract
  change. Display the active profile prominently. Add a
  pre-field evidence workflow that deploys once, switches to OptiTrack, runs its
  profile-specific checks/tests, captures evidence, then switches back to `real`
  and verifies field readiness without redeployment. Keep the profile and artifact
  boundaries extensible for the later integration change.
- **Q75 — First-class behavior-tree and mission-specification artifacts:**
  Settled. Keep source ownership in `III-Drone-Mission`, but
  have every qualified or field release build a signed, immutable mission-catalog
  sub-artifact. Each named mission specification receives a stable logical name,
  content identity, status/classification, full transitive closure of referenced
  behavior-tree XML/model assets, schema version, and required behavior-node/
  interface/runtime compatibility hashes. Build-time validation resolves no
  source-tree/absolute environment paths, rejects missing/escaping references and
  invalid trees/specs, and records the catalog hash in both paired release
  manifests. Real operation may select only catalog entries permitted by the
  active release/profile; raw onboard files and arbitrary path override are
  rejected. Mission selection and every execution record exact artifact IDs.
  Asset-only dirty changes may produce a fast field release without recompiling
  unaffected code, but still pass validation, signing, safety-gated activation,
  health checks, and rollback; normal deployment never mutates an installed
  catalog in place. Release rollback restores the matching catalog atomically.
  Test/legacy assets must be explicitly classified and excluded from qualified
  real allowlists by default. OptiTrack can later declare its limited allowlist
  without changing artifact formats. As `III-Drone-Mission` is a CMake package,
  make CMake own catalog generation, reference closure, validation, dependency
  setup, and package-owned custom-script invocation. Install the generated catalog,
  mission specifications, behavior trees, models, and dependency metadata into the colcon install prefix
  under `share/iii_drone_mission`; resolve them live through the ament package
  index and catalog identities in `sim`, `real`, and profile aliases. Runtime must
  never depend on source-tree paths, including during simulation.
- **Q76 — Package-native parameter installation and reconciliation boundary:**
  Settled. Treat configuration installation and reconciliation as native Python-
  package responsibilities. Install immutable manifests, profile descriptors,
  schemas, and tracked defaults as Python package data in the colcon install
  space, and expose one importable reconciliation engine. For development and
  simulation, startup automatically runs that engine against the writable sim
  parameter sets before any set is selected or applied. For the aircraft, only
  the deployment receiver runs the same engine as a planned, transactional
  release-activation step against persistent onboard parameter sets. Both paths
  obey the settled preserve/add/retire/reintroduction-review rules, but simulation
  startup is the automatic development convenience while drone mutation remains
  deployment-owned. Runtime never prefers source-tree configuration over installed
  package data. Retire the ad hoc shell installer and standalone mutating script
  after migration.
- **Q77 — Blocking reintroduction review during simulation startup:** Settled.
  If automatic pre-simulation reconciliation encounters a
  reintroduced parameter, block simulation startup before applying any parameter
  set and create the same review file under repository-local `.iii/` as the drone
  deployment planner. After every decision is resolved, rerunning sim startup
  completes reconciliation and launches normally. Do not silently choose the new
  default merely because the target is simulation; matching semantics are valuable
  before aircraft deployment.
- **Q78 — Development freshness for installed mission assets:** Settled. Make
  the colcon install space the only runtime source even in
  development. A mission-specification or behavior-tree source edit therefore
  becomes runnable only after the `iii_drone_mission` package's CMake install/
  validation step refreshes the installed catalog. With `--symlink-install`, raw
  assets may remain live-linked where safe, but generated catalog identity and
  dependency validation must still be refreshed. Before simulation startup, detect
  source/install catalog drift and automatically run the targeted
  `iii_drone_mission` build/install/validation step before launch. If generation,
  compilation, installation, or validation fails, simulation does not start and
  reports the exact failure. Field deployment likewise performs this build as
  part of artifact creation.
- **Q79 — Whether automatic sim reconciliation may edit tracked defaults:**
  Settled. Never mutate the repository's tracked
  `sim/tracked/default.yaml` during build or simulation startup. Install it as the
  immutable baseline, then seed/reconcile the existing living sim parameter tree
  under workspace-local `.config/iii_drone/parameter_sets/sim/` before applying
  its selected set. The configuration server and GUI continue to operate on that
  living tree exactly as they do now. Source defaults
  change only through the already settled explicit capture/compare/promote workflow,
  so starting simulation cannot create hidden Git changes or accidentally promote
  an experiment.
- **Q80 — Scope and persistence of local simulation state:** Settled. Keep each
  workspace clone's living sim configuration under its
  own Git-ignored `.config/iii_drone/` rather than sharing `~/.config` across
  clones. Preserve it across builds, container recreation, branch switches, and
  ordinary simulation restarts; let the reconciler handle schema movement in both
  upgrade and downgrade directions. Provide explicit CLI operations to inspect,
  checkpoint, and reset the sim profile to the currently installed tracked default,
  with reset requiring confirmation and retaining a recoverable capture. The sim
  capture path is the real capture path parameterized by `--target sim`: it uses
  the same sealed artifact format, names/descriptions, provenance, verification,
  diff, export/import, and reviewed promotion workflow.
- **Q81 — Installing development/test missions versus packaging aircraft missions:**
  Settled. Let the local colcon install space contain every
  valid, explicitly classified mission catalog entry—including simulation-only,
  test, and legacy entries—so development can exercise them without alternate
  source paths. During drone artifact creation, package only entries allowed by
  the target profile plus their exact transitive dependencies. Drone bundles never
  include sim-only, test, or legacy entries. A field-development deployment may
  include an explicitly classified onboard-compatible `experimental` entry after
  a prominent warning; it must not relabel a local test as aircraft-compatible
  implicitly. Record the resulting reduced catalog hash in the drone manifest.
- **Q82 — Authoritative behavior-node and mission dependency declarations:**
  Settled. `models.xml` is only a manually checked-in BehaviorTree.CPP/Groot
  editor model; runtime does not consume it. Keep runtime node
  registration/port declarations authoritative, add a deterministic build-time
  tool that constructs the same factory, validates every catalog tree against it,
  and generates `models.xml`/Groot project data as derived install-space artifacts.
  Mission authors explicitly declare only logical identity, classification,
  profile allowlist, and entry specification; tooling infers tree/subtree/node
  dependencies from YAML/XML and writes the resolved catalog closure. Do not make
  editor metadata or duplicated dependency lists authoritative. The runtime factory
  plus build-time validation/generation is the model.
- **Q83 — Runtime mission-catalog selection and persistence:** Settled. Replace
  arbitrary-path override with `iii mission select
  <catalog-entry-id>`. For `real`/OptiTrack, require the full maintenance-safe state
  and an entry allowed by the active profile/release; for `sim`, require only that
  no mission/operation is active. Reuse the existing transactional rebuild/rollback
  behavior. Treat selection as temporary runtime-session state: a cold restart or
  reboot restores the profile's release-defined default mission, while the command
  can explicitly select another entry again. Changing a profile's persistent
  default remains a reviewed source/release change, not an onboard mutation.
  Expand `iii mission` into the first-class mission operator surface:
  `status` displays active catalog ID/hash, active profile, release-defined default,
  temporary/default state, executor/mode state, and readiness; `list` shows entries
  selectable for the active profile and their classification; `list --all` also
  shows incompatible/unavailable entries with exact reasons; `show <id>` displays
  metadata and resolved dependencies; `select <id>` performs the safety-gated
  transactional switch; and `select --default` restores the release default.
  Mission/profile compatibility originates in CMake registration and is carried
  into the generated catalog rather than inferred from filenames or paths.
- **Q84 — CMake mission registration and profile defaults:** Settled. Add one
  declarative `iii_register_mission(...)` CMake API with
  required stable ID, specification entry file, classification, and compatible
  profiles. CMake validates IDs/files/profile names, generates the catalog closure,
  and fails duplicate or unclassified registration. Separately, each versioned
  profile descriptor names exactly one default mission from its compatible set.
  The initial `opti_track` alias inherits `real` compatibility/default policy;
  when it later becomes distinct, explicit profile metadata can narrow its
  compatible set and choose its own default.
- **Q85 — Full mission index versus assets packaged on the drone:** Settled with
  a target-specific index. The workspace/local install catalog contains
  all classified entries, but a drone bundle's index and assets contain only the
  union applicable to onboard profiles: `real`, `opti_track`, and future `hil`.
  Sim-only, test, and legacy missions are always absent from the drone index as well
  as its assets. On the drone,
  `iii mission list --all` means every entry present in that target catalog,
  including entries incompatible with the currently active onboard profile; it
  does not reveal source-registry entries intentionally excluded from the bundle.
  No mission asset is fetched dynamically.
- **Q86 — How HIL appears before HIL runtime integration exists:** Settled.
  Reserve `hil` now as a valid onboard profile identity in catalog
  and release schemas, allowing CMake mission registrations to declare HIL
  compatibility and ensuring drone bundles retain the required closure. Mark the
  profile `not_commissioned`/non-bootable until the later Gazebo-over-LAN runtime
  adapter and readiness checks are implemented; `iii system start --profile hil`
  then fails clearly instead of accidentally taking a real or sim code path. Real
  remains the power-on default. Deployment prepares HIL explicitly without
  claiming it is operational in this sweep.
- **Q87 — Classification for experimental onboard missions:** Settled. Define
  `experimental` as a first-class mission classification
  distinct from `production`, local-only `test`, and `legacy`. An experimental
  mission must explicitly list an onboard-compatible profile (`real`, `opti_track`,
  or eventually commissioned `hil`) and pass the same build-time tree/port/
  dependency validation. It may be included only in a field-development bundle,
  never a qualified release, and `iii mission status/list/select` must display a
  persistent prominent warning while it is selected. This supports genuine
  aircraft experiments without putting test assets on the drone.
- **Q88 — Selecting experimental missions for a field-development bundle:**
  Settled. `iii deploy field` detects which registered
  experimental mission entries are affected by the dirty source snapshot and
  include those entries by default, showing each one and its dependency reason in
  the deployment plan. Permit repeatable `--include-mission <id>` for an unchanged
  experimental entry needed for the test and `--exclude-mission <id>` for an
  inferred entry only when nothing selected depends on it. Qualified release
  tooling rejects both options and all experimental entries. Bundle inclusion does
  not activate an experimental mission; `iii mission select` remains separate.
  Deployment plan and completion output summarize mission entries, behavior-tree
  assets, and parameter changes together: added/changed/removed missions with
  classification/profile compatibility; changed trees/models with every impacted
  mission ID; manifest keys/defaults added/changed/removed/reintroduced; affected
  parameter sets and preserve/review actions; and the resulting component/catalog/
  configuration hashes.
- **Q89 — Deployment change-report detail and persistence:** Settled. Print a
  concise grouped summary by default, with warnings and
  required decisions inline. Persist the complete machine-readable impact graph,
  parameter reconciliation plan, and final applied result under the operation's
  Git-ignored `.iii/operations/<operation-id>/` directory, and expose the same data
  through `iii deploy plan/status --json`. The completion report must distinguish
  planned, packaged, transferred, activated, skipped, rejected, and rolled-back
  changes so an interrupted or failed deployment never looks successful.
- **Q90 — Retention of local deployment-operation records:** Settled. Keep
  operation metadata, plans, reviews, reports, hashes, and
  compact diagnostics indefinitely by default because they are small and provide
  the audit trail for dirty field work. Store large bundles/build outputs only in
  the separately size-bounded cache. Add explicit `iii deploy operations prune`
  with dry-run and age/status filters; never prune records referenced by captures,
  installed/protected releases, unresolved reviews, failed operations not yet
  acknowledged, or qualified-release evidence.
- **Q91 — Final onboard filesystem and ownership boundary:** Settled. Standardize
  immutable application slots under
  `/opt/iii/releases/<release-id>/` with `/opt/iii/current` as a root-owned atomic
  selector; receiver A/B slots and stable bootstrap under `/opt/iii/receiver/`;
  root-owned host policy/trust/environment under `/etc/iii/`; persistent mutable
  configuration, legacy shadow, journals, deployment state, and unprivileged
  incoming uploads under `/var/lib/iii/`; bounded logs/audits under `/var/log/iii/`;
  and sockets/locks/transient state under `/run/iii/`. Run the application as
  `iii` and the forced-command deployment transport as unprivileged `iii-deploy`;
  only the narrow receiver/helper owns privileged selectors, host integration,
  and systemd transitions. The separately keyed human `iii` SSH boundary has
  explicit attended full sudo. No release may write into
  another release, `/etc/iii`, or arbitrary host paths.
- **Q92 — Colcon install layout inside immutable releases:** Settled. Preserve
  the current default isolated colcon layout inside each
  release (`<release>/install/<package>/...`) rather than introducing
  `--merge-install`. Start runtime through a release-owned environment wrapper that
  sources `<release>/install/setup.bash`; resolve all package assets through the
  ament index, never by assuming physical prefix paths. Package isolation improves
  provenance and collision detection and matches current development paths, while
  the wrapper hides layout details from systemd and operators. Bundle no source,
  build, or colcon log trees. Immutable drone and GC ROS bundles use this isolated
  install layout; development may continue using `--symlink-install`.
- **Q93 — Host-provisioned versus release-local dependencies:** Settled. Let
  Ansible own the stable native platform baseline—Ubuntu,
  kernel/firmware, ROS Jazzy, systemd, hardware/udev support, and explicitly pinned
  apt libraries. Make each III release carry its isolated colcon output plus all
  project-specific Python packages and non-platform runtime libraries needed by
  that build. The release manifest records the exact compatible host-baseline
  contract, and activation rejects missing/incompatible host packages. Normal
  deployment never runs apt or pip on the drone; host-package changes require an
  explicit `iii host maintenance`/Ansible transaction before application
  activation.
- **Q94 — Release-local Python dependency layout:** Settled. Do not cross-build a
  relocatable virtualenv. Resolve and lock
  Python dependencies during artifact construction, build/download target-ABI
  wheels in the pinned builder, and install release-owned packages into a plain
  target `site-packages` tree inside the immutable release. The release wrapper
  prepends that tree while retaining the provisioned system Python/ROS Jazzy
  packages; manifests record Python ABI and every wheel hash. Reject undeclared
  imports and incompatible native extensions during target-equivalent validation.
  Never invoke pip on the drone.
- **Q95 — Release-local native shared-library policy:** Settled. Keep glibc, the
  dynamic loader, ROS Jazzy, GPU/kernel interfaces,
  and explicitly provisioned platform libraries host-owned. Package workspace-built
  and other approved non-platform shared libraries inside the immutable release.
  During build, scan every ELF dependency/RPATH/RUNPATH against a versioned host-
  library allowlist; reject unresolved libraries, builder/sysroot paths, accidental
  host contamination, and bundled platform-library duplicates. Use relocatable
  `$ORIGIN`-relative RUNPATH where practical plus the release setup wrapper's
  generated library path. Validate the complete closure on target-equivalent ARM64
  before signing.
- **Q96 — Stable systemd units versus release-owned launch definitions:**
  Settled. Install a small fixed set of root-owned systemd units
  through Ansible for the receiver/bootstrap, III system daemon, runtime API, and
  any genuinely host-level companion. Application units invoke stable host launchers
  that resolve `/opt/iii/current`, verify its manifest/selector, and execute the
  release-owned environment wrapper. Releases own the ROS/process topology consumed
  by the III daemon but cannot add, overwrite, enable, or disable host systemd units.
  Any required host-unit contract change is a separately planned Ansible host-
  maintenance prerequisite, while ordinary code/process-topology changes remain
  rollbackable with the release.
- **Q97 — Automatic rollback after a release has been accepted:** Settled. Allow
  automatic rollback only while an activation transaction is
  still in its bounded candidate-health window or while reconciling an interrupted
  activation on boot. Once the receiver durably accepts a release, later crashes,
  failed readiness, or hardware faults must not silently switch software versions—
  especially during or near flight. Stable systemd units may perform bounded
  process restarts; repeated failure enters a visible non-operational fault while
  preserving diagnostics. The operator can request rollback only through the
  normal maintenance-safe gate (or explicit physical recovery override).
- **Q98 — Mapping runtime profiles to parameter profiles:** Settled. Stop assuming
  the runtime profile name is also the parameter-set
  directory name. Add an explicit `parameter_profile` reference to each versioned
  runtime profile descriptor. Keep the only tracked defaults as settled: `real`
  and `sim`. Map `real -> real`, `sim -> sim`, initial `opti_track -> real`, and
  reserved non-bootable `hil -> sim`. The living runtime selector/snapshots remain
  partitioned by runtime profile where needed so OptiTrack experiments do not
  silently change the ordinary real selection, while both reconcile from the same
  tracked real baseline. Future HIL-specific values require an explicit versioned
  overlay/profile decision outside this sweep, not an accidental third default.
- **Q99 — HIL composition across non-mission configuration domains:** Settled.
  Encode profile composition explicitly rather than branching on
  `profile == sim`. For reserved HIL, use the `sim` III parameter baseline and
  simulated-PX4 expectation/airframe baseline, plus the common QGroundControl
  baseline with its sim-specific overlay. Still apply onboard/real policies for
  deployment receiver, clock gate, credentials, hardware-host identity, logging,
  and maintenance safety because III runs on the Pi. Mission compatibility remains
  independently declared as `hil` in CMake. This is schema preparation only; HIL
  stays non-bootable in this sweep.
- **Q100 — Deployment-redesign scope closure:** Rejected. Do not freeze the scope
  yet; continue grilling until the remaining factory, host, release, runtime,
  ground-control, field-operation, recovery, evidence, and retirement branches
  have concrete contracts. The previously identified future capabilities remain
  deferred unless later questions intentionally bring preparation work into scope.
- **Q101 — First-boot cloud-init secret lifecycle:** Settled. Generate per-imaging
  cloud-init seed data only from Git-ignored, permission-
  checked operator inputs. Limit it to initial network profiles, the first SSH
  public key, bootstrap identity, and a one-time Ansible bootstrap credential—never
  release/signing private keys or reusable plaintext passwords. On successful
  convergence, copy required network state into root-only host configuration,
  remove/overwrite secret-bearing cloud-init seed and instance data where the
  filesystem permits, disable unintended cloud-init reruns, revoke bootstrap-only
  authority, and emit an inspection report proving what remains. If sanitization
  cannot be verified, commissioning fails.
- **Q102 — Safe SD-card target selection and image verification:** Settled. Make
  `iii host image` enumerate candidate removable block devices
  with stable device path, model, serial, size, transport, mount state, and whether
  any partition backs the running system. Refuse the system/root disk, mounted or
  in-use devices, unresolved device-mapper relationships, non-removable/internal
  disks by default, and targets smaller than the pinned image. Require explicit
  device selection plus typed confirmation containing the device identity; allow
  no unattended destructive override initially. Verify the downloaded image hash
  before writing, flush/eject, then read back and verify written image/partition
  content before generating a commissioning record.
- **Q103 — SD partition and filesystem layout:** Settled. Keep the checksum-pinned
  Ubuntu image's supported Raspberry Pi boot partition and
  auto-expanded ext4 root filesystem. Do not create custom release/data partitions,
  LVM, encryption, or A/B roots in this sweep; `/opt/iii`, `/var/lib/iii`, and
  `/var/log/iii` share the root filesystem and rely on the settled reserve/retention
  rules. Treat physical reimage as destructive to onboard mutable state: require a
  pre-reimage inspection and explicit backup/export of configuration, captures,
  PX4/QGC inventories where applicable, deployment audits, and diagnostics, with a
  separately confirmed `--accept-data-loss` path only for unrecoverable media.
- **Q104 — Portable host backup and post-reimage restore scope:** Settled. Add
  `iii host backup` to create a verified, versioned archive of
  portable persistent state: III parameter sets/selectors, legacy shadow records,
  tuning journals/captures not already local, deployment audits, hardware/PX4
  inventories and backups, and retained diagnostics selected by policy. Exclude
  SSH/signing private keys, runtime/API credentials, Wi-Fi secrets, receiver
  transaction state, active-release selectors, and host-generated machine identity;
  reprovision/re-enroll those instead. After reimage and Ansible convergence,
  `iii host restore` validates the archive, deploys/chooses a compatible release,
  previews schema reconciliation, restores into a staged persistent root, and
  activates only after review and health checks. Never overwrite a running target
  or restore stale transaction machinery.
- **Q105 — Local backup storage, triggers, and retention:** Settled. Store
  immutable content-addressed host backups under Git-ignored
  `.iii/backups/`, with list/show/verify/export/import operations and checksummed
  portable archives like configuration captures. Require a fresh verified backup
  before a planned physical reimage and before host maintenance that can invalidate
  the current system, unless the operator separately confirms unrecoverable-source
  data loss. Do not require a full host backup before ordinary transactional
  application deployment. Never auto-prune backups initially; explicit pruning
  previews contents and refuses backups referenced by restore/audit evidence.
  Because the settled archive excludes credentials/secrets, integrity verification
  is mandatory but encryption is optional/operator-managed in this scope.
- **Q106 — Consistent onboard snapshot for host backup:** Settled. Require the
  real/OptiTrack target to pass the maintenance-safe
  gate, then have the onboard receiver briefly quiesce configuration/tuning writers,
  flush journals, and create a filesystem-independent point-in-time backup staging
  tree with one manifest/hash boundary. Resume the prior standby runtime state once
  that local snapshot is sealed; download/compression may continue afterward without
  holding the runtime stopped. HIL remains non-bootable; sim backup uses the same
  snapshot engine locally without the physical-flight gate. Never archive a set of
  files copied concurrently without a coordinated revision/checkpoint.
- **Q107 — Interrupted first-boot provisioning:** Settled. Make `iii host
  provision` resumable and Ansible-idempotent. Retain bootstrap access until the
  permanent operator key, receiver, and recovery path are verified; interrupted
  privilege narrowing or secret sanitization must resume before commissioning can
  pass. If the Pi never becomes SSH-reachable, retry through Ethernet recovery. If
  cloud-init remains unrecoverable without authenticated access, reimage rather
  than introduce a default password or hidden bypass.
- **Q108 — Provisioned versus commissioned host state:** Settled. Report
  `provisioned` when raw-image/cloud-init/Ansible convergence,
  credentials, sanitization, receiver recovery, storage, network, clock tooling,
  and host inspection pass. Reserve `commissioned` for a provisioned host that has
  a qualified release installed as protected anchor, complete hardware-role and
  PX4 compatibility evidence, successful activation/rollback and power-cycle tests,
  verified GC connection/QGC baseline, and a fresh portable backup. Field-
  development bundles may run on a provisioned test host, but cannot create a
  production commissioning record.
- **Q109 — Commissioning-record identity, signing, and retention:** Settled.
  Create an immutable structured commissioning record bound to the
  shared hardware-class/host-baseline manifest, qualified release, PX4/QGC
  compatibility evidence, test results, and timestamp—not a fleet aircraft ID.
  Sign it with the commissioning computer's authorized field-signing key, retain a
  verified copy onboard and content-addressed under local Git-ignored
  `.iii/commissioning/`, and support verify/export/import. Redact credentials and
  network secrets; include exact hardware-role observations needed to reproduce
  acceptance even if they contain device serials. Do not commit records to Git or
  publish them automatically.
- **Q110 — Commissioning validity after system changes:** Settled. Compute current
  commissioning status against the latest accepted
  commissioning record. Host-baseline, boot-policy, hardware-role mapping/device,
  PX4 firmware/required parameters, trust-root, or qualified-anchor changes mark it
  `recommission_required` until the affected acceptance subset and recovery tests
  produce a new signed record. Deploying a field-development application/mission
  bundle does not erase the qualified commissioning anchor, but status must show
  `commissioned baseline + field development active` and retain the active bundle's
  warnings. Returning exactly to the commissioned anchor clears that overlay.
  Ordinary tuned III values within the valid schema do not invalidate commissioning;
  their journal/capture provenance remains separate.
- **Q111 — Hardware-device replacement workflow:** Settled. If a replacement
  device still matches the shared role manifest unambiguously,
  require role-specific inspection/functional acceptance and a refreshed
  commissioning record, but no source-manifest change. If it does not match, `iii
  host hardware inspect` reports the unmatched observations and can export a
  reviewable capture; updating match rules/serial allowlists occurs only through a
  feature-branch source change, tests, Ansible convergence, and recommissioning.
  Never auto-learn a new serial or rewrite udev policy from the connected device.
- **Q112 — Universal next-command guidance:** Settled. Every III CLI command result,
  including success, no-op, warning, rejection, failure, interruption, and help,
  ends with one or more context-aware suggested next commands. Suggestions include
  exact target/profile/operation IDs where needed, a short reason, whether the
  command is mutating, and prerequisites. Human output uses a concise `Next:` block;
  structured output carries versioned `next_actions[]` entries. Suggested mutations
  retain their normal plan/confirmation gates and are never executed automatically.
- **Q113 — Interrupt and cancellation semantics for long operations:** Settled.
  Once a receiver/GC mutation is durably accepted, Ctrl-C or client
  loss detaches the CLI but does not cancel the operation; output suggests the exact
  status/reattach command. Provide explicit cancellation only before activation or
  at a receiver-declared safe checkpoint. After selector switch/activation begins,
  reject cancellation and suggest status followed by safety-gated rollback if
  acceptance succeeds, while failed candidates follow automatic transaction
  rollback. Local build/package phases may be cancelled and later resumed from
  verified cache state.
- **Q114 — Concurrency and mutation locking:** Settled. Allow at most one target-
  wide mutating maintenance transaction at a time across deployment,
  rollback, receiver update, host maintenance, restore, backup sealing, network
  reconfiguration, credential/trust mutation, and PX4 parameter application. The
  onboard receiver owns a durable lease/lock keyed by operation ID and authorized
  client identity; client disconnect does not release it. Read-only status/log/
  diagnostics and operation reattachment remain available. Configuration tuning,
  mission selection, and runtime operations are rejected once a maintenance
  transaction enters its quiescing/mutation phase; maintenance planning may occur
  concurrently but must revalidate before lock acquisition. Never offer a force-
  unlock while the owning operation is alive; stale-lock recovery is receiver/
  boot-journal driven and audited.
- **Q115 — Stale-plan and replay prevention:** Settled. Separate reusable content/
  build plans from short-lived mutation authorization. Every apply
  request binds artifact IDs, current release/config/profile/commissioning hashes,
  receiver generation, authorized client, operation ID, and a receiver-issued
  single-use nonce with a short expiry. The receiver rechecks live maintenance-safe
  state immediately before mutation, consumes the nonce atomically, and rejects
  changed state, expiry, duplicate/replayed requests, or a plan produced for another
  target/profile. Replanning reuses verified build artifacts rather than rebuilding
  unnecessarily.
- **Q116 — Deterministic release-bundle container format:** Settled. Use a
  versioned deterministic `tar.zst` container for both GC and
  drone artifacts, with canonical path ordering, normalized metadata, no device/
  special files, no escaping links, and strict unpacked-size/file-count limits. Put
  a small canonical manifest and content index at a fixed archive location and
  publish detached Ed25519 signature/checksum files so the CLI/receiver can verify
  identity before privileged extraction, then verify every extracted file again.
  Name paired assets by release ID and component, but derive trust from signed
  contents rather than filenames. Reserve format/schema versioning for future
  transport changes; never execute archive hooks.
- **Q117 — Reproducibility proof for qualified payloads:** Settled by rejecting a
  mandatory double-build gate as excessive for this research system. Require one
  clean build in the pinned builder with locked inputs, complete manifests, tests,
  signatures, and retained evidence. Keep deterministic packaging and permit an
  optional rebuild-comparison diagnostic when investigating drift, but do not block
  normal qualification on two independent byte-identical builds.
- **Q118 — Local simulation/physical evidence promotion gate:** Settled. Require
  signed local evidence before promoting `develop` into stable `main`; CI remains
  responsible for building/signing the final
  qualified artifact, but the `main -> release` PR must reference a concise field-
  validation record produced from the exact `main` commit (or an artifact proven
  source-identical to it). Require at least the scripted sim suite plus the
  appropriate provisioned-drone smoke/maintenance-safe checks; require actual flight
  evidence only when the release changes flight-critical behavior, mission logic,
  PX4 contracts, hardware interfaces, or safety policy. Pure tooling/docs changes
  need no flight. CI verifies evidence identity/required categories but does not
  contact the aircraft. Implement it as a signed local-evidence handoff:
  1. On a clean checkout of the exact workspace `develop` promotion candidate, `iii release
     evidence collect` verifies governance/lock state, runs the pinned local
     devcontainer simulation suite, derives the required physical-test categories
     from the change-impact policy, and optionally orchestrates the connected
     provisioned-drone smoke/flight checklist.
  2. It seals logs/results/environment/source identity into Git-ignored
     `.iii/release-evidence/<commit>/<run-id>/` and signs a compact schema-versioned
     attestation with the authorized workstation field-signing key.
  3. `iii release evidence submit --pr <number>` posts that signed attestation to
     the verified `develop -> main` promotion PR and triggers a lightweight GitHub verification job.
     PR text is untrusted transport; the signature, source identity, schema, required
     categories, and trusted signer are what the required `promotion-evidence` check
     validates. GitHub never runs simulation or reaches the drone.
  4. Promotion tooling proves that mechanical submodule merge commits/gitlink-lock
     refresh preserve the tested source-content identity. `main -> release` reuses
     the still-valid attestation and requires its own lightweight verification;
     changed content or policy requires recollection. The final qualified release
     includes the verified attestation/hashes in its evidence envelope; full local
     evidence remains exportable for archival.
- **Q119 — How much local validation evidence must be uploaded:** Settled. Submit
  only the signed compact attestation and concise summaries
  to the PR/qualified release by default, including hashes for every detailed log,
  rosbag, screenshot, and checklist artifact. Keep bulky evidence locally under
  `.iii/release-evidence/` with verify/export/import and preserve-by-default rules;
  allow selected artifacts to be attached explicitly when needed for review. Do
  not require uploading simulation logs or flight rosbags to GitHub for every
  research release.
- **Q120 — Reuse of promotion evidence for `main -> release`:** Settled. Permit
  reuse only when automation proves that the `main` candidate
  has the same governed source-content identity, dependency state, relevant policy,
  and required test matrix as the evidence accepted for `develop -> main`.
  Mechanical commit/gitlink identities may differ only through the already verified
  promotion process. Do not expire evidence merely because time passes; invalidate
  it on host/PX4/commissioning/test-policy drift or any relevant content change,
  then recollect only affected categories before release.
- **Q121 — Authority for required validation categories:** Settled. Version a
  repository policy mapping governed packages/paths,
  interfaces, parameter/PX4 manifests, mission classifications, host/deployment
  changes, and change types to required evidence categories such as static/unit,
  local simulation, provisioned-drone bench smoke, OptiTrack, and field flight.
  `iii release evidence plan` shows the exact reasons. Operators/agents may add
  stricter tests freely but cannot silently remove a required category; a reduction
  requires an explicit signed waiver with rationale carried into the promotion and
  release evidence. Keep the policy conservative but coarse enough for this
  research system.
- **Q122 — Validation-waiver limits:** Settled. Allow the solo maintainer to waive
  only a physical evidence category that is genuinely
  unavailable or inapplicable, using an explicit `iii release evidence waive`
  command signed by an authorized field key and bound to one exact candidate/
  policy version. Require rationale, risk statement, compensating evidence, and
  prominent PR/release-note visibility. Never allow a waiver for source governance,
  dependency locks, build success, unit/static checks, artifact integrity/signing,
  deployment safety gates, or a failing test result; a waiver means “not performed,”
  never “failed but accepted.”
- **Q123 — Deployment-focused release notes:** Settled. Generate release notes from
  the verified change-impact graph, manifests, PR metadata, and
  evidence rather than relying on a handwritten summary. At minimum show operator-
  visible features/fixes; GC/drone component changes; mission/behavior-tree catalog
  additions/removals/classifications; III and PX4 parameter schema/default changes;
  QGC/host/provisioning changes; compatibility and migration/reintroduction actions;
  evidence categories and waivers; expected downtime; and required pre/post-deploy
  commands. Permit concise maintainer annotations but never let prose override
  machine-derived facts. Publish the same structured notes with the GitHub Release
  and expose them through `iii release show`.
- **Q124 — SemVer selection and minimum bump policy:** Settled. Make `iii release
  prepare` derive and explain a minimum allowed bump from
  versioned compatibility contracts. Require MAJOR for intentionally unsupported
  upgrade/API/artifact/schema breaks; MINOR for backward-compatible features,
  new production missions/profiles, or additive interfaces; PATCH for compatible
  fixes, tuning/default adjustments, documentation/tooling, and asset changes that
  preserve contracts. The maintainer chooses the final version and may select a
  larger bump, but tooling rejects a smaller one. Field-development release IDs
  remain content/operation based and do not consume SemVer versions.
- **Q125 — One connected-system field-readiness command:** Settled by delegated
  authority. Add read-only `iii field check` for the currently connected
  `iii.local` plus local GC. Report commissioning/field overlay, paired GC/drone
  release compatibility, clock gate, credentials, storage/reserves, receiver/
  runtime health, hardware roles, PX4 firmware/required-parameter drift, III config
  reconciliation/pending cold restart, selected mission/classification, QGC version/
  managed-settings drift, logging/rosbag capacity, backup freshness, and prepared-
  offline cache/recovery assets. Produce a concise pass/warn/fail summary and sealed
  readiness record; never mutate or arm anything. Every finding supplies Q112 next
  commands. This is the canonical connected-system pre-field checklist, but its
  record is evidence rather than a reusable authorization token: every later
  safety-sensitive operation still validates live state.
- **Q126 — Field-readiness severity and enforcement:** Settled by delegated
  authority. Use deterministic policy-derived `PASS`, `WARN`, and `FAIL` findings
  with stable finding IDs. `FAIL` covers conditions that make the declared field
  workflow unsafe or non-recoverable: invalid commissioning without an allowed
  field overlay, incompatible GC/drone components, invalid clock gate, unavailable
  receiver/control plane, required hardware or PX4 mismatch, unresolved parameter
  reconciliation/reintroduction, invalid selected mission, or violated storage/
  rollback reserve. `WARN` covers bounded non-safety deviations such as stale but
  still valid backup/evidence export, optional hardware absence, or offline cache
  age. The command always emits and seals its result; exit status distinguishes
  pass, warning, and failure. A runtime-only computer may produce a checksummed
  diagnostic record; release/commissioning evidence requires signature by an
  authorized field-signing key. Warnings may be acknowledged with rationale in a
  new signed readiness record, but acknowledgement never changes a finding's severity
  or bypasses a live safety gate. Failures cannot be waived by readiness tooling.
- **Q127 — Withdrawal of a previously qualified release:** Settled by delegated
  authority. Never delete, replace, or move a qualified tag, manifest, signature,
  or GitHub Release. Publish an append-only signed release-status statement that
  identifies the exact qualified release and classifies it as `qualified`,
  `withdrawn`, or `unsafe`, with reason, timestamp, superseding version when known,
  and signing authority. `iii field prepare` and online release operations refresh
  and cache the signed status index; offline operation uses the newest verified
  cached index and visibly reports its age without imposing an expiry that would
  strand field operation. Status publication is an emergency-capable but narrow
  protected workflow running trusted code from workspace `release`: `iii release
  status set` plans an exact monotonic transition, and explicit apply dispatches a
  concurrency-locked environment-protected job that signs and publishes an
  immutable `iii-status-<sequence>` GitHub Release/index snapshot. Its non-SemVer
  protected tag points at the trusted workflow revision and never triggers a bundle
  build. Inputs cannot alter code, artifacts, prior statements, or arbitrary refs.
  `withdrawn` blocks new download/install/activation but
  does not trigger an automatic switch. `unsafe` additionally invalidates
  commissioning and blocks flight-capable operation. Revocation never switches
  code while the aircraft may be operating. An unsafe installed release remains
  immutable for diagnostics and may be used by the onboard receiver only as a
  maintenance-safe last-resort control-plane recovery when no non-unsafe accepted
  release can recover the target; that exceptional state blocks flight and requires
  an immediate qualified replacement or reprovision. Status signing and CI bundle
  signing are independently rotatable; a compromised release-status key is removed
  from trust through explicit host maintenance and recommissioning.
- **Q128 — Complete operator-credential or workstation loss:** Settled by delegated
  authority. If any authorized operator computer remains, generate fresh SSH and
  field-signing keys on the replacement computer, enroll their public keys through
  `iii access enroll`, verify access, and revoke lost keys. Never copy private keys.
  Losing only a field-signing key does not require reimaging when authorized SSH
  access remains. If every authorized SSH credential is lost, provide no password,
  default account, hidden token, boot-partition key injection, or receiver bypass:
  power off and remove the SD card, use a safe read-only `iii host salvage` flow to
  extract declared portable state when the media remains readable, then require
  physical SD reimage, Ansible reprovisioning, fresh key enrollment, portable-state
  restore, and recommissioning. Salvage never modifies, boots, or injects access
  into the old installation and is not a credential-recovery mechanism. A lost
  GC/workstation is rebuilt
  from stock supported Ubuntu plus the repository, regenerated machine identity,
  fresh keys, verified imported records/caches, and a new provisioning record.
- **Q129 — Disaster recovery for local evidence and operator state:** Settled by
  delegated authority. Treat Git-ignored `.iii/` state as a schema-versioned local
  registry whose durable domains include content-addressed release caches,
  operation records, captures, backups, commissioning/readiness records, release
  evidence, status indexes, and import/export receipts. Add `iii records
  inventory/verify/archive/import` to create deterministic checksummed portable
  archives for user-managed external/offline storage. Archives preserve provenance
  and cross-domain references, support incremental content reuse, never auto-prune,
  and clearly report omitted or unavailable bulky content. Exclude all SSH/signing
  private keys, runtime credentials, Wi-Fi secrets, and machine identity. Repository
  state and compact GitHub attestations are not substitutes for bulky local evidence;
  field readiness warns when irreplaceable records have no verified external archive.
- **Q130 — Codex-intended automation-ready documentation:** Settled by delegated
  authority. Migrate the workspace and every editable III repository's maintained
  documentation into one indexed, versioned operating manual usable by humans,
  Codex/AI agents, local automation, and CI. Exclude third-party, generated,
  vendored, dependency, historical-evidence, and build-artifact documentation from
  rewriting. There is no separate agent-only operational truth: docs and `AGENTS.md`
  route to the same canonical commands and policy schemas. Every runnable workflow
  states purpose, authority boundary, prerequisites, supported profiles/hosts,
  plan/dry-run, exact mutation, structured output and exit statuses, evidence,
  interruption/resume semantics, rollback/recovery, and Q112 next commands. Branch
  and CI docs encode the exact feature -> `develop` -> verified mechanical promotion
  -> `main` -> workspace `release` -> immutable tag chain, strict submodule lock and
  linked-PR policy, local evidence handoff, waiver limits, solo-maintainer rulesets,
  and AI-safe/non-interactive contracts. Generated command/schema references come
  from implementation metadata; documentation validation checks links, command
  existence/help, stale branch names, forbidden legacy paths, ownership, and release
  inclusion. Documentation changes follow the same branch hygiene as code and are
  a required qualified-release input, not a post-release cleanup.
- **Q131 — Final cutover and legacy-retirement gate:** Settled by delegated
  authority. Retire the legacy repository and old CLI paths only after one exact
  candidate passes the complete signed acceptance matrix: clean GC provisioning;
  raw verified SD imaging and interrupted/resumed aircraft provisioning; Ansible
  idempotence and offline reconvergence; qualified release creation from the
  protected release chain; dirty/untracked field build from the GC; component-aware
  GC/drone deployment; activation, client-loss, power-loss, health failure, and
  automatic rollback; receiver self-update failure recovery; real/OptiTrack profile
  switching; native QGC plus PX4 inventory; parameter add/remove/reintroduction,
  live tuning, arbitrary capture, and promotion; log/diagnostic pull and receipt-
  based prune; key enrollment/revocation and replacement-computer recovery; host
  backup, destructive reimage, restore, and recommissioning; signed field readiness;
  and a fully offline prepared field cycle. The matrix must be executable from the
  automation-ready runbooks, retain machine-verifiable evidence, and prove that no
  supported path needs the old repository, onboard Docker, password SSH, source
  synchronization, or direct aircraft filesystem mutation. Archive the legacy
  repository read-only with a pointer to replacement commands; do not delete its
  history.
- **Q132 — Deployment-redesign scope closure:** Settled by delegated authority.
  The architecture is closed for implementation after the integrity sweep in this
  backlog. Future source-repository-independent field operation, actual split-host
  HIL, profile-specific OptiTrack integration, multiple tracked parameter variants,
  delta transfer, TLS/private-CA operation, and fleet/per-aircraft identity remain
  explicitly deferred. This sweep may add only the schemas and boundaries already
  specified for those futures. Implementation discoveries that contradict a
  load-bearing decision require an ADR/backlog amendment; ordinary technical choices
  may proceed within these contracts without reopening the design interview.

### Resolved operational policy defaults

The following defaults close previously qualitative terms such as `fresh`,
`bounded`, `short-lived`, and `periodic`. They live in versioned policy/manifests,
are printed by plans and readiness output, and may be tightened by a release or host
profile. They cannot be weakened below these floors by an ad hoc CLI flag:

- **Safety telemetry (Q12/Q43):** activation preflight requires PX4 telemetry no
  older than one second and continuously landed/disarmed/no-reference-owner for
  three seconds before acquiring the mutation lock. Candidate acceptance still
  requires Q43's independent ten-second stable window.
- **Receiver self-update (Q32):** the new receiver has 30 monotonic seconds after
  selector switch to start, reopen its socket, pass self-tests, and prove journal/
  retained-release compatibility before the bootstrap restores the old slot.
- **Network mutation (Q36):** preserve Ethernet DHCP throughout; a changed network
  profile has 90 monotonic seconds to reconnect and receive authenticated
  confirmation before onboard automatic reversion.
- **Pre-clock buffering and retained sessions (Q39/Q59):** the degraded-clock ring
  holds at most 10,000 records or 16 MiB, whichever comes first, drops oldest first,
  and records a loss counter. Normal retention always protects the current plus four
  newest completed runtime sessions. Debug output has a 256 MiB per-session ceiling
  inside the existing global 14-day/1-GiB-or-5-percent cap.
- **Field signer unlock (Q47):** the host signing agent defaults to eight hours,
  never exceeds 24 hours, forgets decrypted key material on logout/reboot/explicit
  lock, and never exposes the private key to builder containers.
- **Partial upload cleanup (Q51):** verified resumable partials expire after seven
  days of inactivity unless referenced by an active operation; cleanup never uses
  wall time alone when the target clock is untrusted.
- **Clock trust (Q59/Q60):** boot synchronization uses at least five samples,
  rejects samples with round-trip time above 500 ms, and must verify absolute offset
  at or below 250 ms before opening the gate. Afterward, time synchronization is
  slew-only. A detected backward discontinuity above 250 ms or forward discontinuity
  above two seconds triggers Q60's maintenance-safe transition to
  `DEGRADED_CLOCK`, or `CLOCK_FAULT_ACTIVE` until maintenance-safe if flight/control
  is active. Active manual measurement warns above 250 ms and requires
  stop/gate/resync above one second. Runtime ordering never uses wall time.
- **PX4 mirror (Q66):** parameter events trigger a two-second-debounced full-set
  reconciliation; while connected and disarmed, a complete reconciliation also runs
  every 60 seconds and once at clean session end. Armed/in-flight state is read-only
  and never starts a bulk parameter transfer.
- **GC artifact cache (Q71/Q73):** default non-protected cache quota is 50 GiB with
  at least 10 GiB or 10 percent filesystem free, whichever is greater. Prepared
  offline sets, installed slots, evidence, captures, keys, and provisioning inputs
  are protected rather than silently evicted; preparation fails with a size report
  when they cannot fit.
- **Backup freshness (Q105/Q108):** freshness is state-based, not merely age-based:
  a backup is fresh only if its sealed persistent-state generation still matches the
  target and no declared invalidating mutation has occurred. Readiness additionally
  warns after 30 days without a newly verified external archive even when state is
  unchanged.
- **Mutation authorization (Q115):** receiver nonces expire after five monotonic
  minutes, are single-use, client/operation/state-bound, and are consumed atomically
  at mutation-lock acquisition.
- **Bundle extraction (Q116):** each component archive permits at most 20 GiB
  unpacked content, 200,000 entries, path length 255 bytes, and path depth 32. The
  signed manifest declares tighter actual limits; streaming extraction enforces both
  declared and host limits before committing any privileged path.
- **Readiness/status age (Q125–Q127):** readiness identity becomes stale immediately
  when boot ID, release, profile, configuration, commissioning, PX4-required state,
  mission, GC/QGC pair, or policy hash changes. It is evidence, never authorization.
  A verified release-status index older than seven days is a warning while offline,
  never an automatic expiry; online preparation must refresh it or fail visibly.

### Decision-to-implementation coverage index

Every independent normative clause in each decision is an acceptance obligation,
not merely its title. P5.T0 materializes clauses as stable `Q<question>.c<clause>`
matrix rows and refuses completion when a clause lacks an owning task and test/
evidence path. The focused owners below implement and test the decision; P5.T0
additionally verifies traceability and P5.T1/Q131 exercise the physical end-to-end
subset. This index is normative and must be updated in the same change as any
decision or task renumbering.

| Decision | Focused implementation owners |
|---|---|
| Q1 | P0.T2, P3.T5, P5.T1 |
| Q2 | P1.T0, P3.T0, P3.T1, P5.T1 |
| Q3 | P1.T2, P3.T1, P3.T2, P5.T6 |
| Q4 | P3.T0, P3.T1, P5.T1 |
| Q5 | P1.T2, P2.T6, P3.T1, P3.T8, P5.T0 |
| Q6 | P0.T2, P3.T3, P3.T5 |
| Q7 | P2.T5, P3.T3, P5.T3 |
| Q8 | P2.T5, P3.T3, P3.T8 |
| Q9 | P2.T2, P3.T1, P5.T1 |
| Q10 | P1.T3, P2.T0, P2.T2 |
| Q11 | P1.T3, P2.T2, P3.T3 |
| Q12 | P2.T1, P2.T4, P3.T9 |
| Q13 | P2.T0, P3.T9 |
| Q14 | P0.T5, P0.T9, P1.T4 |
| Q15 | P0.T5, P0.T6, P0.T7, P0.T8 |
| Q16 | P0.T6, P0.T7, P0.T8, P0.T11 |
| Q17 | P1.T4 |
| Q18 | P1.T4, P2.T6 |
| Q19 | P1.T4, P3.T1, P3.T4 |
| Q20 | P0.T2, P3.T10 |
| Q21 | P3.T4, P3.T11 |
| Q22 | P2.T0, P2.T2, P2.T4, P4.T1 |
| Q23 | P4.T2 |
| Q24 | P4.T1, P4.T3 |
| Q25 | P4.T1 |
| Q26 | P2.T6, P2.T8, P4.T1 |
| Q27 | P0.T8, P4.T4 |
| Q28 | P4.T0, P4.T4 |
| Q29 | P4.T3, P4.T4 |
| Q30 | P2.T2, P2.T4, P2.T5, P5.T1 |
| Q31 | P2.T2, P2.T5 |
| Q32 | P2.T3, P3.T1 |
| Q33 | P2.T0, P2.T7 |
| Q34 | P3.T5, P5.T1 |
| Q35 | P3.T4, P3.T6, P5.T1 |
| Q36 | P3.T0, P3.T7 |
| Q37 | P2.T5, P3.T1, P3.T2, P3.T8 |
| Q38 | P2.T2, P3.T1, P3.T2, P3.T3 |
| Q39 | P2.T7 |
| Q40 | P2.T7, P2.T8 |
| Q41 | P3.T2, P5.T1 |
| Q42 | P2.T6, P3.T2 |
| Q43 | P2.T4 |
| Q44 | P1.T1, P1.T2, P1.T3, P2.T6 |
| Q45 | P1.T0, P1.T2 |
| Q46 | P1.T1 |
| Q47 | P1.T3, P3.T3, P3.T8 |
| Q48 | P0.T9, P1.T4 |
| Q49 | P2.T3, P2.T4 |
| Q50 | P1.T0, P1.T2, P3.T8 |
| Q51 | P1.T3, P2.T5 |
| Q52 | P3.T8, P4.T2, P4.T3 |
| Q53 | P2.T6, P4.T2 |
| Q54 | P4.T3 |
| Q55 | P4.T2, P4.T3 |
| Q56 | P2.T8, P4.T3 |
| Q57 | P2.T6, P3.T8, P5.T3 |
| Q58 | P0.T2, P1.T3, P2.T6, P3.T7 |
| Q59 | P2.T6, P2.T7, P3.T1, P3.T2, P3.T8 |
| Q60 | P2.T6, P2.T7, P3.T1, P3.T2 |
| Q61 | P3.T8, P3.T9 |
| Q62 | P1.T3, P2.T6, P3.T9 |
| Q63 | P3.T8, P3.T9 |
| Q64 | P3.T9, P3.T10 |
| Q65 | P1.T3, P3.T10 |
| Q66 | P2.T8, P3.T10 |
| Q67 | P2.T8, P3.T10, P4.T4 |
| Q68 | P3.T8 |
| Q69 | P3.T3, P3.T8 |
| Q70 | P1.T1, P2.T6, P3.T9 |
| Q71 | P2.T0, P3.T9 |
| Q72 | P2.T1, P3.T9 |
| Q73 | P1.T0, P1.T2, P2.T6, P3.T8, P3.T9, P5.T0 |
| Q74 | P0.T2, P1.T3, P1.T5, P2.T6, P5.T0 |
| Q75 | P1.T3, P1.T5, P2.T1 |
| Q76 | P4.T0, P4.T1 |
| Q77 | P4.T1 |
| Q78 | P1.T5, P4.T0 |
| Q79 | P4.T0, P4.T1 |
| Q80 | P4.T1, P4.T3 |
| Q81 | P1.T5 |
| Q82 | P1.T5 |
| Q83 | P1.T5, P2.T1, P2.T6 |
| Q84 | P1.T5 |
| Q85 | P1.T3, P1.T5 |
| Q86 | P0.T2, P1.T3, P2.T6 |
| Q87 | P1.T5, P2.T1, P2.T6 |
| Q88 | P1.T5, P2.T6 |
| Q89 | P1.T1, P2.T6, P2.T8 |
| Q90 | P2.T6, P2.T8 |
| Q91 | P0.T3, P2.T0, P2.T2, P3.T1 |
| Q92 | P1.T2, P1.T3 |
| Q93 | P1.T0, P1.T2, P3.T1 |
| Q94 | P1.T2 |
| Q95 | P1.T2 |
| Q96 | P2.T2, P3.T2 |
| Q97 | P2.T4 |
| Q98 | P0.T2, P2.T6, P4.T0 |
| Q99 | P0.T2, P1.T3, P2.T6, P3.T7, P3.T10 |
| Q100 | P0.T0 |
| Q101 | P3.T0, P3.T1 |
| Q102 | P3.T0, P3.T11 |
| Q103 | P3.T0, P3.T11 |
| Q104 | P2.T8, P3.T11 |
| Q105 | P2.T8, P3.T4, P3.T11 |
| Q106 | P2.T2, P3.T11, P4.T2 |
| Q107 | P3.T0, P3.T1 |
| Q108 | P3.T1, P3.T5, P3.T6, P3.T9, P3.T10, P5.T1 |
| Q109 | P2.T8, P5.T1 |
| Q110 | P3.T4, P5.T1 |
| Q111 | P3.T5, P5.T1 |
| Q112 | P0.T11, P0.T12, P5.T2 |
| Q113 | P0.T12, P2.T2, P2.T3, P2.T6 |
| Q114 | P2.T2, P2.T6 |
| Q115 | P0.T2, P2.T2 |
| Q116 | P1.T3 |
| Q117 | P1.T4 |
| Q118 | P0.T5, P1.T4, P5.T0 |
| Q119 | P1.T4, P2.T8 |
| Q120 | P0.T5, P0.T6, P1.T4 |
| Q121 | P0.T5, P1.T4, P5.T0 |
| Q122 | P0.T5, P1.T4, P5.T0 |
| Q123 | P1.T4 |
| Q124 | P0.T9, P1.T4 |
| Q125 | P2.T6, P5.T0 |
| Q126 | P0.T12, P2.T6, P5.T0 |
| Q127 | P0.T2, P1.T4, P2.T0, P2.T6, P3.T4, P5.T0 |
| Q128 | P2.T5, P3.T3, P3.T8, P3.T11, P5.T0 |
| Q129 | P2.T8, P5.T0 |
| Q130 | P0.T11, P0.T12, P5.T2, P5.T3, P5.T4, P5.T5 |
| Q131 | P0.T4, P5.T0, P5.T1, P5.T6 |
| Q132 | P0.T0, P5.T0 |

### Execution and completion contract

Phase numbers express architectural layering, not a strict waterfall. P0 contract
tasks gate any dependent implementation. P5.T0 and P5.T2 are bootstrap work: the
clause-level verification matrix and documentation ownership/validation framework
must exist before an implementation task can be marked complete. P1 artifact work,
P2 receiver/CLI work, P3 host/GC provisioning, and P4 configuration work may then
proceed in parallel only where their referenced contracts are already fixed.

A task is implementation-ready when every predecessor named by its phase delivery
order or acceptance criteria has a fixed contract, its owning repository is known,
and its required test environment is available or explicitly represented by a
scripted signed local/physical acceptance row. A task is complete only when all of
its acceptance items pass, its tests and matrix rows retain evidence, affected
canonical documentation is updated, dependency-lock/submodule policy is satisfied,
and no work is hidden in TODOs or deferred acceptance. Partial implementation keeps
the task In-Progress; it does not create an implicit follow-up task.

## Incomplete

### P0: Resolve Architecture And Contracts

Phase acceptance:

- [x] Every open decision that affects implementation has an agreed answer.
- [x] Domain terms and architecture decisions are recorded without duplicating
      runtime ownership already defined by the existing ADRs.
- [x] Release, configuration, safety, and recovery contracts are concrete enough
      for later tasks to be implemented independently.

Delivery order:

1. Record the closed decisions as domain language, ADRs, and contracts in P0.T1–P0.T3.
2. Implement governance policy primitives and audits in P0.T5–P0.T12; these may
   proceed in parallel once manifest and branch contracts are fixed.
3. Complete P0.T4's retirement mapping before replacement implementation, but do
   not archive or remove anything until Q131 and all P5 acceptance gates pass.

#### P0.T1: Record Deployment Domain Language And ADRs

**Status: Completed (2026-08-25).** Added the indexed deployment bounded context
and ADRs 0006–0009. Verification: required-term search passed and the deployment
contract/documentation suite passed (18 tests).

Description:
Add deployment terms to the workspace domain documentation and record decisions
whose rationale must survive future architecture reviews. At minimum define
qualified release, field-development release, aircraft configuration, tuning
session, activation, acceptance, and rollback.

Acceptance:

- [x] `CONTEXT.md` or an indexed deployment `CONTEXT.md` defines stable terms.
- [x] ADRs record repository ownership, offboard-only builds, immutable release
      activation, and persistent configuration separation where warranted.
- [x] ADRs record release-status withdrawal, total-credential-loss recovery,
      portable local-record archives, readiness semantics, and final cutover.
- [x] Existing Operations Interface and runtime ownership ADRs are not reopened.

Tests:

- `rg -n "Qualified Release|Field-Development Release|Tuning Session" CONTEXT.md deployment docs`

#### P0.T2: Specify Release And Compatibility Manifests

**Status: Completed (2026-08-25).** Added strict v1 release, release-status,
operational-policy, and portable-record schemas; qualification, target
compatibility, monotonic withdrawal, content identity, and anti-weakening APIs;
and clean/dirty/incompatible/incomplete/tampered fixtures. Verification: JSON
syntax validation and deployment suite passed (30 tests).

Description:
Define versioned schemas for native release identity, source provenance, target
platform, dependency lock, toolchain, included packages, checksums, configuration
schema compatibility, PX4 compatibility, and qualification evidence. Define
which fields are required for qualified versus field-development releases.

Acceptance:

- [x] Manifests identify clean, dirty, modified-submodule, and untracked-source states.
- [x] Manifest classification refuses qualified status unless branch, clean
      state, lock, test evidence, version tag, signer, and explicit action all comply.
- [x] A target can reject an incompatible artifact before runtime shutdown.
- [x] A release and exported tuning capture can be correlated unambiguously.
- [x] The release-status schema is append-only, independently signed, bound to an
      exact qualified release, and represents `qualified`, `withdrawn`, and `unsafe`.
- [x] Readiness, commissioning, evidence, backup, capture, operation, and archive
      manifests use explicit schema versions and content identities that can be
      cross-referenced without embedding local absolute paths.
- [x] One versioned operational-policy schema carries every value in “Resolved
      operational policy defaults”; plans/results record its hash, reject unknown
      incompatible policy versions, and do not permit per-command weakening.
- [x] Manifest schemas reject unknown incompatible versions.

Tests:

- Schema validation fixtures for clean, dirty, incompatible, incomplete, and tampered manifests.

#### P0.T3: Specify Filesystem, Ownership, And Persistence Contracts

**Status: Completed (2026-08-25).** Added the versioned onboard filesystem,
ownership, persistence, unit, reserve, and fixed receiver-protocol contracts;
ROS-independent path/storage APIs; and packaged schema/policy assets. Verification:
temporary-root, hostile-input, reserve, import, and wheel-content checks passed
(36 tests plus isolated wheel inspection).

Description:
Finalize release, persistent data, secret, log, and transient paths; ownership;
permissions; retention; disk-space reservations; and behavior across activation,
rollback, OS reboot, and physical reimage.

Acceptance:

- [x] Production runtime has no dependency on `/home/iii/ws` or source paths.
- [x] Release activation cannot overwrite persistent aircraft configuration.
- [x] Secrets never enter release bundles or Git.
- [x] Disk exhaustion behavior is explicit and testable.
- [x] The `deployment/` Python distribution is ROS-independent, packages its
      schemas/declarative assets, and exposes one policy API consumed by CLI,
      receiver, Ansible/host tooling, tests, and CI without cyclic imports.
- [x] Privileged receiver entry points expose only fixed schema-validated actions;
      unprivileged CLI/build/archive code can be installed without root authority.

Tests:

- Filesystem contract test against a temporary target root.

#### P0.T4: Plan Legacy Deployment Retirement

**Status: Completed (2026-08-25).** Inventoried legacy commit
`4ab4ae76013ba3ff904189777e7c97af107d94e1`, preserved non-authoritative USB
observations, mapped every behavior to its replacement or explicit rejection,
and documented the signed Q131 archive gate. No archival mutation was performed.
Verification: retirement and documentation contract suite passed (38 tests);
remaining active setup-variable removal is owned by P5.T6 cutover.

Description:
Inventory any remaining authoritative data in `III-Drone-deployment`, migrate
the udev mappings and relevant historical knowledge, mark the repository
retired, and define when it can be archived. Do not carry forward its Compose
runtime or mutable workspace synchronization.

Acceptance:

- [x] Every retained legacy behavior has an explicit destination and rationale.
- [x] No current documentation directs operators to the legacy repository.
- [x] Archival occurs only after replacement provisioning and field update paths pass acceptance.

Tests:

- `rg -n "III-Drone-deployment|docker-compose.yml" README.md docs setup scripts tools deployment`

#### P0.T5: Implement Required Promotion-Source CI

**Status: Completed (2026-08-25).** Added the versioned branch and change-impact
policies, exact source/base and mechanical-diff gate, governed source-content
identity, Ed25519 evidence verification, waiver limits, and the required
least-privilege CI job. Release maps governed submodules to `main`; no submodule
`release` branch is assumed. Verification: policy/signature/waiver fixtures,
workflow YAML validation, and deployment suite passed (51 tests).

Description:
Add a required CI job that validates allowed source/base combinations and the
content provenance of mechanical promotion branches. Permit feature/work-sweep
branches into `develop`, only verified `promote/develop-to-main/*` branches into
`main`, and only `main` into workspace `release`. Prove that a main promotion
was cut from `develop` and differs only by expected post-submodule-merge gitlink
and lock refresh. Remove stale `staging` assumptions and map workspace release
checks to III submodule `main` rather than nonexistent submodule release branches.

Acceptance:

- [x] Feature work cannot target `main` or `release` directly.
- [x] Main promotion accepts only a verified develop-derived mechanical
      promotion branch needed for submodule gitlink refresh.
- [x] Develop-to-main promotion cannot merge without Q118's valid signed local
      simulation/physical evidence for the tested source-content identity.
- [x] Required evidence categories are derived and explained by the final Q121
      versioned impact policy; reductions require a signed retained waiver.
- [x] Release accepts only workspace `main`, and no release branch is required
      in each submodule repository.
- [x] Release PR checks compare governed III gitlinks with exact submodule
      `origin/main` heads and verify the dependency lock.

Tests:

- CI fixtures for allowed feature-to-develop, develop-derived promotion-to-main,
  main-to-release, and rejected source/base combinations.

#### P0.T6: Protect Workspace Integration And Release Branches

**Status: Completed.**

Description:
Create active GitHub rulesets for workspace `develop`, `main`, and the new
long-lived `release` branch. Require pull requests, the settled approval policy,
resolved review threads, promotion-source CI, dependency-governance checks, and
the applicable III test suite. Prevent deletion and non-fast-forward updates;
define narrowly justified bypass actors, if any.

Acceptance:

- [x] `develop`, `main`, and `release` rulesets are active and target only their
      intended refs.
- [x] Required checks block merge when pending, failing, or absent.
- [x] Workspace `main` requires the promotion-evidence check; workspace `release`
      requires proof-based evidence reuse or recollection according to Q120.
- [x] Direct pushes, deletion, and force-pushes are blocked according to policy.
- [x] The rulesets contain no undocumented bypass.

Tests:

- Read-only GitHub ruleset audit plus controlled allowed/rejected test PRs.

Implementation note (2026-08-25): workspace PRs #29 and #30 passed every
required develop gate. Live reconciliation reports all three desired rulesets
unchanged, the rules contain no bypass actors, and an up-to-date direct push to
`develop` was rejected by GH013 because a PR and six required checks were
required.

#### P0.T7: Protect Editable III Submodule Branches

**Status: Completed.**

Description:
Create matching active GitHub rulesets for `develop` and `main` in each editable
`src/III-*` and `tools/III-*` repository. Require feature-to-develop and verified
develop-derived promotion-to-main pull requests, applicable package checks, and
the settled review policy. Do not create submodule `release` branches.

Acceptance:

- [x] Every editable III repository has active `develop` and `main` protection.
- [x] Direct pushes, deletion, force-pushes, and invalid promotion sources are blocked.
- [x] Workspace linked-PR checks and submodule rulesets agree on target branches.
- [x] Third-party and forked repositories are not mutated by this task.

Tests:

- Organization-wide read-only ruleset audit and representative controlled PR checks.

Implementation note (2026-08-25): the rollout uses target-specific required
contexts (`promotion-source-develop` and `promotion-source-main`) so a successful
check attached to one commit on a develop-targeting PR cannot be reused by a
main-targeting PR. The original context remains as a non-required compatibility
check during rollout. Twenty allowed submodule PRs passed and merged across the
two rollout passes. Configuration PR #14 was then updated to current `main`,
failed `promotion-source-main`, was reported `BLOCKED`, and was closed without
merge. All 20 submodule rulesets reconcile unchanged and no submodule `release`
branch exists.

#### P0.T8: Update Coordinated Promotion Automation

**Status: Completed (2026-08-26).**

Description:
Update `scripts/git/create_stack_prs.sh`,
`scripts/git/create_develop_to_main_prs.sh`, pointer-refresh scripts, manual
GitHub workflow inputs, and documentation to use the agreed
feature -> develop -> verified promotion -> main flow. Give mechanical branches
the `promote/develop-to-main/*` namespace and make their allowed diff auditable.
Add a main-to-release helper or documented direct PR workflow that cannot carry
release-only implementation changes.

Acceptance:

- [x] Dry-run remains the default for local automation that pushes or opens PRs.
- [x] Main promotion creates/updates linked submodule PRs, refreshes resulting
      main gitlinks and the lock, and satisfies P0.T5.
- [x] Main-to-release promotion carries exactly the main workspace state.
- [x] Old `staging` and ambiguous `release/develop-to-main-*` terminology is removed.

Tests:

- Shell tests with fake Git/GitHub adapters for feature, main-promotion, pointer
  refresh, rerun/idempotence, and main-to-release flows.

Implementation note (2026-08-26): the fixed
`promote/develop-to-main/<promotion-id>` prepare/refresh lifecycle now defaults
to a mutation-free dry run, updates linked submodule pull requests, pins exact
merged `main` heads, and refreshes and verifies the workspace lock. The
workspace-only `main -> release` helper is idempotent and cannot carry
release-only implementation changes. Promotion-source validation audits the
mechanical candidate against `develop` while retaining the actual base-to-head
impact identity. Fake Git/GitHub adapter coverage includes reruns and invalid
namespaces; 63 deployment tests passed. Ten Node 24 submodule workflow updates
and workspace PR #31 passed the full protected stack and merged.

#### P0.T9: Protect Qualified Tags And Release Entry Points

**Status: Completed (2026-08-26).**

Description:
Define immutable `vX.Y.Z` tag rules and enforce that qualified release builds
and deploy actions accept only clean tagged commits reachable from workspace
`release`. Field-development builds remain available from any branch and dirty
state but cannot request, imitate, or replace qualified status.

Acceptance:

- [x] Qualified tags cannot be moved or deleted through normal developer access.
- [x] A tag outside workspace `release` cannot produce a qualified manifest.
- [x] Missing/dirty/lock-divergent/test-incomplete release state fails closed.
- [x] Only an accepted qualified deployment replaces the protected recovery anchor.

Tests:

- Qualification fixtures for valid release tags and invalid branch, dirty,
  moved-tag, lock, evidence, and field-release cases.

Implementation note (2026-08-26): workspace PR #32 added a versioned complete
qualification-evidence contract, dry-run-by-default strict-SemVer publisher,
independent tag-build preflight, qualified-manifest binding, and fail-closed
protected-anchor selection. The publisher checks the live remote `release` head
and refuses reused tags. All 72 deployment tests and protected PR gates passed.
Live ruleset 21496168 is active for `refs/tags/v*`, blocks deletion and
non-fast-forward changes, and has zero bypass actors.

#### P0.T10: Audit Governance Enforcement

**Status: Completed (2026-08-26).**

Description:
Add a read-only audit command or script that compares repository policy with the
actual GitHub rulesets for the workspace and editable III submodule repositories.
Run it during release preparation and document remediation for drift.

Acceptance:

- [x] Audit reports missing branches, rulesets, required checks, source gates,
      tag protection, and unexpected bypass actors.
- [x] Output is concise enough to retain as qualified-release evidence.

Tests:

- Read-only GitHub ruleset audit script verifies expected enforcement across
  the workspace and editable III submodule repositories.

Implementation note (2026-08-26): added a pagination-safe read-only GitHub
adapter, exact desired/live comparison, stable drift IDs and exit codes, compact
content-addressed JSON evidence, human output, and reviewed remediation. Release
publication reruns and embeds the audit. Fake-client drift fixtures cover missing
branches/rulesets, missing target-specific source checks, tag weakening,
unexpected rulesets, and bypass insertion. The live audit passed across all 11
repositories with all 24 rulesets observed and zero findings.

#### P0.T11: Define CI/CD And AI Automation Contracts

**Status: Completed (2026-08-26).**

Description:
Define one composable automation contract for feature PR creation, stacked
submodule PR orchestration, develop-to-main promotion, main-to-release
promotion, qualification, artifact retrieval, and deployment handoff. Commands
must work for humans, CI jobs, and AI agents without parsing decorative terminal
text or relying on an interactive shell. Separate planning, policy validation,
mutation, and reporting so every write is previewable and attributable.

Acceptance:

- [x] Every operation supports non-interactive invocation, preflight/dry-run,
      stable exit codes, and versioned structured output in addition to concise
      human output.
- [x] Every result exposes Q112-compliant context-aware next commands in human and
      structured output without bypassing mutation planning or confirmation.
- [x] Plans identify exact repositories, refs, expected old/new SHAs, checks,
      permissions, and mutations before any push, PR, merge, tag, or publication.
- [x] Mutating runs have an operation ID, persist sufficient state to resume or
      safely retry, and report partial success with deterministic recovery steps.
- [x] Local and CI entry points call the same policy/build primitives rather
      than implementing branch or qualification rules twice.
- [x] GitHub workflows use least-privilege job permissions, pinned actions,
      concurrency controls, timeouts, immutable artifacts, and explicit trusted
      event boundaries for write-capable jobs.
- [x] AI-agent guidance states allowed operations, required checks, dirty-tree
      handling, submodule policy, and when explicit maintainer intent is needed.
- [x] PR bodies and workflow summaries carry machine-readable markers and
      human-readable evidence without allowing PR text to become trusted input.

Tests:

- Contract tests for JSON/schema output and exit codes; fake Git/GitHub tests
  for plan/apply, interrupted runs, retries, stale refs, partial submodule PR
  creation, permission denial, and untrusted PR metadata.

Implementation note (2026-08-26): added the versioned automation plan and
operation-state schemas plus a content-addressed planner/state engine covering
feature PRs, stacked PRs, both promotions, qualification, artifact retrieval,
and deployment handoff. Atomic persisted operations support dry-run, apply,
resume, retry, interruption, stale-ref rejection, partial success, permission
failure, and deterministic recovery actions through one structured/human result
contract. The trusted linked-submodule gate derives changed gitlinks and
repository ownership from the base commit, then verifies untrusted PR locators
against GitHub. Root and all ten editable III repositories now use pinned
actions, explicit least-privilege permissions, non-cancelling concurrency,
timeouts, immutable evidence, and trusted-event boundaries; PR text is never a
trusted policy input. Workspace PRs #34-#36 and ten submodule hardening PRs
merged through protected branches. Verification passed 98 deployment tests,
Python compilation, submodule-lock validation, every required GitHub check, and
the live 11-repository governance audit with 24/24 rulesets and zero findings.

#### P0.T12: Implement The Universal III CLI Result And Operation Contract

**Status: Completed (2026-08-26).**

Description:
Implement the P0.T11/Q112 contract once in `tools/III-Drone-CLI` and migrate every
existing and new `iii` command—not only deployment commands—to it. Introduce a
versioned result envelope containing command identity, outcome, stable finding/error
codes, operation ID/state where applicable, affected target/profile/release, evidence
references, and ordered `next_actions[]`. Render human output from the same envelope
so decorative text cannot disagree with JSON. Centralize operation-state storage,
Ctrl-C detach reporting, and exit-code mapping while allowing command-specific
payload schemas. Help and parser errors must use the same next-action mechanism.

Acceptance:

- [x] One result library and schema are used by `iii system`, build/deploy/release,
      host/GC/QGC/PX4, mission/config/capture/log/records, governance, field, and
      documentation commands; no command maintains a private incompatible envelope.
- [x] Every success, no-op, warning, rejection, failure, partial result,
      interruption/detach, cancellation, and help/parser result contains at least
      one valid context-aware next action or an explicit terminal-state reason when
      no command can follow.
- [x] Human `Next:` rendering and structured `next_actions[]` are generated from
      identical data and identify command, reason, mutation flag, prerequisites,
      target/profile/operation arguments, and confirmation requirements.
- [x] Stable exit-code families distinguish success, completed-with-warning,
      policy/safety rejection, execution failure, usage error, and internal error;
      accepted remote work interrupted by Ctrl-C reports conventional interruption
      while retaining operation ID and exact reattach command.
- [x] Structured output written to stdout is machine-clean; progress and diagnostics
      use declared stderr/event channels and never require ANSI/decorative parsing.
- [x] Non-interactive mode refuses prompts, returns a structured required-input
      finding, and never silently accepts a default for destructive or external
      mutation. Interactive and non-interactive paths invoke the same plan/policy.
- [x] Existing CLI commands are inventory-tested for coverage, including commands
      implemented in editable submodules and forwarded local/remote variants.

Tests:

- Result-schema golden tests, human/JSON equivalence, exit-code matrix, stdout
  cleanliness, help/parser failures, prompt refusal, next-action argument escaping,
  every-command inventory coverage, remote detach/reattach, and no-next-action
  terminal-state fixtures.

Implementation note (2026-08-26): `III-Drone-CLI` v0.2.0 now owns the sole
`iii.command-result/v1` implementation and result/plan/state schemas. Every
existing parser leaf is inventoried and dispatched through one runner; required
future command families are declared and agent guidance forbids private
envelopes. Human and JSON output share the same model, child-process stdout is
contained, diagnostics use stderr, and help/usage/setup/internal failures use
the same next-action invariant. Mutating commands receive content-addressed
dry-run plans, explicit non-interactive confirmation, atomic private operation
state, idempotent completed replay, content-identity/state-binding verification,
symlink rejection, Ctrl-C status 130, and exact argv-safe reattachment. The
deployment package pins CLI v0.2.0 and re-exports the exact canonical types while
keeping trusted policy-only imports frontend-independent. CLI PRs #11-#12 and
workspace PRs #37-#38 passed protected governance and merged. Final host and
Jazzy-devcontainer verification passed 101 deployment tests, 49 CLI tests,
Python compilation, isolated two-package installation, submodule lock, every CI
gate, and the live 11-repository audit with 24/24 rulesets and zero findings.

### P1: Build Reproducible Offboard Releases

Phase acceptance:

- [x] Neither qualified nor field-development release construction executes on an aircraft.
- [x] The same target contract drives laptop and CI builders.
- [x] Built output is immutable, complete, attributable, and target-compatible.
- [x] Once required caches exist, field-development releases can be built and
      packaged without internet access.

Phase verification (2026-08-27): P1.T0-P1.T5 are complete. Their retained tests
cover the canonical target/ABI contract in laptop and CI builders, dirty-source
capture, cached offboard ARM64 construction, immutable bundle verification,
qualified promotion without rebuilding, offline field construction, and installed
mission-catalog closure. Aircraft-side construction remains structurally absent.

Delivery order:

1. P1.T0 fixes the target ABI and host/release dependency boundary.
2. P1.T1 and P1.T2 may then proceed in parallel; both feed P1.T3.
3. P1.T5 may proceed once P0 manifests and package installation contracts are
   fixed, but its output must be consumed by P1.T3.
4. P1.T4 qualifies and publishes only the already-tested P1.T3 artifact format.

#### P1.T0: Establish The Canonical ARM64 Target Environment

**Status: Completed.**

Description:
Replace the Humble/Jazzy and host/sysroot ambiguity with one versioned target
definition covering Ubuntu, ROS, architecture, Python ABI, compiler, system
libraries, and hardware-specific dependencies. Generate or acquire the sysroot
without copying mutable state from an aircraft.

Acceptance:

- [x] `setup_real.bash`, build images, and manifest target metadata agree.
- [x] Builder inputs are digest-pinned and reproducible.
- [x] Target incompatibility is detected before transfer or activation.
- [x] The target definition distinguishes the final Q93 Ansible-owned host
      baseline from release-local dependencies and records compatible package/
      ABI constraints without requiring an aircraft-derived sysroot.

Tests:

- Build and run a target ABI probe in an ARM64 target-equivalent environment.

Implementation notes (2026-08-26):

- Added the content-addressed `iii.target-definition/v1` contract for Raspberry
  Pi 5 / Ubuntu 24.04 Noble / AArch64 / ROS Jazzy / CPython 3.12 cp312 /
  glibc 2.39 / GCC 13.3, including the Q93 Ansible host baseline and an
  explicitly disjoint release-owned dependency boundary.
- Replaced the legacy Humble and aircraft-derived sysroot flow with digest-
  pinned ROS ARM64 and Ubuntu amd64 OCI inputs, an Ubuntu snapshot, exact
  cross-toolchain package versions, and `/opt/iii/sysroot` generated solely
  from the immutable target seed.
- Added fail-closed target-definition identity, manifest derivation, ABI probe,
  and pre-transfer/pre-activation compatibility checks. Release manifests now
  bind the target-definition content ID.
- Reworked native real setup and both build entrypoints to use ROS Jazzy and
  `/opt/iii/current`, without sysroot impersonation or host executable symlinks.
- Verification: the real emulated ARM64 probe passed with AArch64/Noble/Jazzy,
  Python 3.12.3 cp312, glibc 2.39, and GCC 13.3.0 against platform digest
  `sha256:d849b6203853848bf20f5e5d6d77c1275bff1ff727d93ab055799cb33c2dac7a`;
  114/114 deployment tests passed on the host and in the ROS Jazzy
  devcontainer; shell/Python syntax, diff hygiene, and submodule lock passed.

#### P1.T1: Capture Dirty Workspace Provenance

**Status: Completed.**

Description:
Implement a source snapshot module that accounts for the top-level repository,
all governed submodules, modified tracked content, relevant untracked files,
ignored/generated exclusions, and the dependency lock. It must fail when source
classification is ambiguous rather than silently omit files.

Acceptance:

- [x] Identical source content yields the same content identity.
- [x] Modified submodules and untracked source change the identity.
- [x] Secrets, build trees, logs, datasets, and unrelated artifacts are excluded.
- [x] A human-readable provenance report accompanies every field-development release.
- [x] Change-impact analysis selects GC-only, drone-only, or paired artifacts from
      the governed dependency graph and explains every cause; unsafe manual
      component omission fails closed.

Tests:

- Automated fixtures for clean, tracked-dirty, submodule-dirty, untracked-source,
  ignored-secret, and generated-output workspaces.

Implementation notes (2026-08-26):

- Added a versioned source policy covering the workspace and all ten editable
  III repositories, relevant build/source roots, explicit sensitive/generated/
  dataset exclusions, and dependency-complete drone/GC impact rules.
- Added deterministic content capture for tracked bytes, deletions, safe
  symlinks, relevant non-ignored untracked source, governed submodule state,
  and the dependency lock. Commit and branch metadata remain attributable but
  do not perturb identity when the actual source bytes are identical.
- Added schema-validated machine snapshots, deterministic human-readable
  field-development provenance, manifest bindings for both artifacts, atomic
  output, explicit exclusion reporting, unmerged/unsafe-path rejection, and
  fail-closed manual component-selection validation with per-path causes.
- Verification: eight source fixture scenarios passed, including clean,
  tracked-dirty/deleted, submodule-dirty, untracked-source, ignored secret and
  generated output, unsafe symlink, deterministic identity, and paired impact;
  122/122 deployment tests passed on host and in the Jazzy devcontainer; a live
  11-repository dirty-workspace capture produced both artifacts and correctly
  selected paired drone+GC output; schemas, syntax, diff hygiene, and lock passed.

#### P1.T2: Implement Cached Cross-Compilation

**Status: Completed.**

Description:
Build III packages offboard using persistent compiler/build caches, package
change detection, and targeted colcon rebuilds while producing a complete
non-symlinked install tree suitable for immutable deployment. Preserve the
III-only test policy.

Acceptance:

- [x] No build action is sent to the aircraft.
- [x] Incremental field builds reuse caches without contaminating release output.
- [x] The output contains no absolute developer-workspace dependencies.
- [x] Release output follows the final Q92 colcon layout, includes a release-owned
      environment wrapper, resolves package data through ament, and contains no
      source/build/log tree or development symlink escaping the release root.
- [x] Failed partial builds cannot be packaged as complete releases.
- [x] All non-host runtime dependencies declared by Q93 are present inside the
      immutable release and verified without apt/pip/network access on target.
- [x] Python dependency packaging follows the final Q94 target-ABI, lock, hash,
      import-validation, and release-local path contract without embedding builder
      paths or requiring a relocatable virtualenv.
- [x] Native library closure follows the final Q95 host allowlist/bundling/RUNPATH
      contract and passes complete ELF dependency validation before packaging.

Tests:

- Clean build, no-change rebuild, single-package change, interface change with
  downstream rebuild, and deliberate compile failure.

Implementation notes (2026-08-26):

- Added schema-validated build policy and build-record contracts, an isolated
  offboard ARM64 colcon builder, content-bound package/downstream cache keys,
  persistent ccache/build state, immutable partial-to-complete promotion, and
  live source-recapture rejection. Normal builds use only local Docker and
  explicitly disallow target package-manager or aircraft build transports.
- The final release is a non-symlinked isolated `install/<package>` tree with a
  release-owned environment wrapper at canonical `/opt/iii/current`, no
  source/build/log tree, no escaping symlinks, and no absolute builder/sysroot
  paths. Package exports were made relocatable and production runtime log
  discovery now defaults to `/var/log/iii` rather than a workspace.
- `iwr6843aop_pub` now installs all ten radar profiles beneath its ament share,
  resolves the default profile through `ament_index_python`, and declares its
  NumPy/serial/ament runtime dependencies. The source snapshot policy captures
  this fork as a drone build input, and byte-identity validation proves installed
  package assets match governed source before the build source is removed.
- Added a hash-pinned CPython 3.12 ARM64 wheel lock and offline materializer.
  The builder verifies exact wheelhouse membership/hashes/tags, streams a plain
  release-local site-packages extraction, rejects path/collision/data-layout
  faults, and validates every declared import under the target image with the
  network disabled.
- Complete target-side ELF closure validates all 76 installed ELF objects through
  the release wrapper against the exact 148-SONAME host allowlist, bundled
  libraries, real resolved paths, and RUNPATH rules. Missing/unapproved SONAMEs,
  unresolved dependencies, builder paths, or network/package-manager reliance
  fail sealing.
- Acceptance evidence covers a clean build, a 7.31-second no-change rebuild with
  all eight III cache entries hit and zero compiler work, a runtime-only change,
  an Interfaces change rebuilding its downstream closure, and a deliberate Core
  compile failure that produced no build record or committed failed cache key.
  The final post-merge sealed builds are `7366c6628c080213f980f6505126e2911e3a6db3e806393014f8e8060321d336`
  and no-change `fd1247042889f9f0b66c04cc141d05c377eca498868e17bcd332a49202d68751`.
- Verification passed 141/141 deployment tests on both host and Jazzy
  devcontainer, 626/626 touched III package tests, the emulated target ABI probe,
  locked-wheel offline imports, installed ament lookup, exact asset/path/symlink
  audits, failure-path tests, diff hygiene, and the refreshed submodule lock.
  Owning PRs merged: Configuration #17, Core #58, Mission #12, Runtime #6,
  Supervision #13, and IWR fork #1.
- The 2026-09-04 field acceptance continuation added an explicit build resource
  contract. ARM64 colcon receives a bounded worker count and its Docker builder
  has a matching hard CPU quota; the GC release builder now uses the governed
  materialized source tree instead of sending the 275 GiB development workspace
  as Docker context, and its private BuildKit builder has the same quota. A
  no-change ARM64 rebuild completed with zero compiler cache misses, and live
  container inspection confirmed a 16-CPU quota while the host has 32 logical
  CPUs. Focused build/release coverage passed 26 cases after these changes.
- The final five-area R57 field build captured dirty source identity
  `50b5fac9d90a55b1b7d26a66f7200b70e6da33a343d1a70b270c7e29f0dee1cb`
  and produced ARM64 build
  `1965429b4c49950a44e0f3395e7d1c3c07b845a8477d487a7558d59c26933e89`.
  Fifteen target packages compiled successfully under the 16-of-32 CPU cap;
  ccache reported 540 direct hits and eight misses, and the emulated target
  smoke/closure checks passed. The paired GC build
  `0762111862540a32f90445c8953777a2106bcf2a18cca6186e43bd56c18088fe`
  reused the pinned QGroundControl 5.0.8 artifact and passed both OCI runtime
  smoke checks.

#### P1.T3: Package And Verify Release Bundles

**Status: Completed (2026-08-26).**

Description:
Produce paired but independently installable drone and GC artifacts under one
workspace release record. Package the native ARM64 install tree, installed runtime
assets and migrations for the drone; package pinned frontend/proxy images, CLI/
companion payloads, and host-native GC application metadata separately. Declare
exact API/schema compatibility ranges and checksums. Sign every bundle according
to the trusted-signer policy. Keep generated bundles and private signing keys
outside Git.

Acceptance:

- [x] Extraction recreates a complete release without source/build trees.
- [x] Corruption or manifest/content disagreement is detected.
- [x] Unsigned, unknown-signer, and invalid-signature bundles are rejected.
- [x] Signer add/list/revoke operations support key rotation without sharing
      private material or accidentally removing the final trusted signer.
- [x] Qualified and field-development release classes share one artifact format.
- [x] Paired GC/drone artifacts share release identity and compatibility evidence
      while remaining independently verifiable, installable, and rollbackable.
- [x] Release records bind exact real/sim PX4 parameter-manifest and managed QGC-
      settings hashes plus their compatible PX4/QGC versions.
- [x] Profile support is extensible release metadata rather than a hard-coded
      sim-local/real-remote dichotomy; uncommissioned profiles fail closed.
- [x] Bundle contents and compressed size are inspectable before deployment.
- [x] Bundle serialization, detached verification, extraction limits, path/link/
      special-file rejection, and schema versioning follow the final Q116 format;
      filenames and archive hooks are never trusted.
- [x] Streaming extraction enforces both manifest-declared and host ceilings of
      20 GiB unpacked data, 200,000 entries, 255-byte paths, and depth 32 before
      privileged commit; limit violations leave no staged release.

Tests:

- Round-trip package/extract/verify and tampered-content rejection.

Implementation notes (2026-08-26):

- Added canonical v1 release-component, detached-signature, public-signer, and
  trusted-signer schemas plus deterministic USTAR/Zstandard packaging with fixed
  `META/` documents, normalized headers, SHA-256 content indexing, exact paired
  payload/compatibility identities, and independently portable drone/GC component
  directories.
- Added streaming inspect/verify/extract transactions with detached Ed25519 trust
  validation, canonical frame/header enforcement, revalidation on the opened
  archive descriptor, signed/host ceilings, file-by-file hashing, link/special/
  traversal/duplicate/extra-entry rejection, atomic commit, and failure cleanup.
- Added proof-of-possession signer generation/add/list/revoke tooling with 0600
  private/store permissions, symlink resistance, authority separation, and final-
  active-signer protection. Canonical operator entrypoints refuse private keys and
  generated bundle sets inside the workspace.
- Extended release metadata with per-component targets, API/schema ranges, exact
  real/sim PX4 manifests, managed QGC compatibility, and extensible commissioned
  profile state. Documented the format and complete operator workflow.
- Verification passed 175/175 deployment tests on both host Python and the Jazzy
  devcontainer. The 34 bundle tests cover deterministic paired round trips,
  field/qualified parity, operator entrypoints, signer rotation/revocation,
  corruption and signed disagreement, unsafe archive forms, all four ceilings,
  no-staging failure behavior, PX4/QGC compatibility binding, and post-inspection
  replacement resistance. Schema loading, Python compilation, diff hygiene, and
  the submodule lock also passed.

#### P1.T4: Implement The Qualified Release CI/CD Pipeline

**Status: Completed (2026-08-26).**

Description:
Implement the settled qualified-artifact producer policy as a protected,
reproducible pipeline. Starting from an immutable `vX.Y.Z` tag on workspace
`release`, independently revalidate governance and source state, build/test the
ARM64 target and x86_64 GC application, assemble evidence, sign both paired
artifacts, publish them immutably, and make
it retrievable by the III CLI for local deployment to `iii.local`. A CI system
must never require network reachability to the aircraft.

Acceptance:

- [x] Qualification starts only from an immutable version tag whose commit is
      on workspace `release` and whose dependency state passes all policy gates.
- [x] The required `develop -> main` promotion-evidence check verifies Q118's
      signed local attestation against source-content identity and change-derived
      simulation/physical categories without running Gazebo or contacting an
      aircraft in GitHub-hosted CI; `main -> release` revalidates or recollects it
      according to the final Q120 reuse contract.
- [x] Build inputs, test results, manifests, checksums, signer identity, logs,
      and artifact identity are retained as one auditable release record.
- [x] Qualified payload reproducibility follows Q117's single clean pinned-build
      contract; optional rebuild comparison is diagnostic rather than a release gate.
- [x] Rerunning a version is idempotent and cannot silently replace a published
      artifact with different bytes.
- [x] Publication and signing jobs receive only the minimum required secrets
      and cannot run for untrusted pull-request code.
- [x] The dedicated Ed25519 CI key is isolated in a protected Actions
      environment, its public identity is included in evidence, and documented
      rotation/revocation does not require sharing private key material.
- [x] The III CLI can list, fetch, verify, cache, and deploy a qualified artifact
      while actual aircraft transfer remains a local operator action.
- [x] Publication maintains an append-only signed release-status index and can
      publish Q127 withdrawal/unsafe statements without mutating tags or assets.
- [x] `iii release status set` uses trusted `release`-branch workflow code,
      monotonic transition rules, protected environment authority, serialized
      sequence allocation, immutable non-SemVer `iii-status-<sequence>` publication,
      and cannot execute or sign user-supplied code/data outside the status schema.
- [x] `iii release list/show/fetch` verifies and reports cached release status;
      withdrawn releases cannot be newly fetched as deployable and unsafe status
      is never hidden by an offline or stale cache.
- [x] A version tag is only an immutable qualification attempt trigger, not proof
      of a qualified release. Failed qualification retains the protected tag and
      failure evidence, publishes no deployable release/artifact, marks that SemVer
      version unusable, and requires a new version for the next attempt.
- [x] Deployment-focused human/structured release notes follow the final Q123
      machine-derived contract and are identical in GitHub publication and
      `iii release show`, with prose unable to mask manifest facts.

Tests:

- Workflow tests for valid tag, wrong branch, stale policy, dirty/impossible
  source claim, failed ARM64 build/test, signing failure, duplicate version,
  tampered download, signed withdrawal/unsafe publication, stale/invalid status
  index, and successful offline deployment from a populated cache.

Implementation notes (2026-08-26):

- Added protected, tag-only qualification and serialized release-status workflows
  with pinned Actions, least-privilege job permissions, protected signer
  environments, authenticated promotion reuse, exact eight-check evidence, durable
  failed-attempt records, immutable draft-to-public publication, and trusted
  release-branch failure/status recorders.
- Added schema-validated qualification checks, GC build records, release records,
  signed publications, machine-derived notes, globally chained signed status
  statements/indexes, monotonic cache refresh, failed-version consumption, and
  exact-byte GitHub publication with resumable missing-asset completion.
- Added the ROS-free x86_64 GC builder with digest-pinned bases, hash-locked proxy
  dependencies, audit-clean frontend lock, one BuildKit solve feeding both the
  smoke-tested daemon image and retained OCI archive, platform/blob validation,
  and exact cleanup. A real two-image run passed and produced validated frontend
  and proxy OCI archives; a repeat build was retained only as diagnostic evidence,
  not used as a qualification gate.
- Added `iii release list/show/fetch/cache/verify/deploy/status set` through the
  canonical result/operation boundary. Remote and offline retrieval verifies the
  signed publication, audit record, paired bundles, and complete status chain;
  learned unsafe state cannot be downgraded by an older signed index.
- Verification passed 205/205 deployment tests and 57/57 CLI tests on the host,
  204/204 deployment and 57/57 CLI tests in the Jazzy devcontainer before the
  final additional build-record regression, 52/52 GC Python tests, 125/125 GC
  frontend tests, zero high-severity npm audit findings, frontend production
  build, Python compilation, actionlint, diff hygiene, and submodule-lock checks.
  Per maintainer direction, task-sized CLI #13 and GC #22 were withdrawn without
  branch deletion; their tested commits remain on the shared feature branches for
  inclusion in fewer, larger repository-level PRs.

#### P1.T5: Build And Validate Mission Catalog Artifacts

**Status: Completed.**

Description:
Turn release-approved mission specifications, behavior trees, models, and their
transitive references into a signed immutable catalog sub-artifact. Replace
source-tree/environment-expanded runtime paths with catalog-relative content IDs,
bind compatibility to behavior-node/interface/runtime identities, and classify
production, profile-limited, test, and legacy entries explicitly.

Acceptance:

- [x] Every catalog entry has stable logical name, content hash, schema, status,
      profile allowlist, compatibility hashes, and complete referenced assets.
- [x] Missions are declared through the final Q84 CMake registration contract;
      duplicate IDs, missing specifications, unknown profiles, missing
      classification, and incompatible profile defaults fail configuration/build.
- [x] Validation rejects missing/escaping/absolute references, duplicate IDs,
      malformed YAML/XML, unavailable behavior nodes/ports, incompatible contracts,
      and unclassified test/legacy content.
- [x] Behavior-node registration and the generated node/port model share one
      authoritative descriptor; validation fails if runtime registration, model
      generation, XML use, or port contracts diverge.
- [x] Qualified onboard allowlists contain only production missions; local-only
      test and legacy missions cannot enter any drone catalog.
- [x] Local catalogs retain all classified development entries, while drone
      catalogs contain only onboard-profile (`real`, `opti_track`, future `hil`)
      entries and always omit sim-only/test/legacy metadata and assets. The final
      Q87 contract governs explicitly onboard-compatible experimental entries.
- [x] Experimental entries pass production-grade validation, are impossible to
      publish in qualified artifacts, remain visibly marked in CLI/runtime state,
      and follow the final Q88 field-bundle inclusion contract.
- [x] Paired GC/drone release manifests bind the exact catalog hash, while the
      installed immutable catalog is packaged independently of source directories.
- [x] Package-owned custom generators/validators run from CMake, emit only
      deterministic build-tree outputs, and establish the required mission-
      specification/behavior-tree dependency closure without mutating source files.
- [x] CMake installs the catalog and all referenced assets beneath
      `share/iii_drone_mission`; runtime resolves them through the ament package
      index and catalog identities in both simulation and deployed profiles, with
      no `WORKSPACE_DIR`, source-checkout, or absolute-path fallback.
- [x] Simulation preflight detects when mission sources and the installed catalog
      differ, automatically runs the targeted mission-package build/install/
      validation step, and never launches stale or invalid assets.
- [x] Asset-only dirty changes can create a dependency-complete field artifact
      without recompiling unaffected code and without bypassing signing/validation.
- [x] Runtime mission selection accepts catalog IDs rather than filesystem paths,
      enforces profile allowlists and the final Q83 safety/persistence contract,
      and transactionally restores the previous entry after rebuild failure.
- [x] `iii mission status/list/list --all/show/select` and `select --default` expose active
      identity, hash, profile compatibility, classification, resolved dependencies,
      release default, temporary override state, runtime readiness, and safe
      selection without displaying or accepting target filesystem paths; `--all`
      explains incompatible and unavailable entries rather than hiding them.

Tests:

- Valid multi-tree closure, malformed/missing/escaping reference, duplicate name,
  unavailable node/port, compatibility mismatch, production/test classification,
  qualified allowlist, asset-only build, install-space-only sim/real lookup,
  source-tree absence, tamper, and deterministic rebuild tests.

Implementation notes (2026-08-26):

- Added the Q84 package-owned registration/generation contract with deterministic
  local, production-only qualified, and explicit field-candidate reductions. The
  catalog content-addresses the complete specification/tree closure and binds the
  interface, runtime, behavior-node, source-state, generated node model, and Groot
  project identities; malformed, unclassified, escaping, incompatible, linked,
  missing, extra, or tampered content fails closed.
- Replaced mission path parameters and environment expansion with installed ament
  catalog identities. Mission runtime, interfaces, Runtime API, GC, MCP workflows,
  and the canonical `iii mission` surface now use catalog IDs, expose readiness and
  experimental warnings without paths, enforce profile/safety gates, and rebuild
  selection transactionally with rollback to the previous entry on failure.
- Added simulation source/install attestation and targeted incremental preflight.
  An intentional behavior-tree-only drift was detected and repaired by the exact
  mission-package build; the unaffected mission shared library retained its mtime.
  The final installed local catalog contains seven entries and eighteen assets at
  `sha256:735e2aab6a7b5691f3a2b172b96b258098cce9b39973d17ebabc80648fbf6d57`
  with an empty source-drift result.
- Qualified ARM64 payload construction promotes only the verified qualified
  reduction, removes local/field variants, and binds the same logical catalog hash
  into the release and paired component bundle manifests. Field reductions admit
  only explicitly selected onboard-compatible experimental IDs with persistent
  warning metadata and use the same signed bundle verification boundary.
- Verification passed Mission 63/63, deployment 210/210, III CLI 66/66, MCP 28/28,
  Runtime 282/282, Configuration 88/88, Contracts 25/25, GC 52/52, and Interfaces
  7/7 tests in the Jazzy devcontainer, plus diff hygiene, Python error checks,
  forbidden legacy path/service scans, and final installed-catalog zero-drift
  verification. The ARM64 target build boundary is covered by release-pipeline and
  qualified-catalog promotion tests; a publishable qualified release was not built
  from the intentionally dirty multi-repository feature workspace.

### P2: Implement Transactional Onboard Release Management

Phase acceptance:

- [x] A staged release never mutates the active release.
- [x] Activation, health acceptance, and rollback survive command interruption and power loss.
- [x] An onboard host component completes or rolls back accepted transactions
      without requiring the CLI, SSH connection, network, or operator computer.
- [x] Runtime safety gates prevent deployment during aircraft operation.

Delivery order:

1. Implement P2.T0 staging and P2.T2 receiver transaction/journal primitives.
2. Add P2.T3 receiver A/B self-update compatibility, then P2.T1 safety
   authorization and P2.T4 health/rollback before production activation.
3. Add P2.T5 transport and P2.T6 CLI orchestration only through those primitives.
4. P2.T7 logging and P2.T8 local records integrate before end-to-end evidence or
   destructive host workflows are considered complete.

Phase 2 verification (2026-08-26):

- Isolated full Python suites passed for the CLI (120), Mission (22),
  Configuration (56), Contracts (25), Interfaces (7), GC (55), deployment
  integration (365), and Runtime (298). The initial combined invocation exposed
  only test-process module-name/environment collisions; each owning suite was
  rerun in isolation so no product failure was hidden.
- The ROS Jazzy phase gate built and tested all six affected III packages with
  `--base-paths src`: 681 tests, zero errors, failures, or skips. The first shell
  attempt stopped before build execution because `set -u` is incompatible with
  the ROS setup script; the corrected invocation completed successfully.
- The documentation manifest was regenerated for the new operator guide, and
  the Runtime simulation-profile test now explicitly isolates the unrelated
  receiver-clock gate. Both corrected full suites passed.
- Transaction interruption and power-loss behavior is covered by deterministic
  journal/fault-injection tests. No live aircraft power-cycle or physical-media
  replacement drill was available in this environment, so no hardware exercise
  is claimed.

#### P2.T0: Implement Release Staging And Retention

**Status: Completed.**

Description:
Install verified bundles into release-ID directories, enforce storage reserves,
track current/previous/candidate state, and garbage-collect only releases that
are neither active nor required for rollback or evidence retention.

Acceptance:

- [x] Re-staging the same release is idempotent.
- [x] An active or rollback release cannot be garbage-collected.
- [x] Field-development retention preserves the active and immediately previous
      field release plus the protected qualified anchor.
- [x] Only an explicitly qualified release can replace the protected anchor.
- [x] Insufficient storage fails before modifying runtime state.
- [x] The unprivileged SSH account cannot directly modify active release paths.
- [x] Only bundles with a trusted signature and valid checksums can enter the
      privileged staged-release area.
- [x] Q127 release status is checked before staging and again immediately before
      activation; `withdrawn` and `unsafe` candidates fail closed.
- [x] A newly learned unsafe status never deletes an installed release or causes
      an autonomous selector switch; the target exposes its recovery-only state
      and blocks flight-capable operation as specified by Q127.

Tests:

- Temporary-root tests for first install, duplicate install, low disk, retention,
  interrupted extraction, corrupt staging, withdrawn candidate, unsafe active/
  anchor release, stale status index, and last-resort recovery-only behavior.

Implementation notes (2026-08-26):

- Added the schema-validated `iii.onboard-release-state/v1` inventory and a
  receiver-owned `ReleaseStore` for immutable drone slots beneath
  `/opt/iii/releases/<release-id>`. Detached verification completes before space
  projection or extraction; extraction re-verifies the exact signed identity,
  flattens only the signed payload, freezes it root-owned/group-readable with no
  write bits, and revalidates every installed path, type, size, mode, and hash.
- Staging is selector-independent and idempotent. Atomic, fsync-backed state tracks
  active, rollback, candidate, protected qualified anchor, the newest two accepted
  field releases, exact signed bundle identities, and the monotonic cached Q127
  status index. Replacing an unaccepted candidate removes only the old known slot;
  garbage collection refuses every protected role and unknown/corrupt content.
- Storage preflight accounts for compressed input, declared peak extraction,
  receiver/checkpoint/diagnostic overhead, and preserves the greater of the 2-GiB
  or 10% root-filesystem reserve before privileged materialization.
- Qualified staging and state-bound acceptance authorization check signed status
  independently. Withdrawn and unsafe releases are rejected for new staging or
  normal activation. Newly learned unsafe active/anchor state is persisted without
  deletion or selector switching, blocks flight capability, rejects stale index
  downgrade, and permits an already-installed unsafe candidate only as explicit
  last-resort recovery when no accepted deployable alternative remains.
- Verification passed the full 220/220 deployment suite in the Jazzy devcontainer,
  including 10 release-staging state/retention/security cases and six filesystem/
  hostile-input cases. The tests exercise duplicate staging, real permission denial
  as `nobody`, low disk, signature and installed-tree tamper, interrupted extraction,
  candidate replacement, protected retention, explicit anchor authority,
  withdrawn/unsafe status, stale indexes, and recovery-only behavior; schema,
  Python error, JSON, and diff-hygiene checks also passed.

#### P2.T1: Implement Safety-Gated Activation

**Status: Completed.**

Description:
Before stopping runtime, verify the shared logical target identity, release compatibility, fresh
PX4 state, control ownership, Mission Execution state, and configuration
migration readiness according to the settled maintenance policy. Persist the
transaction before each irreversible step.

Acceptance:

- [x] Activation is rejected while safety state is stale or operational gates fail.
- [x] The maintenance override is interactive, audited, narrowly authorized,
      stops all III units first, requires physical-safety confirmation, and is
      unavailable to unattended scripts by default.
- [x] Activation never starts Mission Execution or a Direct Operation.
- [x] Activation switches code, configuration checkpoint, and mission catalog as
      one compatible release transaction; rollback restores the matching catalog.
- [x] Real runtime rejects mission selection by arbitrary filesystem path and
      records exact catalog/spec/tree IDs for selection and execution evidence.

Tests:

- State-matrix tests for landed/disarmed, armed, airborne, Mission-owned,
  Custom Operation, stale PX4, unavailable runtime API, and maintenance override.

Implementation notes (2026-08-26):

- Added a content-identified `iii.activation-safety/v1` observation and fail-closed
  gate binding logical target/profile, runtime identity/freshness, PX4 availability,
  landed/disarmed/failsafe/navigation state, Mission/Custom/Direct/Reference
  ownership, configuration migration readiness, and the settled three-second
  continuous-safe interval. Unknown, unavailable, stale, active, and mismatched
  observations are rejected before selector mutation.
- Added the attended recovery-only maintenance override. It is disabled for
  unattended calls and non-TTY streams, stops and proves `iii.target` plus only
  canonical III units before prompting, requires the target-specific physical
  safety phrase, and audit-binds actor, operation, release, target/profile, stopped
  units, and exact observation. It cannot waive known armed, airborne, active
  Mission/Custom/Direct/Reference ownership or an unready configuration checkpoint.
- Added a durable, fsync-backed activation transaction and versioned composite
  selector binding immutable release, configuration checkpoint/schema, mission
  catalog hash, and profile. The transaction journal precedes every code,
  configuration, and composite-selector mutation. Rollback restores the complete
  prior tuple and matching catalog; every journal proves `autonomy_started:false`.
- Extended the filesystem contract for persistent configuration checkpoints and
  receiver-owned activation journals, documented the receiver safety boundary,
  and added Draft-7 schemas for safety observations, overrides, selectors, and
  transactions.
- Extended Mission interfaces, catalog resolution, Runtime contracts, selection
  results, latched execution status, and command-decision events with exact catalog
  ID/hash, entry hash, specification asset ID, and sorted behavior-tree asset IDs.
  Mission rejects specifications whose used tree identities differ from their
  catalog entry closure; Runtime rejects missing or malformed selection evidence
  and continues to reject filesystem path selection on real targets.
- Verification passed 17 focused activation/state-matrix/rollback tests, 25 focused
  Runtime mission identity/event tests, the full 256/256 deployment suite, and the
  four affected ROS package build/test run (662 tests, zero failures) in the Jazzy
  devcontainer. All deployment JSON and Draft-7 schemas parsed/validated; Python
  compile, fatal Flake8, and repository diff-hygiene checks passed.

#### P2.T2: Implement The Onboard Deployment Receiver

**Status: Completed.**

Description:
Create a minimal host-installed, root-owned deployment receiver that validates
structured requests and owns the privileged filesystem, transaction journal,
release selector, systemd, authorized-key, and host-configuration mutations
needed by deployment. Install and supervise it independently from replaceable
III releases. Once it accepts activation, it—not the remote CLI—owns health
deadlines, durable acceptance, rollback, and boot reconciliation. Expose only
the settled narrow authenticated command transport; reject arbitrary commands,
paths, unit names, and environment injection. Narrow initial Ansible bootstrap
elevation after this receiver is installed and verified.

Acceptance:

- [x] `iii-deploy` has no unrestricted passwordless sudo path; receiver requests
      cannot reach the separately keyed human `iii` maintenance authority.
- [x] Only declared III release paths and systemd units can be mutated.
- [x] Requests and results are audit logged without secrets.
- [x] Key-management uses add -> prove new credential -> revoke old sequencing and
      rejects in-band removal of the final usable SSH operator key. Complete key
      loss follows Q128 physical salvage/reimage; there is no receiver override.
- [x] Receiver binaries/configuration are not stored under `/opt/iii/releases`,
      and replacing or breaking an III release cannot replace or stop the receiver.
- [x] Accepted operations continue through their deadline after SSH/network/client
      loss, and status can be reattached by operation ID after reconnection.
- [x] Target-wide mutations obey the final Q113–Q114 detach/cancel/concurrency
      contract with one durable receiver-owned operation lease, read-only
      observability, safe-checkpoint cancellation, and audited stale-lock recovery.
- [x] Apply authorization follows the final Q115 state-bound, expiring, single-use
      nonce contract and its five-minute monotonic default; stale/replayed/cross-
      target plans cannot mutate the host.
- [x] Receiver restart or host reboot deterministically reconciles every durable
      transaction state without starting autonomy.
- [x] The 60/120/60-second target/deadline/rollback budgets are enforced onboard
      using monotonic deadlines where applicable and reported with measurements.
- [x] The receiver cannot update the stable bootstrap, systemd recovery unit,
      trust root, or final selector fallback through a normal release operation.

Tests:

- Privilege and hostile-input tests covering path traversal, arbitrary units,
  environment injection, unsupported operations, direct unprivileged writes,
  client/network loss, receiver restart, host reboot, deadline expiry, successful
  boot-journal reconciliation, final-key denial, stale/replay rejection, and
  bootstrap-mutation rejection.

Implementation notes:

- Added the root-owned receiver engine, fixed canonical request/plan contracts,
  five-minute state-bound nonces, one durable target-wide lease, operation journals,
  hash-chained audit records with result identities, safe cancellation, and boot
  reconciliation that explicitly reports `autonomy_started: false`.
- The receiver now claims the exact five-file bundle plus optional status index into
  a size/reserve-checked, operation-scoped root-owned directory before durable
  acceptance. It fsyncs and re-verifies the claimed archive/release/status identities;
  later changes to the unprivileged upload tree cannot affect execution or resume.
- Added forced-command SSH key add/prove/revoke state, pending-key self-proof only,
  final-key denial, derived `authorized_keys` reconciliation, sshd-ancestry peer
  authentication, a bounded Unix-domain socket transport, and no TCP surface.
- Added stable receiver/reconciliation systemd units outside application release
  slots, explicit receiver filesystem/host privilege policies, zero final sudo
  grants, schemas, packaged host assets, and domain invariants. Normal operations
  have no action or write path for bootstrap/fallback, receiver units, or trust roots.
- Verification: 35 focused receiver/staging/security tests and all 239 deployment
  tests passed in the Jazzy devcontainer; Python compile, E/F lint, canonical JSON,
  diff hygiene, schema validation, and pinned-backend wheel payload inspection passed.
  `systemd-analyze verify` parsed both units successfully; this non-provisioned host
  could only warn that the future `/opt/iii/receiver/current` executable is absent,
  plus unrelated host-unit warnings. Real root-owned installation/boot/SSH-loss tests
  remain part of the P3.T1 provisioned-host and later end-to-end acceptance work.

#### P2.T3: Implement Transactional Receiver A/B Self-Update

**Status: In-Progress.** The A/B package, slot, compatibility, bootstrap,
reconciliation, receiver protocol/upload, and CLI invocation surfaces are
complete and target-equivalent evidence is green. Physical A/B switching and
forced-fallback evidence on the intended aircraft remain required before this
task can return to Completed.

Description:
Implement the Q32/Q49 receiver update path on top of P2.T2 without coupling it to
application health. Stage a signed receiver payload into the inactive receiver slot,
prove it can manage all retained application/configuration/journal/protocol formats,
switch only through the minimal Ansible-installed bootstrap, and restore the old
receiver on any readiness failure. A successful receiver remains active when a later
application candidate fails; the receiver changes no stable bootstrap/unit/trust
contract itself.

Acceptance:

- [x] Receiver payloads are separately identified/signed, extracted into an
      inactive slot, and cannot overwrite active/fallback/bootstrap content.
- [x] Before switch, compatibility proves read/resume of durable journals/audits,
      activation/rollback of every retained release manifest, configuration
      checkpoint handling, and installed bootstrap/CLI protocol ranges.
- [x] The stable bootstrap owns selector switch/revert and grants the new receiver
      30 monotonic seconds to start, reopen its socket, pass self-tests, and report
      exact generation/compatibility readiness.
- [x] Startup, timeout, socket, self-test, journal, retained-release, or protocol
      failure restores the prior slot and aborts before application activation.
- [x] Host/CLI/network loss cannot suppress the bootstrap deadline or reversion;
      reboot at every persisted self-update stage reconciles deterministically.
- [x] A successful receiver update remains active across application rollback only
      after proving compatibility with the restored application/configuration pair.
- [x] Ordinary receiver payloads cannot modify bootstrap code, systemd recovery
      unit, trust roots, selector fallback, or host-maintenance policy.

Tests:

- Successful A/B handoff, incompatible retained release/journal/CLI, bad signature,
  failed extraction/start/socket/self-test, 30-second timeout, client/network loss,
  power loss at every selector/journal stage, application rollback under new
  receiver, and forbidden bootstrap/trust mutation.

Implementation notes (2026-08-26):

- Added deterministic `iii.receiver-update-manifest/v1` packages with an isolated
  Ed25519 `receiver-update` authority/signing domain, exact content index, detached
  manifest/archive signature, safe USTAR extraction, immutable root-owned A/B
  slots, generation monotonicity, post-verification archive recheck, and rejection
  of links, special files, hostile paths, stable-bootstrap/systemd/trust content,
  active-slot writes, and fallback-slot replacement.
- Added fixed-path installed compatibility inspection. It validates every retained
  release manifest and operation journal, verifies the hash-chained receiver audit,
  validates activation transactions and the active composite selector, inventories
  configuration checkpoint schemas, and authenticates installed bootstrap/CLI plus
  request protocol versions. Every observed format must be in the signed candidate
  compatibility closure before inactive-slot installation can become switchable.
- Added a stable Ansible-owned bootstrap state machine and separate apply/reconcile
  units. Only the bootstrap can mutate the dedicated receiver selector directory;
  the running receiver can write only inactive slots and its durable update state.
  Fallback advances to the current working slot before the old inactive slot is
  replaced, preserving a working receiver through repeated A/B updates.
- The bootstrap journals before selector changes, launches the candidate outside
  the replaceable service, independently proves a live Unix socket plus exact
  receiver ID/generation, self-tests, journal compatibility, and bootstrap/CLI/
  request protocols, and commits only within 30 monotonic seconds. Startup, probe,
  timeout, reboot, or compatibility failure restores fallback; every state records
  `application_activation_started:false` and reconciliation is idempotent across
  all persisted switch/revert stages without a client or network dependency.
- A committed receiver exposes a separate compatibility assertion for restored
  application/configuration pairs and is not reverted merely because application
  activation rolls back. Added fixed protocol descriptors, readiness/signature/
  manifest/state schemas, packaged bootstrap entrypoint/assets, selector-isolated
  filesystem policy, and explicit ordinary-update forbidden paths.
- Verification passed 28 focused A/B/signing/compatibility/fault/reboot cases and
  the full 284/284 deployment suite. Fatal Flake8, Python compile, Black, all JSON
  and Draft-7 schema checks, diff hygiene, and a 98-file wheel payload inspection
  passed. `systemd-analyze verify` found no dependency/sandbox graph conflict; on
  this unprovisioned host it reported only the expected absent future `/opt/iii`
  executables plus unrelated host-unit warnings.
- Physical provisioning exposed a missing invocation surface, so P2.T3 was
  reopened rather than treating unreachable internals as complete. The receiver
  now owns a fixed, resumable, client/content-bound upload, exact plan/action
  protocol, root-owned claim with size and storage-reserve gates, durable
  acceptance, staged systemd preparation, and no-block stable-bootstrap handoff.
  The CLI adds read-only signed inspection and one retained
  `receiver-update apply` transaction with a schema-validated actual record.
- Candidate generation and persisted control generation now reconcile across the
  handoff without weakening the converged host generation floor. Candidate child
  processes are terminated before replacement and before the main receiver
  service resumes. A systemd scheduling failure explicitly closes the pre-switch
  state as reverted, releases the mutation lease, and leaves the selector
  untouched so a later update is not permanently blocked.
- Canonical provisioning-artifact review found that the slot launchers still
  delegated to the stable bootstrap virtualenv. The builder now safely expands
  the complete hash-locked ARM64 wheel closure into each separately signed slot,
  rejects wheel path traversal, links, collisions, and expansion-limit overflow,
  and launches receiver/gateway/client modules with system site packages disabled
  from the immutable active selector. A/B selection therefore changes executable
  code while the Ansible-owned recovery bootstrap remains unchanged.
- Focused verification after reopening passed 95 receiver A/B, engine, state,
  upload, bootstrap-entrypoint, transport/policy, and systemd cases; 31 CLI
  deploy/SSH cases; 18 offline documentation cases; 26 contract/systemd cases;
  deterministic wheel construction included all four new schemas and the prepare
  unit. Physical update/fallback evidence remains open before returning this task
  to Completed.
- Committed source `8415622` produced canonical provisioning artifact r7, record
  `bcd2d80c8954cf5ad8cdd5968696101054e9335606fcb0c69c8d92001a882770`,
  with receiver identity
  `14ae2380ce5048c734b9157c35bab8a28207308e93fceb9dfa58c0c2a6e75db9`.
  Its 55 MiB signed generation-1 slot contains 1,470 indexed entries. Under the
  ARM64 target-equivalent image, the slot-local receiver, client, and SSH gateway
  entrypoints each imported the embedded closure and exited zero on `--help`; an
  inspected native extension was confirmed AArch64 ELF. The target-equivalent
  Ansible fixture now consumes this same canonical slot builder instead of the
  retired bootstrap-delegating wrapper.
- Added the canonical read-only-plan/apply builder for later receiver
  generations. It authenticates the exact host-provisioning artifact record,
  complete wheel closure, generation-1 bundle identity, owner-only signer,
  active receiver-update trust entry, schemas, and portable-state policy before
  expanding and signing a separately identified slot; every input is
  reauthenticated immediately before materialization. It refuses overwrites,
  generation regression, non-SemVer versions, tampering, links, and unsafe
  output paths and emits a validated `iii.receiver-update-artifact/v1` record.
- The production r7 source produced signed generation 2 / `v1.0.1` artifact
  `.iii/receiver-update-r7-g2` under operation
  `iii-receiver-update-r7-g2-20260828`. Artifact record
  `5d54c1f140ca032d40c449db836fabb061df6a6dc04ade5b8d018b63022862f3`
  binds source record `bcd2d80c8954cf5ad8cdd5968696101054e9335606fcb0c69c8d92001a882770`
  and receiver `14ae2380ce5048c734b9157c35bab8a28207308e93fceb9dfa58c0c2a6e75db9`
  to candidate receiver
  `9b2c6e8d91a31b3c3fa52df6af6e2c9799a9d99e3bf55eaeb5580203be2b0364`.
  Focused tests include real generation-2 creation plus schema, signature,
  permissions, wheel-tamper, and generation-regression checks. Physical A/B
  apply and forced fallback evidence remain open until r8 is booted on the Pi.
- The live stock-boot parser correction supersedes r7 before physical use.
  Committed source `f65dec6` produced r8 provisioning record
  `36dcdec6c4a4916bdf3a0b5f7fae5373d66b82dfafc9b48ceb677aecce3b0f70`
  with generation-1 receiver
  `a626fe0e38498d45ce80fc71d68603c2442bbb25afdb571ac1904797785ff393`.
  Its canonical generation-2 / `v1.0.1` artifact record
  `2095b8b9d024952a7fcc48ede78d5ecdc4bd501b70b348c12a3a16613fa8aa02`
  binds candidate receiver
  `1f81a9795225de69ad485393ad5fedde08266dbcd6916a8c923d66b43ce2475f`;
  both signatures and the 1,471-file initial slot verify locally. Subsequent
  pseudo-flash execution superseded these artifacts before physical use.
- The deferred physical flash was replaced by an exact ARM64 pseudo-flash of the
  signed payloads under native AArch64 emulation. That gate found two production
  defects rather than weakening acceptance: module execution through
  `python -m` returned without calling `main`, and a root receiver process could
  write `__pycache__` into an otherwise immutable signed slot. All three receiver
  modules now have executable module guards, and generated launchers enforce
  `PYTHONDONTWRITEBYTECODE=1` plus `python -B -S`. Regression tests execute every
  launcher module and prove the packaged launchers are selector-local and
  bytecode-free.
- Development artifact r10 is the first corrected pseudo-flash candidate.
  Provisioning record
  `2540f6e8ac5465dcf8367c2d00cd29342fc11ee5b1f387070a4755f9e6055212`
  installed generation-1 receiver
  `a57079effedc2ebae0d31905f5fa22b21ea904d5359be16b5a61be6fd09c1eee`;
  receiver-update record
  `f7a3189628177f290e6e1d0739f6a3acf8d892399d5a963a558139b4ab2c876d`
  committed generation 2 in slot B with slot A retained as fallback. Native
  AArch64 imports, all three root launcher entrypoints, both complete signed slot
  trees, the selector, and zero bytecode cache entries passed. Retained evidence
  `.iii/evidence/pseudo-flash-r10-arm64-20260829.json` has SHA-256
  `23b99f0e063df8db28ea9f99cf182557e73959906a04fef933d4e031a1e7031e`.
  The full Noble/systemd convergence, idempotence, drift repair, finalization,
  bootstrap revocation, and fresh permanent forced-command session then passed
  in 513.38 seconds. Because r10 was deliberately built from the working tree,
  it is target-equivalent evidence only; a fresh artifact from committed source
  is required for the deferred physical A/B and fallback acceptance.
- Committed fix `00bbb00` produced the clean-source r11 provisioning record
  `9093beca08812d3c642cc5abf549d2a629e911e4073e36b6fd15dff210b47649`
  and generation-1 receiver
  `092db7af6cef71f072a6a2aafb64865a9bf36d1999be3d88de5da6ed3c0fdf3f`.
  Its separately planned generation-2 update record
  `79f32baa4df8089ccb2db7319dc8bb58658ee09b5759538dfe696f729d7533aa`
  committed receiver
  `528a354de229e4e2c7578e5be86f6eba6ef3d513e8834b88d876587ddd8bab8f`
  to slot B with slot A retained as fallback. The exact signed ARM64 closure
  passed native extension imports, root execution of all three launchers in
  both generations, complete signed-slot verification, and a zero-bytecode
  mutation check. Evidence
  `.iii/evidence/pseudo-flash-r11-arm64-20260829.json` has SHA-256
  `04fe9d53f3d71639e29569154f31dbb023133ff433ff11d2a151065c17cac0b1`.
  This is the retained candidate for the deferred physical flash; physical A/B
  switch and forced-fallback evidence remain open.
- The no-touch source-invocation correction at committed workspace source
  `600c16f` and CLI source `46e05b0` produced clean-source r12 provisioning
  record `70b1b8eafebedf54cdbb44e2fb6946a5037dd04745ddb01450d07a79163fd59e`
  with generation-1 receiver
  `1ad612f9721827ebcc1136866982d049509ff286ed2cef22fb7d61bbbd775bb3`.
  Its separately planned generation-2 update record
  `b39ec223567135afd47d2005259a297b850afc283222b945efa0c142878126f9`
  committed receiver
  `dbbc7eef5b69c1c47a4d7bc1ca13af60097428e5ad96dba89184fc3bf7971892`
  to slot B while retaining slot A as fallback. Both complete signed slot trees
  verified, and all three selector-local launchers executed natively under the
  isolated AArch64 runtime with networking disabled and a read-only active-slot
  mount. The slot remained bytecode-free. Evidence
  `.iii/evidence/pseudo-flash-r12-arm64-20260831.json` has SHA-256
  `3a487fd75fe66fcbc74c3aa956137af18d4a0130069623a8a20bfaba82528596`.
  R12 supersedes r11 as the deferred physical-flash candidate; it is still
  target-equivalent evidence and does not claim physical commissioning.
- The physical-topology correction at committed workspace source `e8d3a1c` and
  unchanged CLI source `46e05b0` produced clean-source r13 provisioning record
  `3e8ee2884e193012e52c0cbd923fd61a0f17eddf5200e8848afc53466601fd71`
  with generation-1 receiver
  `2ff8ee5c9097368cdbb78683ffcd2cb140cbb075b6076c4ddc2c3d4e9f2af5c9`.
  Its separately planned generation-2 update record
  `1b7ea921ccdcb252363ee734240e24503b088c92e2d327781befa280bb897691`
  committed receiver
  `2eb98bf3f0be75da74b0abd7ee9cd279ac337f2574a0c6d99f34e07be1b91c6f`
  to slot B while retaining verified slot A as fallback. Both signed slots
  verified; all three selector-local launchers executed under native AArch64
  emulation with networking disabled and the active slot mounted read-only; the
  slot remained bytecode-free. Evidence
  `.iii/evidence/pseudo-flash-r13-arm64-20260902.json` has SHA-256
  `8e5b53740d3f5aa14a4bf10640ec6f2c6f1d498837557b8f4ad17a0403573e34`.
  R13 supersedes r12 for the final physical flash, but remains target-equivalent
  evidence and does not claim a physical receiver switch or commissioning.
- The dedicated PX4-Ethernet correction at committed workspace source `ee516ca`
  and unchanged CLI source `46e05b0` produced clean-source r14 provisioning
  record `4f9148197611d3f97a5b3047be6bdfefc2b587fb2323f1f822d1b38609aa6682`
  with generation-1 receiver
  `0c5960c47026aa009099ed87267d68ce964482dd8e30ac35850996e18e74a672`.
  Its separately materialized generation-2 update record
  `a7c9317f4f4d6821cf168a983af022be7ec1c2af14853f26cd827eea6759f181`
  committed receiver
  `b3da77cb978e8e68e71d63b276b36d75cb8d1f8b1cb3192fb99a28ef70990713`
  to slot B while retaining verified slot A as fallback. Both signed slots
  verified; all three selector-local launchers executed from both slots under
  native AArch64 emulation with networking disabled; all fourteen native
  extensions reported ELF machine 183 (AArch64), and both slots remained
  bytecode-free. Evidence
  `.iii/evidence/pseudo-flash-r14-arm64-20260902.json` has SHA-256
  `a118f6af0ee2b69c848313bad3fa3b47bb8146cfbc040034d4375bd16fb71fff`.
  R14 supersedes r13 for the final physical flash, but remains target-equivalent
  evidence and does not claim a physical receiver switch, PX4 link, or
  commissioning.
- The first remote workspace CI run exposed that the host-finalization fixture
  always requested root-owned runtime projections, even on an unprivileged
  GitHub runner. The fixture now retains the production root/group ownership
  path under root while relying on the temporary tree's inherited runtime group
  when unprivileged. The original non-root reproduction and all 10 finalization
  tests pass on the host, the same 10 pass through the root devcontainer path,
  and the 117-test receiver-focused suite passes without weakening production
  ownership enforcement.
- The final SSH-account correction at committed workspace source `af128c6` and
  CLI source `4c60c7a` produced clean-source r17 provisioning record
  `4d1f1171a39ce57009862a84752d9fd7708692335e9d457f6ac48bb77c0432a5`
  with generation-1 receiver
  `17fd233c8d581c49f2d8a68b33305bf55018426959c1d6ae3c4012cdc24e7cfd`.
  Its generation-2 / `v1.0.3` update record
  `7a5731a2d66e4d71a4d2bbb9715da6a6b4369ea2934d6fc7a4eb562429bfe5bf`
  committed receiver
  `f26eb475bbae3d06e5515b7745f2e7a2dcca904bbb81ada4fdce12e18704cddf`
  to slot B while retaining verified slot A as fallback. Both signed slots,
  every selector-local launcher from both generations, all fourteen AArch64
  native extensions, immutable slot modes, and the zero-bytecode gate passed in
  the isolated network-disabled ARM64 pseudo-flash. Evidence
  `.iii/evidence/pseudo-flash-r17-arm64-20260903.json` has SHA-256
  `6322256992266ee39aa6813ae30ac6b0aa5bfa33ebed5d4ceb2569d8c11db174`.
  This is target-equivalent evidence only: the physical Pi was not flashed or
  contacted, and physical A/B, forced fallback, and commissioning remain open.

#### P2.T4: Implement Activation Health And Automatic Rollback

**Status: Completed (2026-08-26).**

Description:
Atomically select the candidate, restart required systemd/runtime processes,
verify daemon/runtime/configuration/ROS/hardware readiness, mark acceptance, and
restore code plus configuration checkpoints on failure. Health evaluation and
rollback execution run under the onboard receiver and never depend on remote
polling continuing.

Acceptance:

- [x] Success is reported only after defined health gates pass.
- [x] Failed health restores a known previous release without activating autonomy.
- [x] Recovery resumes correctly after power loss at every transaction stage.
- [x] Diagnostic evidence is retained for failed activation and rollback.
- [x] Disconnecting or terminating the CLI immediately after activation request
      acceptance cannot suppress health timeout or rollback.
- [x] Acceptance requires a 10-second stable window within the 120-second
      deadline and persists an evidence snapshot before selector commit.
- [x] Health proves release identity agreement across daemon/runtime API,
      configuration reconciliation, required hardware, required services,
      required managed-node states, and compatible fresh landed/disarmed PX4.
- [x] Active mission/custom/direct operation or Reference Owner blocks acceptance;
      only canonical-profile entities explicitly marked optional may be absent.
- [x] Automatic release rollback authority ends when acceptance is durably
      committed according to the final Q97 contract; later failures use bounded
      process restart, visible fault state, retained diagnostics, and explicitly
      safety-gated operator rollback.

Tests:

- Fault injection at each persisted transaction stage and each health gate.

Implementation notes (2026-08-26):

- Added a receiver-owned activation coordinator that binds a signed release-health
  policy, staged release authorization, immutable configuration checkpoint,
  current safety observation, composite selector, control-plane proof, health
  evidence, and release-state acceptance into one durable transaction. Candidate
  health must remain continuously valid for ten seconds and is never accepted
  after the 120-second monotonic deadline.
- Health now fails closed on receiver/bootstrap identity, daemon and runtime
  release/profile identity, runtime API compatibility, canonical configuration
  reconciliation, declared hardware and service readiness, exact managed-node and
  systemd states, PX4 interface/firmware/parameter compatibility, fresh landed and
  disarmed state, and all mission/custom/direct/reference ownership. Optional
  absence is accepted only when the signed profile explicitly declares it.
- Added fixed-path onboard adapters. The root receiver can start only the two
  fixed control-plane units and the canonical daemon profile over its Unix socket;
  it independently composes systemd and immutable receiver-readiness proof with
  identity-bound runtime observations. Runtime publishes canonical atomic health
  and safety observations, verifies the selected configuration checkpoint and
  hardware-role evidence, and removes observations on failure or shutdown.
- Activation and explicit rollback use fixed `plan-activate`/`activate` and
  `plan-rollback`/`rollback` protocol leaves with retained expected state, a bound
  nonce, apply-time safety recheck, durable detached execution, and reconnectable
  operation journals. Client or network loss after acceptance cannot affect the
  onboard deadline, rollback, or reboot reconciliation.
- Every pre-acceptance state, including evidence-persisted but not yet accepted,
  restores the previous code/configuration tuple and starts only its control plane
  after reboot. Once release-state acceptance is durable, automatic rollback is
  disabled permanently; later control-plane failures get at most two bounded
  restart attempts and a visible fault. Operator rollback rechecks current safety,
  retained-role identity, qualified status, the complete health gate, and then
  swaps active/rollback roles only after new acceptance evidence is durable.
- Receiver A/B compatibility now inventories and schema-validates retained
  activation-health transactions and evidence so an update cannot orphan the new
  durable formats. Added the new transaction, evidence, control-plane, runtime
  observation, release-health policy, and receiver-plan schema surfaces to the
  packaged deployment wheel.
- Verification passed the full 323/323 deployment suite, including fault injection
  at every pre-acceptance state and health domain, detached activation and rollback,
  accepted-journal reboot reconciliation, signed-status rollback denial, bounded
  post-acceptance recovery, and receiver-update compatibility. The Jazzy
  devcontainer built `iii_drone_runtime` and passed all 288 package tests. Fatal
  Flake8, Python compilation, modified-file Black, all 45 Draft-07 schemas, wheel
  payload inspection, diff hygiene, and the updated submodule lock all passed.

#### P2.T5: Replace The SSH Deployment Adapter

**Status: Completed (2026-08-26).**

Description:
Replace password files, `sshpass`, agent forwarding, and shell-interpolated
commands with the settled shared client-authentication mechanism, explicit
local-network host-trust behavior, structured command execution, explicit
transfer destinations, and least-privilege elevation.

Acceptance:

- [x] The adapter consistently targets only `iii.local` and clearly reports the
      accepted lack of server host-key authentication.
- [x] Complete bundles upload to release-ID-specific unprivileged partial paths,
      resume only after identity/size agreement, and are never privileged-staged
      before full signature/checksum verification.
- [x] Temporary disconnect preserves resumable state; stale partial cleanup is
      limited to partials inactive for seven days, uses monotonic/boot evidence when
      target wall time is untrusted, and cannot remove an active upload.
- [x] Commissioning measures complete transfer against the 120-second field-WLAN
      target and records whether content-addressed optimization is justified.
- [x] Secrets are never printed, placed in release artifacts, or stored in world-readable files.
- [x] Key list/add/revoke operations never expose private key material and
      cannot remove the final usable SSH key in-band. Rotation must first enroll
      and verify a replacement; complete authority loss follows Q128 reimage and
      recommissioning, not an override.
- [x] The shared logical runtime identity is checked after connection, without
      claiming that it cryptographically authenticates the physical host.
- [x] User-controlled values cannot alter remote command structure.

Tests:

- SSH adapter tests for `iii.local`, unreachable, unauthorized,
  interrupted/resumed/mismatched/stale-partial transfer, hostile argument,
  representative transfer budget, and unexpected logical-runtime cases.

Implementation notes (2026-08-26):

- Replaced the password-file, interactive-password, agent-forwarding, SCP, rsync,
  and shell-interpolated adapter with argv-only key authentication to the fixed
  unprivileged `iii-deploy@iii.local` endpoint. The adapter requires a current-user-owned
  mode-0600 Ed25519 private key, derives the receiver client identity from its
  canonical public key, redacts credential paths from failures, disables every
  password and forwarding path, and explicitly reports that server host keys are
  not authenticated under the accepted initial local-network risk.
- Added a forced `iii-deployment-ssh-gateway` with only canonical receiver IPC,
  exact upload-control verbs, and the configured OpenSSH SFTP subsystem. SFTP
  starts in the fixed incoming root, denies link operations, holds the global
  upload lock, and applies fail-closed Linux Landlock confinement so the shared
  `iii` account cannot use SFTP to write sibling configuration or runtime state.
  No user value is evaluated by a shell or incorporated into command structure.
- Added content-bound upload manifests for the exact five-file drone component
  plus optional signed status index. A release-specific `<release-id>.partial`
  resumes only when the retained upload identity, complete-file hashes, and every
  partial size agree. Finalization hashes the complete file set, checks the inner
  release identity, fsyncs file directories, and atomically exposes the upload;
  the root receiver still independently claims and verifies it before any
  privileged staging or execution.
- Added canonical inactivity evidence with boot ID, monotonic time, wall time,
  and wall-trust state. Seven-day cleanup cannot acquire the SFTP session lock,
  uses monotonic age only within one boot, uses wall age across boots only when
  both observations trust wall time, retains malformed/uncertain entries, and
  rejects linked, replaced, or otherwise unsafe incoming roots and trees.
- Transfer results retain release/upload/transfer identity, expected logical
  profile, exact byte totals, resumed bytes, elapsed time, the 120-second target,
  host-authentication limitation, and whether the target was met. A single miss
  records that repeated representative measurements are still required and does
  not prematurely justify a content-addressed protocol change.
- Extended receiver self-update compatibility to inventory and schema-validate
  retained upload manifests/activity, preserved receiver-owned add/prove/revoke
  sequencing and final-key denial, and documented the non-shell transport and
  accepted physical-host-authentication limitation. The CLI transport commit is
  `68b6752`; the workspace gitlink and governed lock were updated together.
- Verification passed 60 focused receiver/upload/security/update cases, the full
  332/332 deployment suite, 9 focused adapter cases, and the full 75/75 CLI suite.
  Fatal Flake8, modified-file Black, Python compilation, shell syntax, every
  Draft-07 schema, diff hygiene, dependency-lock verification, and both wheel
  payload/entrypoint inspections passed. This workstation has neither a resolving
  `iii.local` endpoint nor an enrolled deployment identity, so no live field-WLAN
  timing is claimed; the measured commissioning record and target/miss behavior
  are covered deterministically and remain ready for provisioned-host evidence.

#### P2.T6: Rebuild The III CLI Deployment Surface

**Status: Completed.**

Description:
Replace legacy `install`, `container`, and raw synchronization behavior with
build, inspect, stage, activate, deploy-development, rollback, status, and
configuration-capture workflows backed by the release modules. Keep runtime
operation on the existing `iii system ...` path.

Acceptance:

- [x] Every mutation supports a useful dry-run or preflight report.
- [x] `iii deploy field` plans dependency-aware GC/drone component selection,
      permits only compatibility-safe overrides, prepares the verified GC updater
      handoff before any drone receiver mutation for paired changes, and never turns
      PX4 manifest drift into an implicit FMU write. P3.T9 owns transactional
      host-native GC activation and health checking after the GC host is converged.
- [x] Plan and completion output group mission, behavior-tree, and parameter
      changes with dependency reasons and resulting identities; the final Q89
      contract persists structured impact and actual-result records per operation.
- [x] CLI output always names target endpoint, expected/advertised profile, and
      release identity; mutating profile mismatch fails closed.
- [x] Sourced dev/field profiles provide convenient defaults, `--target sim|real`
      overrides per command, and no hidden mutable global target is retained.
- [x] Target descriptors decouple endpoint, execution host, runtime profile, and
      simulator provider so future Pi-runtime/workstation-Gazebo HIL does not
      require a bundle, receiver, deployment-protocol, or capture-format redesign.
- [x] The same deployed release can cold-switch from default `real` to a declared
      future `opti_track` profile and back without redeployment, while missing
      profile contracts/readiness fail closed.
- [x] Middleware interface/peer policy uses detected stable LAN interfaces and
      supports a disabled-by-default future simulator-peer extension without
      installing Gazebo or simulator assets onboard.
- [x] Commands return machine-meaningful failure status and retain diagnostics.
- [x] Local operation records and large artifact caches are separate; exact
      content-bound record retention/pruning follows the final Q90 protection
      contract and uses the atomic operation-registry foundation that P2.T8 expands
      with shared blobs, references, and portable archives.
- [x] `iii field prepare` refreshes and verifies the signed Q127 release-status
      index, records offline-cache completeness, and never makes a withdrawn or
      unsafe release newly deployable.
- [x] Read-only `iii field check` implements Q125–Q126 with stable finding IDs,
      deterministic pass/warn/fail exit statuses, human/JSON output, sealed local
      records, optional signed warning acknowledgement, and no mutation/arming or
      authorization-token behavior.
- [x] `iii system clock sync` works from every authorized operator computer via
      receiver/SSH without the GUI, and the GC companion invokes the same operation
      automatically on discovery.
- [x] `DEGRADED_CLOCK` blocks runtime mutations while retaining read-only status,
      diagnostics, authenticated clock sync, deployment, and recovery surfaces.
- [x] Legacy destructive synchronization is unavailable.

Tests:

- CLI parser and orchestration tests plus a local fake-target end-to-end test.
- Field prepare/check tests for online/offline status refresh, stale/invalid status
  signatures, stable findings/exit statuses, warning acknowledgement, live-state
  drift after a sealed record, and unwaivable failure.

Implementation notes:

- Replaced the remaining deployment CLI surface with typed plan/inspect/stage/
  activate/rollback/field/status/configuration-capture operations. Universal
  dry-run/confirmation retains exact operations, Q90 prune plans bind every
  candidate record hash and protection reason, apply rejects stale candidates,
  caches remain outside operation deletion, and legacy destructive sync/raw SSH
  paths remain unavailable.
- Added strict `sim`, `real`, initial `opti_track`, and reserved `hil` target
  descriptors plus detected stable-LAN middleware policy and a disabled future
  simulator peer. Dev/field setup files provide process-local defaults; explicit
  target/profile flags never mutate hidden global state. The same installed
  release boots `real` or the declared `opti_track` alias through the existing
  supervision profile contract.
- Added detailed source impact with GC/drone dependency reasons, mission/catalog
  additions/changes/removals and tree closure, parameter/default/set changes,
  explicit legacy-shadow reintroduction review, resulting identities, concise
  human rendering, strict JSON schemas, and durable planned/actual phase records.
  GC-only and drone-only paths are independent; paired work packages the verified
  GC handoff before any authenticated drone transfer, and PX4 writes are always
  explicitly false.
- Added online/offline Q127 cache preparation and Q125/Q126 sealed readiness with
  stable PASS/WARN/FAIL findings, drift-sensitive identities, unwaivable failures,
  and warning acknowledgements. Unsigned checks are diagnostic; signed readiness
  and acknowledgements require an active trusted `workstation-field` key and never
  become authorization.
- Added receiver-owned five-sample clock planning/synchronization, boot-bound
  `DEGRADED_CLOCK`/`OPERATIONAL` state, measure-only operational sync, automatic
  fixed-profile runtime start after the initial gate, GC discovery companion, and
  fail-closed Runtime API mutation gating while read-only/session/recovery/
  deployment/clock-sync surfaces remain available.
- Task-specific verification passed 46 deployment tests, 79 CLI tests, 8 Runtime
  gate tests with `RuntimeWarning` promoted to error, and 7 GC discovery tests.
  Modified-file Black, fatal Flake8, Python compilation, shell syntax, every
  Draft-07 schema, diff hygiene, and CLI/deployment wheel payload inspection
  passed. No enrolled `iii.local` target or authorized field clock endpoint is
  available on this workstation, so no live wall-clock step or field activation is
  claimed; authenticated fake-target orchestration covers the accepted boundary.
  Per operator direction, the full regression suite is deferred to the end of
  Phase 2 rather than repeated after this task.

#### P2.T7: Implement Session-Aware Log And Diagnostic Lifecycle

**Status: Completed (2026-08-26).**

Description:
Segment runtime/host logs by boot and runtime session, bound idle logging, retain
deployment/configuration evidence according to its stronger persistence rules,
and provide hash-verified local pull plus receipt-aware onboard pruning. Keep
rosbags/datasets outside automatic log retention. Integrate projected log use
with the deployment storage reserve.

Acceptance:

- [x] The current session and four newest completed sessions survive age-based
      cleanup despite intermittent aircraft power cycles.
- [x] Runtime/host logs obey the 14-day and lesser-of-1-GiB-or-5% cap without
      deleting protected deployment/configuration evidence.
- [x] Healthy idle operation emits no unbounded repetitive info logs; debug/
      verbose mode is explicit, session-scoped, capped at 256 MiB, and still obeys
      the global storage limit.
- [x] Before clock trust, ordinary III logs are bounded in memory and carry boot/
      monotonic ordering; after synchronization they flush once with reconstructed
      UTC and explicit uncertainty metadata, without duplicates or false precision.
- [x] The degraded-clock ring enforces 10,000-record/16-MiB limits, drops oldest
      first, and persists the exact dropped-record count after clock trust.
- [x] The latest 50 deployment records and records referenced by retained
      releases remain available; failed activation diagnostics honor their
      pull/acknowledgement-or-30-day protection.
- [x] `iii logs pull` and `iii deploy diagnostics pull` produce immutable local
      manifests/checksums and record onboard receipts only after local verification.
- [x] Pruning accepts only exact receipt-backed content identities and cannot
      remove the current session, active transaction, protected release evidence,
      tuning journals, configuration/shadow checkpoints, or rosbag datasets.

Tests:

- Multi-boot/session retention, invalid clock, idle log-rate, debug cap, size/
  age pressure, interrupted/corrupt pull, verified receipt, duplicate pull,
  receipt-bound prune, protected-data denial, and deployment-reserve interaction.

Implementation notes:

- Added canonical boot/session metadata, monotonic sequencing, process/boot
  recovery, transition-only availability logging, explicit session debug mode,
  and root-timer retention. Plans preserve the current plus four newest completed
  sessions, apply both the 14-day and lesser-of-1-GiB-or-five-percent limits, and
  report protected overage rather than deleting protected evidence or violating
  the deployment reserve.
- Added the bounded 10,000-record/16-MiB pre-clock ring and receiver-authenticated
  clock mapping. The first canonical hash-bound clock state flushes once with UTC
  lower/upper bounds and exact loss count; shutdown also completes a newly trusted
  flush, while malformed, stale-boot, faulted, or tampered clock state never gains
  false UTC precision.
- Added receiver-owned immutable export snapshots, bounded chunks, client-bound
  verified receipts, and exact protection-aware prune plans. Current sessions,
  active/recent operations, the newest 50 deployment audit operation IDs,
  retained-release evidence, configuration/tuning/rosbag/dataset domains, and the
  receiver audit remain protected. Unacknowledged failed diagnostics have no
  receipt-backed deletion path, which is stronger than the settled 30-day
  protection floor.
- Added resumable `iii logs pull` and `iii deploy diagnostics pull` with safe local
  locators, short-write handling, immutable source/local manifests, content hash
  verification, stale target/destination rejection, multi-file interruption
  recovery, and duplicate identity checks. `iii logs prune --pulled` rechecks the
  fresh receiver plan and uses a durable quarantine transaction that resumes
  safely after power loss before reclaiming bytes.
- Task-specific verification passed 82 deployment/receiver tests, 46 CLI tests,
  and 23 Runtime tests in the Jazzy devcontainer (13 existing deprecation
  warnings). Modified-file Black, fatal Flake8, Python compilation, 65 Draft-07
  schemas, diff hygiene, isolated systemd unit verification, CLI/deployment wheel
  payload inspection, and the targeted `iii_drone_runtime` colcon build passed.
  No enrolled aircraft is available here, so no live receiver pull/prune or real
  power-cycle evidence is claimed. Per operator direction, the full regression
  suite remains deferred until P2.T8 closes Phase 2.

#### P2.T8: Implement The Local Operator Record Registry And Portable Archive

**Status: Completed.**

Description:
Create one host-user-owned, Git-ignored `.iii/` registry for local release caches,
operation records, captures, backups, commissioning/readiness records, release
evidence, signed release-status indexes, and import/export receipts. Each domain
keeps its own schema and retention policy while sharing content-addressed blobs,
atomic metadata updates, reference tracking, integrity verification, and portable
archive primitives. Implement `iii records inventory/verify/archive/import` for
workstation/GC disaster recovery without treating the repository or GitHub as a
backup for bulky evidence. Archives go only to an explicit operator-selected path;
the CLI does not invent cloud storage or silently copy data.

Acceptance:

- [x] Registry paths never enter Git and never depend on a container filesystem or
      one absolute workspace checkout path.
- [x] Every record has schema version, content identity, creation source, logical
      target/profile where applicable, cross-domain references, and integrity state.
- [x] Concurrent CLI processes cannot corrupt indexes; writes use lock, staging,
      fsync/atomic replacement, and deterministic crash recovery.
- [x] Archive planning reports included domains, referenced blobs, total size,
      omitted/missing content, destination capacity, and whether the result is full
      or incremental before writing.
- [x] Archives and imports are deterministic, path-safe, checksummed, idempotent,
      and preserve references without overwriting conflicting content.
- [x] SSH/signing private keys, runtime/API credentials, Wi-Fi secrets, machine
      identity, and unredacted secret-bearing inputs are always excluded and make
      archive creation fail if a schema incorrectly attempts to include them.
- [x] No automatic pruning occurs. Explicit prune operations show protected
      references and cannot remove records required by retained releases, restore,
      commissioning, promotion, or unarchived irreplaceable evidence.
- [x] `iii field check` can report last verified external archive coverage and age
      without making an external archive mandatory for ordinary operation.

Tests:

- Empty and mixed-domain registries; duplicate content; concurrent writers; crash
  during metadata/blob commit; full and incremental archives; insufficient
  destination space; path traversal/symlink/special-file attacks; corrupt and
  partial import; cross-computer import; secret-exclusion fixtures; protected prune;
  and loss/rebuild of local indexes from archive manifests.

Implementation notes:

- Added the host-user-owned registry root contract and canonical domains for
  operations, paired release caches, captures, backups, commissioning/readiness,
  release evidence, signed status indexes, verified log/diagnostic pulls, and
  archive/import receipts. Release, deploy, logs, and field providers now resolve
  the same root; Git worktrees accept only ignored `.iii/` state.
- Added `iii records inventory/verify/archive/import/prune` through the universal
  result and retained-operation flow. The implementation derives versioned record
  descriptors with file/directory topology, content identities, creation source,
  target/profile, references, integrity, and protections; serializes shared blobs
  and metadata under locks with fsync/atomic replacement and hard-crash staging
  recovery; and excludes the controlling operation from its own exact snapshot.
- Portable archives use deterministic USTAR headers/order/padding, a canonical
  checksummed manifest, full or base-bound incremental coverage, explicit capacity
  planning, post-write byte verification, idempotent identical destinations, safe
  cross-computer import, and conflict-preserving reconstruction of empty directory
  topology. Import recreates a missing derived index without trusting it as
  authority.
- Secret scanning fails closed over record paths, JSON fields, CLI arguments,
  assignment/env files, bearer credentials, machine identity, Wi-Fi stores, and
  full-stream private-key material. Explicit prune reauthenticates current registry
  state and external archive bytes and cannot remove retained-release, restore,
  commissioning, promotion, referenced, unresolved, unacknowledged, or unarchived
  irreplaceable records; shared blobs and automatic pruning remain out of scope.
- `iii field check` now embeds last verified archive receipt coverage/age and media
  availability as a warning-only observation. Field preparation/readiness records,
  release audit evidence, and signed release-status indexes are retained in their
  owning domains. Added the operator recovery guide and four packaged Draft-07
  record/archive schemas.

Task-specific verification:

- All 120 III CLI tests passed in the Jazzy devcontainer, including empty/mixed and
  duplicate registries, concurrent writers/reindexing, metadata/blob crash debris,
  exact retained archive/prune replay, deterministic full/incremental/idempotent
  archives, stale bases, capacity refusal, traversal/link/special/truncated/corrupt
  attacks, cross-computer and repeated imports, secret fixtures, protected prune,
  receipt/media reauthentication, and index loss/rebuild.
- All 7 deployment field contract tests passed. All 69 deployment Draft-07 schemas
  validated; focused Black, fatal Flake8, Python compilation, and diff hygiene
  passed. Clean temporary CLI/deployment wheel builds contained both new providers
  and all four installed record/archive schemas. The governed submodule lock passed.
- No real external operator medium or replacement GC is attached in this
  environment, so no claim is made for a physical-device unplug/replug or live
  disaster-recovery drill. Per operator direction, the full regression suite runs
  once below at the Phase 2 boundary rather than after each task.

### P3: Provision Raw Ubuntu Into A Converged Aircraft Host

Phase acceptance:

- [x] A documented raw-image workflow creates an SSH-reachable target.
- [x] Ansible converges that target into the complete III host baseline.
- [x] A second convergence run reports no unintended changes.
- [ ] Re-convergence succeeds offline from the prepared development laptop.

Delivery order:

1. P3.T0 and P3.T1 establish bootstrap and convergence; P3.T2–P3.T7 layer on the
   resulting stable host contract and may be developed independently where safe.
2. P3.T3 credentials and P3.T7 networking must pass before remote deployment is
   commissioned. P3.T5 hardware and P3.T6 boot evidence must pass before real
   runtime commissioning.
3. P3.T8–P3.T10 complete the paired GC/QGC/PX4 surface.
4. P3.T11 backup/restore and P3.T4 maintenance are accepted only after the
   receiver, local record registry, and qualified recovery anchor exist.

Phase software-boundary verification (2026-08-27):

- The full III-only boundary passed 586 deployment tests (five explicitly opt-in
  native target/systemd tests skipped), 706 tests across the nine III ROS package
  targets, 125 GC frontend tests, generated-contract freshness, lint with three
  existing Fast Refresh warnings and no errors, typecheck, production frontend
  build, three workspace integration tests, and 180 CLI tests.
- The boundary run found and fixed GC login-unit policy drift, extensible PX4
  manifest-identity schema drift, a real-profile Runtime test fixture that did not
  provide the now-required machine verifier, and stale GC mission-catalog generated
  types/consumers. Focused regressions passed before the completed boundary reruns.
- Physical raw-image/SSH reachability, login/logout, second-host convergence, and
  prepared-offline laptop recommissioning remain intentionally unclaimed because
  this environment has no attached Raspberry Pi/SD target or replacement GC host;
  P3.T8 and the physical phase criteria therefore remain open.
- The final opt-in target-equivalent phase gates passed on 2026-08-27: privileged
  Noble/systemd Ansible convergence (`1 passed` in 555.92 s), native systemd
  boot/restart/broken-release recovery/switching (`1 passed` in 12.10 s), Ubuntu
  22.04 and 24.04 GC online/offline convergence and drift repair (`2 passed` in
  324.76 s), and replacement-record import/fresh-identity ordering (`1 passed`).
  The Ansible harness now builds the complete local receiver distribution graph
  and includes the immutable portable-state policy in its signed receiver payload;
  the native-systemd fixture materializes every Ansible-owned writable path and
  retains unit journals on startup failure.

#### P3.T0: Create SD Imaging And First-Boot Cloud-Init Profiles

**Status: Completed (2026-08-26).** Added the checksum-pinned Canonical Ubuntu
24.04.4 Raspberry Pi image/profile contracts, owner-only Git-ignored NoCloud
input rendering, fail-closed removable-media inspection, retained typed-proof
write/readback/eject transactions, private-mount-namespace seed installation,
content-addressed evidence, CLI commands, schemas, packaging, and the first-boot
runbook. Changed files are under `deployment/provisioning/`,
`deployment/src/iii_deployment/{host_imaging,seed_mount}.py`, deployment schemas
and tests, `tools/III-Drone-CLI/iii/host.py`, CLI tests, and
`docs/host-imaging-and-first-boot.md`.

Description:
Pin and verify the official Ubuntu Server 24.04 ARM64 Raspberry Pi image, then
define the generated cloud-init bootstrap layer for storage, boot, host identity,
networking, initial key trust, and Ansible reachability. Keep application
installation out of cloud-init beyond what is necessary to establish the host;
do not fork or rebuild the Ubuntu disk image initially.

Acceptance:

- [x] Profiles are validated before writing media.
- [x] Partition growth/layout and destructive reimage preflight follow the final
      Q103 contract and never imply that `/var/lib/iii` survives physical reimage.
- [x] The upstream image checksum and release identity are verified before use.
- [x] Destructive media selection, confirmation, system-disk/in-use rejection,
      write flushing, readback verification, and evidence follow the final Q102
      contract; automation cannot bypass the interactive target proof.
- [x] No production password or aircraft secret is embedded in committed files.
- [x] Cloud-init inputs, on-media seed data, bootstrap authority, post-convergence
      sanitization, and residual-secret inspection follow the final Q101 contract.
- [x] Failed first boot leaves diagnosable local evidence.
- [x] Provisioning resumes safely after host/CLI/network interruption and follows
      Q107's Ethernet-recovery/reimage boundary without a bypass credential.

Tests:

- Automated image/VM boot where Raspberry Pi hardware permits, plus physical-media acceptance.

Verification (2026-08-26): 69 focused deployment/CLI/contract/documentation
tests passed; 74 schemas parsed and the provisioning contracts validated;
rendered cloud-init network-v2 data passed host Netplan generation; the pinned
SHA appeared exactly once in Canonical's live `SHA256SUMS`; live read-only
inspection rejected both internal NVMe disks; Black, fatal Flake8, diff checks,
and deployment/CLI wheel-content checks passed. The raw writer test streamed,
flushed, and hashed a 2 MiB+17 byte decompressed fixture and exact readback, while
fault-injection covered changed media/input, target-proof denial, backup/data-loss
authority, record validation, secret redaction, interruption-safe replay, and
private seed transfer. The first two host test invocations did not collect tests
because the executable/venv lacked pytest; `python -m pytest` used the installed
host module. Netplan validation initially exposed an unsupported wildcard Wi-Fi
match; the profile was corrected to Raspberry Pi `wlan0` and the rerun passed.
No removable media, Raspberry Pi, or compatible VM image was attached, so no
physical write/eject, first boot, or hardware recovery result is claimed; those
remain mandatory commissioning evidence on applicable hardware.

#### P3.T1: Implement Idempotent Ansible Host Roles

**Status: Completed (2026-09-02).** The completed 2026-08-26 host baseline now
includes the separately keyed `iii` human field-maintenance boundary while
preserving the forced-command `iii-deploy` receiver and bootstrap-only Ansible
identities. Implemented a data-driven Raspberry Pi 5
host baseline under `deployment/ansible/`, the retained `iii host provision
check/apply` workflow, signed receiver bootstrap/A/B recovery installation,
permanent forced-command access, pinned Ubuntu/ROS package policy, UTC/slew-only
time ownership, operator-LAN firewalling, host health evidence, and fail-closed
cloud-init/bootstrap finalization. Host inputs, plans, Ansible recaps, checks,
and run reports are versioned and content-bound; canonical JSON evidence is
rendered through byte-tested templates. Added the operator runbook at
`docs/host-provisioning.md` and bound Ansible target/package values back to the
canonical target definition.

Verification completed with production-profile Ansible lint across 35 files,
syntax checks for both production playbooks, 125 focused deployment tests, 50
focused CLI/result/record tests, and a privileged Noble/systemd target-equivalent
test (453.60 s) covering first convergence, zero-change check mode, injected
drift detection, repair, post-repair zero drift, signed receiver readiness,
bootstrap authority/secret removal, canonical final report verification, and
ARM64 emulation integrity. No physical Raspberry Pi or live aircraft was
available; real ARM64 snapshot package identities were verified against the
governed ROS snapshot and the integration harness separately smoke-tested ARM64
execution before and after the native-systemd run.

Final target-equivalent rerun (2026-08-27): `1 passed` in 555.92 s after the
isolated receiver artifact fixture was corrected to wheel all four local Python
distributions and sign the required portable-state policy into the immutable
receiver slot. First apply, zero-change check, injected drift, repair, final
zero-change check, recovery services, and bootstrap finalization all passed.

Description:
Create roles for OS baseline, ROS installation, III user/groups, directories,
udev, hardware dependencies, network/time, firewall, log retention, systemd,
deployment receiver/bootstrap/recovery substrate, release installer prerequisites,
and health inspection. Do not encode the ROS runtime graph outside `system_spec.py`.

Acceptance:

- [x] Roles support check/diff mode where technically possible.
- [x] Second application is idempotent.
- [x] Hardware-class variation is data-driven rather than copied playbooks.
- [x] OS package changes are pinned/auditable and separated from application deployment.
- [x] Host time is UTC with normal Ubuntu synchronization configured, while all
      correctness-critical journals use boot identity and monotonic sequencing.
- [x] Post-gate host synchronization is configured slew-only; no NTP/system service
      can step wall time behind the receiver's Q59–Q60 gate and audit path.
- [x] Authenticated preflight measures operator/target offset and supports a
      narrow audited clock-set operation only while the III graph is stopped;
      active runtime is never subjected to an automatic wall-clock step.
- [x] First convergence installs and verifies the receiver service, local socket,
      control client, separate bundle/status signing trust, transaction paths, and
      independent recovery hook before narrowing bootstrap privilege.
- [x] Runtime API firewall/service configuration exposes only the documented
      plain-HTTP/WS operator-LAN port and credentials, never privileged deployment
      operations; SSH/receiver transport remains the only deployment authority.
- [x] A separately keyed `iii` account accepts an interactive public-key
      session from the operator CIDR and `sudo -n id -u` returns `0`, while the
      `iii-deploy` receiver identity remains forced-command-only and Ansible continues
      to use only the temporary `iii-bootstrap` identity.

Maintenance-access extension verification (2026-09-02): 64 focused access,
provisioning, documentation, receiver-policy, and matrix tests passed; 69 target,
host-maintenance, release-pipeline, and staging tests passed; Ansible lint passed
all 76 production-profile files with zero failures/warnings; the full deployment
phase passed 667 tests with five explicit opt-in skips; and the final privileged
target-equivalent boundary passed all three tests in 722.44 seconds on the final
2026-09-03 rerun. The rehearsal exposed and fixed two startup-order races (the
test bootstrap key's transient `/tmp` handoff and readiness inspection before
the receiver had written its evidence) plus an implicit `/opt/iii` parent mode;
the parent is now an explicit root-owned `0755` filesystem contract. The run
proved first convergence, zero drift, injected-drift repair, bootstrap revocation,
fresh forced-command `iii-deploy` receiver access, a distinct `iii` login, and
`sudo -n id -u` returning `0`. Physical post-flash access remains a P5
commissioning gate and is not inferred from target-equivalent evidence.

Tests:

- Ansible syntax/lint, check mode, first convergence, second-run idempotence,
  and drift-repair tests on a target-equivalent host.

#### P3.T2: Install Real-Profile Systemd Units

**Status: Completed (2026-08-26).** Installed Ansible-owned real-profile daemon,
runtime-API, and aggregate target units; a fixed selector-aware launcher; a
content-identified host-unit contract; and an external non-secret runtime
environment. The launcher authenticates the selector, release, profile, converged
host contract, its own installed bytes, and all installed unit bytes before every
start. Release manifests now bind the required unit contract, activation rejects
host-contract version drift before selector mutation, and receiver policy forbids
application ownership of all host-control assets.

The receiver clock gate now models boot/monotonic state through
`DEGRADED_CLOCK`, `FLUSHING_CLOCK`, `OPERATIONAL`, and
`CLOCK_FAULT_ACTIVE`; authenticates five settled samples and exact durable
daemon/API flush commits; rechecks UTC alignment after flushing; and preserves an
explicit-restart boundary after discontinuity recovery. Runtime API and daemon
output stays in bounded process-local rings before trust and during faults, with
no pre-gate session metadata or journald output. Only the separately retained,
time-untrusted clock recovery audit is persistent, under host log rotation.

Description:
Create production units and stable launcher/environment files for the III daemon
and runtime API. Use the active immutable release, real profile, persistent
paths, shared non-development identity, external secret files, restart policy, and correct
ordering without sourcing development shell profiles.
Follow Q96's host/release boundary: Ansible owns fixed units and stable selector-
aware launchers; releases own only the daemon-consumed runtime topology and wrappers.

Acceptance:

- [x] Units contain no dev credentials, sim profile, or `/home/iii/ws` dependency.
- [x] Application bundles cannot create, replace, enable, or disable host units;
      a unit-contract change is detected as a host-maintenance prerequisite before
      release activation.
- [x] On host boot, receiver and minimal control plane enter `DEGRADED_CLOCK`;
      the real ROS graph is not booted until clock synchronization and buffered-
      log flush commit successfully.
- [x] Ordinary daemon/API output remains in a bounded monotonic in-memory buffer
      while degraded and cannot leak to persistent journald/file sinks; minimal
      recovery audit is separately persisted and marked time-untrusted.
- [x] The post-sync transition reconstructs UTC metadata, flushes logs durably,
      records the gate transition, and then boots real-profile standby in order.
- [x] Clock synchronization enforces the settled sampling/RTT/offset thresholds;
      post-gate discontinuity enters `CLOCK_FAULT_ACTIVE` without interrupting
      active monotonic-time control, then transitions to `DEGRADED_CLOCK` only when
      maintenance-safe. New operations remain blocked throughout the fault.
- [x] Runtime Stop can leave the independently supervised runtime API online.
- [x] A broken active release fails visibly and remains recoverable through SSH.

Tests:

- `systemd-analyze verify` plus boot, restart, failure, and release-switch tests.
- Clock tests cover invalid boot time, high RTT, threshold edges, slew-only service
  configuration, discontinuity while standby, discontinuity during active control,
  maintenance-safe transition, buffered-log uncertainty, and explicit resync/start.

Verification (2026-08-26): 93 focused workspace deployment tests, 29 Runtime
tests, and 8 Supervision tests passed; the focused clock suite reached 25 cases
covering retained flush replay, Boolean/integer spoof rejection, post-flush drift,
fault transitions, and authenticated service commits. All 82 Draft-07 deployment
schemas parsed; focused Black, fatal Flake8, compilation, documentation, content-
identity, diff, and submodule-lock checks passed. Production-profile Ansible lint
reported zero failures/warnings across 17 files and both playbooks passed syntax
checks. A privileged native-systemd Noble container passed boot, process restart,
API independence, broken-release/SSH recovery, A-to-B selector switch, and
container-restart recovery in 12.98 s. The complete target-equivalent Ansible
first-convergence, zero-drift, injected-drift, repair, and second-zero-drift test
passed twice (516.02 s and 494.87 s), including the final host-unit contract. No
physical Raspberry Pi was attached, so no claim is made for a hardware power-cycle;
that remains commissioning evidence rather than an unverified task result.

Final native-systemd rerun (2026-08-27): `1 passed` in 12.10 s for boot, daemon
restart, API/SSH independence, broken-release recovery, A-to-B selector switch,
and container restart. The test now reports unit status and journals on startup
failure and faithfully creates the Ansible-owned tuning path required by the
production unit namespace.

#### P3.T3: Implement Shared Target Identity And Secret Provisioning

**Status: Completed (2026-09-02).** The completed 2026-08-26 shared target
identity now includes an independent retained maintenance-key identity and
explicit separation from receiver enrollment/revocation. Added one content-identified shared-aircraft
profile with stable `iii-aircraft` / `iii-aircraft-runtime` hardware-role IDs,
public machine-enrollment records, receiver-derived SSH and Runtime verifier
projections, independently revocable field-signing authority, and fail-closed
real-profile startup. The retained `iii access` workflow now prepares fresh
owner-only credentials outside Git, proves second-computer enrollment, inventories
independent authorities, and revokes either a machine or only its signer without
copying private material or retaining a shared onboard CLI token.

Description:
Define one shared target profile for the Raspberry Pi 5 hardware class while
keeping real secrets outside Git. Provision the shared runtime/system IDs,
the solo-operator browser secret, per-computer runtime API credentials and SSH
public-key authorization, and
operator-network policy without a per-aircraft inventory. Provide authenticated
key list/add/revoke flows for moving from the provisioning workstation to a
ground-control computer without copying private keys.

Acceptance:

- [x] The committed example target profile is safe and documents all non-secret fields.
- [x] The shared logical target identity is stable across release changes,
      physical reboot, and replacement Pis.
- [x] Missing/generic identity or development credentials fail real-profile startup.
- [x] A second computer can be authorized, verified, and later revoked through
      the documented deployment workflow.
- [x] `iii access enroll/list/revoke` manages independently identified machine
      credentials without copying private SSH/signing keys or retaining one shared
      all-computers CLI token; onboard values are stored as verifiers/hashes.
- [x] Workstation/GC field-signing keys are generated outside the repository with
      owner-only storage and OS-keyring/passphrase protection; the signing agent
      enforces the settled 8-hour default/24-hour maximum and signs only validated
      manifest/status/evidence digests, never arbitrary builder-container input.
- [x] Replacement-computer recovery generates fresh machine identity and keys,
      verifies enrollment before revoking an old computer, and imports only
      verified non-secret records/caches through P2.T8.
- [x] If all authorized SSH credentials are unavailable, every remote recovery or
      boot-partition injection attempt is rejected and documentation directs the
      operator to backup inspection, physical reimage, restore, and recommission.
- [x] Losing or revoking a field-signing key does not remove runtime-only access;
      signing and SSH authorities are reported and recovered independently.
- [x] Provisioning binds the independent maintenance public key without copying
      its private key, reports its SHA-256 client identity, and fails closed on
      malformed, linked, changed, non-owner-controlled, or missing key input.

Maintenance-identity extension verification (2026-09-02): the owner-only
materializer accepts normal OpenSSH public-key comments but canonicalizes the
installed identity to algorithm plus key bytes, includes its client ID in the
retained artifact/plan, and never places the private key in provisioning output.
Focused negative/identity tests and the final target-equivalent SSH/sudo proof
passed. Host baseline `ea7bde80411c155c80b84a575953d1b5752f8b21d798aa9210a57aa624ccd41d`
and target definition `ecc2e4e9dc553e1fa6fb30f350b3034db48262604ba9ca28c954cc0694b17b34`
supersede the previous candidate identities.

Tests:

- Inventory schema tests; first/second-computer enrollment; signing-only loss;
  staged replacement with old-key revocation; all-SSH-key-loss denial; attempted
  default password/bypass; and reimage/restore/recommission recovery acceptance.

Verification (2026-08-26): the final cross-repository task matrix passed 147
tests with one intentional non-root skip; that exact root-owned production
credential-projection case passed separately under `sudo`. The active Jazzy
devcontainer built `iii_drone_runtime` and passed all 27 selected credential,
configuration, and gating tests. All 86 deployment schemas passed Draft-07
meta-validation, target profiles parsed, the Ansible JSON template test passed,
production-profile Ansible lint reported zero failures/warnings across 41 files,
and both aircraft playbooks passed syntax checks. Focused Black, Pyflakes,
compile, diff, documentation, and contract checks passed. The privileged native
systemd target passed in 12.23 s, and the final target-equivalent Ansible
first-convergence, zero-drift, injected-drift repair, and second-zero-drift path
passed in 478.70 s. No physical Raspberry Pi was attached, so physical reimage,
power-cycle, and recommission remain commissioning evidence rather than an
unverified hardware claim.

#### P3.T4: Implement Controlled Host Maintenance

**Status: Completed (2026-08-27).** Implemented receiver-owned, retained
`iii host maintenance` check/apply/status/reboot operations and a fixed
root-owned systemd/Ansible execution boundary. Package work is bound to an
identified Noble/Jazzy/ARM64 policy, isolated signed snapshot sources, exact
preflighted package deltas, a state-bound verified backup, and complete durable
before/after evidence. Offline cache preflight, explicit reboot journaling,
post-boot protected-release authentication, boot gating on failure, and
recovery/reprovision guidance all fail closed. Bundle and release-status trust
rotations require proof of possession, preserve old public history, retain
backup copies, prevent final-signer/operator stranding, and create a
recommissioning marker; SSH and runtime credentials remain separate `iii
access` operations.

Description:
Add an explicit III CLI and Ansible workflow for Ubuntu, kernel, ROS, and system
package maintenance. Keep it separate from release deployment, suppress
unattended package mutation, snapshot package state before/after, report reboot
requirements, and validate the protected qualified recovery release after the
maintenance reboot. Route major platform transitions to SD-card reprovisioning.

Acceptance:

- [x] Normal qualified and field-release deployments cannot invoke package
      installation, upgrade, removal, or repository changes.
- [x] Host maintenance requires explicit operator intent and produces a retained
      before/after package and platform report.
- [x] Offline runs fail before mutation when required packages are absent from
      the local cache; cached maintenance is supported where practical.
- [x] Kernel/reboot-required changes schedule an explicit reboot and post-boot
      validation rather than rebooting unexpectedly.
- [x] Failure to validate the protected qualified release is surfaced with a
      documented recovery or reprovision recommendation.
- [x] Major Ubuntu/ROS baseline changes are rejected by in-place maintenance.
- [x] Bundle signer, release-status signer, SSH authority, and runtime credential
      rotation are separate planned changes; trust-root replacement is backup-first,
      cannot strand the final usable operator, and triggers Q110 recommissioning.
- [x] A compromised release-status signer can be removed and replaced without
      mutating historical release/status records; conflicting statements remain
      visible and are resolved by the newly commissioned trust policy.

Tests:

- Idempotent no-change run, cached/offline update, unavailable-package failure,
  reboot-required update, interrupted Ansible run, post-boot validation failure,
  major-baseline rejection, signer rotation, compromised-status-key replacement,
  final-operator-stranding rejection, and post-trust-change recommissioning.

Implementation notes and verification:

- Added the four host-maintenance contracts, fixed policy/playbook, installed
  oneshot executor and Ansible role, receiver protocol/engine/reconciliation,
  protected-anchor validator, signer history-boundary semantics, CLI surface,
  documentation, and focused deployment/CLI/systemd tests.
- The final focused matrix passed 163 deployment tests and 38 CLI tests. Black,
  Pyflakes, compile, diff, 91-schema Draft-07 meta-validation, policy identity,
  production-profile Ansible lint (zero findings across 21 files), and three
  syntax checks passed. The privileged native-systemd target passed in 11.94 s.
- The first target-equivalent run failed after 303.18 s because its dedicated
  playbook omitted the new install role while host health required the installed
  artifacts. After adding the role, the full first-convergence, zero-drift,
  injected-drift repair, second-zero-drift, and finalization scenario passed in
  532.22 s. No physical Raspberry Pi was attached, so an actual aircraft reboot
  remains commissioning evidence rather than a claimed local result.

#### P3.T5: Implement And Commission The Shared Hardware-Role Manifest

**Status: In-Progress.**

Description:
Define the shared Raspberry Pi 5 attached-device contract for mmWave CLI/data
interfaces, charger/gripper, and cable camera. Keep the PX4 MAVLink/uXRCE-DDS
transport on the Raspberry Pi's built-in Ethernet interface as an independent
fail-closed activation and field-safety gate. Generate udev rules and
stable `/dev/iii/*` paths from ambiguity-aware vendor/product/interface/stable-
property matching, using exact serial allowlists only when commissioning proves
they are necessary. Add host inspection and runtime health integration. Reconcile
the conflicting retired/current udev literals through physical evidence.

Acceptance:

- [x] One committed manifest, not per-aircraft inventory, declares required and
      optional roles and the evidence used to match each role.
- [x] Generated rules are deterministic and installed idempotently by Ansible.
- [x] `iii host inspect` reports raw device evidence, resolved roles, missing
      roles, and ambiguity without exposing unrelated sensitive host data.
- [x] Required missing/ambiguous roles block real-profile health acceptance;
      optional-device absence is reported without pretending it is present.
- [x] Camera selection does not rely on unstable `/dev/video0` enumeration.
- [ ] Legacy and current serial-specific rules are retired only after every role
      passes physical unplug/replug, reboot, and swapped-port commissioning.
- [x] Replacement devices matching an existing role contract require role-specific
      functional evidence and recommissioning without manifest mutation; unmatched
      devices produce a reviewable capture and can become supported only through a
      feature-branch manifest/rule change, tests, convergence, and recommissioning.
- [x] Inspection/commissioning never auto-learns serials or rewrites matching rules
      from observed hardware.

Tests:

- Manifest/schema and rule-generation fixtures for exact, missing, ambiguous,
  duplicate, changed USB port, and optional devices; physical Pi commissioning
  for unplug/replug, reboot, port swap, simultaneous-device enumeration, matching
  replacement, unmatched replacement capture, and auto-learn rejection.

Implementation notes and verification:

- Added the content-identified shared hardware-class manifest, explicit
  required/optional indexes, three schemas, deterministic golden udev rules,
  sanitized sysfs/udev inspection, strict phase/functional-evidence evaluator,
  Ansible installation/retriggering, root-owned receiver action, independent
  activation-health integration, both CLI spellings with non-overwriting local
  capture, trusted-local-policy binding, `/dev/iii/*` real defaults, and the
  operator/commissioning/replacement documentation.
- Focused verification passed 67 deployment contract/policy tests, then 35
  hardware/receiver tests after final trust-binding changes, 33 CLI/result
  tests, and 681 `iii_drone_configuration` tests in the Jazzy devcontainer.
  Black, Pyflakes, compile, diff checks, Ansible production lint (zero findings
  across 23 files), and both aircraft convergence syntax checks passed.
- A read-only live attempt was made. `iii.local` did not resolve and the CLI
  returned `III_SSH_IDENTITY_UNAVAILABLE`; no configured per-computer SSH key or
  attached Pi was available. Therefore unplug/replug, reboot, port-swap,
  simultaneous enumeration, functional role checks, and actual legacy-rule
  retirement are not claimed. The manifest remains
  `retained-pending-physical-evidence`, the old Core rule remains untouched, and
  this task stays In-Progress while later software tasks continue.
- Physical discovery on 2026-09-02 proved the operator link over the Pi's USB
  Ethernet adapter and exposed that the old manifest incorrectly modeled the
  PX4 as a required USB serial role. The authoritative runtime architecture uses
  the Pi's built-in Ethernet interface for MAVLink and uXRCE-DDS, with fresh
  PX4 compatibility and fused landed/disarmed evidence checked independently.
  The manifest, generated udev rules, production-profile metadata, tests, and
  operator documentation now remove only the false `/dev/iii/fmu` role; they do
  not relax or substitute for the PX4 Ethernet safety gate. Focused red/green
  verification passed all 33 hardware-role, host-inspection, release-pipeline,
  and documentation-contract tests.
- Authenticated immutable captures under `.iii/evidence/physical-20260902/`
  proved the charger/gripper Arduino once at `/dev/ttyACM0` with stable
  `/dev/iii/charger-gripper`, while camera checks were explicitly deferred for
  its battery dependency. Replugging the mmWave device and trying two USB cables
  still produced no USB tty device and neither `mmwave_cli` nor `mmwave_data`.
  The latest capture is
  `hardware-after-mmwave-second-cable-old-policy.json` (SHA-256
  `a8e717193f79340e631282d5fad600780b5e31f429818010373419d2c34da793`).
  This is negative enumeration evidence, not functional or commissioning
  acceptance; unplug/replug, reboot, port swap, simultaneous enumeration, and
  legacy-rule retirement remain open.

#### P3.T6: Manage The Raspberry Pi Boot Baseline

**Status: In-Progress.** The physical reprovision rehearsal passed, but the
post-provision live inspection exposed one additional stock Ubuntu conditional
filter that the deployed receiver rejects. Source and schema fixes pass locally;
the task remains open until the corrected receiver is installed and the same
physical inspection accepts the boot baseline.

Description:
Define the source-controlled Raspberry Pi 5 boot profile and host inventory for
firmware, kernel, command line, device-tree overlays, and Ubuntu boot settings.
Keep stock defaults unless the hardware contract documents a requirement. Route
all changes through host maintenance with backups and reboot validation; normal
application releases must not mutate the boot partition.

Acceptance:

- [x] Provisioning produces a deterministic documented boot profile with no
      unsupported overclocking or unexplained options.
- [x] `iii host inspect` reports effective firmware/kernel/boot configuration
      and drift from the declared profile.
- [x] Application bundle activation has no permission or operation capable of
      modifying boot files.
- [x] Host maintenance backs up changed boot files and records package/settings
      deltas before requesting an explicit reboot.
- [x] Physical SD repair/reprovisioning steps are documented and tested because
      A/B boot/root recovery is intentionally deferred.

Tests:

- Profile rendering/drift tests, application-mutation denial, no-change
  idempotence, boot-setting change with backup, reboot validation, and physical
  SD repair/reprovision rehearsal.

Implementation notes and verification:

- Added a canonical content-identified Raspberry Pi 5/Noble boot profile,
  strict effective-config/kernel/command-line inspection with bounded include
  parsing and secret redaction, composite same-boot hardware/boot host
  inspection, Ansible profile installation without stock boot rewrites, wheel
  packaging, and trusted-local-policy validation in `iii host inspect`.
- Normal application and receiver-update policy now forbids `/boot` and the
  installed boot policy. Retained `boot-settings` host maintenance records exact
  setting/overlay drift and file hashes/modes, requires a state-bound backup,
  preserves internal copies of the installed profile plus both boot files,
  restores them on failure, requires a separately explicit reboot, validates
  the post-boot profile/protected anchor, and marks commissioning stale.
- Focused verification passed 93 deployment boot/maintenance/receiver/policy/
  schema tests, 40 CLI/result tests, 12 boot/documentation contract tests,
  production Ansible lint with zero findings across 22 files, syntax checks for
  both convergence playbooks and the privileged maintenance playbook, Python
  Black/Pyflakes/compile/diff checks, and a built-wheel content check for all six
  new boot/host-inspection members.
- The physical SD procedure is documented with read-only filesystem diagnosis,
  stable-device and typed destructive-write gates, deterministic reimage,
  resumable reprovisioning, restore, power-cycle, and recommissioning criteria;
  its command/safety contract is tested. No spare physical SD card or connected
  Pi is available; `iii.local` did not resolve and the live authenticated CLI
  attempt returned `III_SSH_IDENTITY_UNAVAILABLE`. Therefore an actual
  repair/reprovision rehearsal and resulting physical evidence are not claimed.
  This task remains In-Progress while later software tasks continue.
- Physical execution began on 2026-08-27 against a 116.2 GiB removable Kingston
  card. The prior `/home/iii` tree was mounted read-only with journal replay
  disabled and preserved as a numerically owned tar archive before the separately
  acknowledged destructive write. Operation `iii-image-aircraft-20260827-r3`
  wrote and read back all 4,139,719,168 image bytes at raw SHA-256
  `3a19cadaefbdbe7bbe7f51a9db74acd87cccbe57685fb398b563522e50eca1f0`,
  verified the three NoCloud seed files, flushed block buffers, and powered off
  the reader. Immutable local record
  `.iii/imaging-records/2972842eeadee150984be04ec124ef9cebed26a06fcdce0a92df74457fe6a413.json`
  has outcome `verified` and file SHA-256
  `942351d911b4dc366c5634f0f5abd9a89dd95f3dff8ce674f8f41d961c3a3a0c`.
  First boot, repair/reprovision completion, and recommissioning evidence remain
  open, so the acceptance item and task remain In-Progress.
- The first physical boot reached the authenticated `ansible-ready` cloud-init
  marker and direct Ethernet DHCP address `10.42.0.70`. The canonical host apply
  completed convergence, zero drift, and finalization, then exposed two
  production defects: the aggregate run schema omitted callback `categories`,
  and root-owned mode-0600 permanent `authorized_keys` could not be read after
  OpenSSH dropped privileges to `iii`. Both now have red/green regressions. The
  target-equivalent full convergence/drift-repair/finalization test opens a new
  permanent SSH session after bootstrap removal and passed in 631.50 s. Because
  the already-finalized physical card correctly has no bypass, a second governed
  reimage/reprovision rehearsal is required before this task can complete.
- The second governed image write completed on 2026-08-28 as operation
  `iii-image-aircraft-20260828-r4`; record
  `9c94c3a3fb388bbe76f91e8131c9b154f7b5db791443dc9a576a3249a67c7708`
  proves the same 4,139,719,168-byte image SHA, corrected seed readback, flush,
  and hardware power-off. The clean Pi booted at `10.42.0.71`, cloud-init reached
  `done` with its authenticated `ansible-ready` marker, `iii.local` resolved,
  and retained host operation `iii-host-provision-aircraft-r2-20260828`
  converged with 70 changes, proved zero-change idempotence, and finalized with
  no failures. Bootstrap revocation then exposed a third production defect: the
  immutable signed receiver slot used root-only `0550`/`0440` modes, so OpenSSH
  authenticated the permanent key but the unprivileged forced command exited
  126. Receiver slots now use root-write-protected public read/execute modes
  (`0555`/`0444`), finalization validates every gateway traversal/execute bit for
  the configured runtime identity before revocation, and the SSH regression
  requires actual gateway execution. The focused receiver boundary passed 48
  tests and the full Noble/systemd convergence, idempotence, drift repair,
  finalization, and fresh permanent-session test passed in 704.82 s. A final
  governed physical reimage is still required because the correctly finalized
  second card contains no bypass authority.
- The final governed repair/reprovision rehearsal completed on 2026-08-28.
  Imaging operation `iii-image-aircraft-20260828-r6` produced verified record
  `d679645957ad78b9957daacd1d7a7ad832256865ac0ebf0a6226bb805b83690c`;
  the clean Pi reached error-free `ansible-ready` cloud-init at `10.42.0.70`.
  Artifact record
  `ff58b7f73e7a9c2eacb0c76449221a406e588cadfeb8dcdf3ed9e37f5a683f33`
  bound the committed ARM64 controller inputs. Host operation
  `iii-host-provision-aircraft-r3-20260828` completed 70 first-run changes and
  139 checks with no failure, predicted exactly zero changes across 93 checks,
  finalized all 17 checks, revoked bootstrap access, and opened a new permanent
  forced-command gateway session. The run also found and fixed the documented
  artifact builder's missing executable mode and Canonical Ubuntu's valid stock
  `initramfs initrd.img followkernel` parsing gap; focused regressions pass.
- The authenticated powered-Pi inspection
  `.iii/evidence/physical-host-inspect-powered-20260828-r3.json` then proved the
  installed generation still rejects the pinned image's valid `[pi3+]`
  conditional section. Read-only extraction authenticated the image's
  `config.txt` SHA-256 as
  `fcf55af036aa70e9600dae8313a0bebafc43e6aedef8c31883828526b357e28d`,
  exactly matching the live host. The parser and `iii.boot-inspection/v1`
  schema now accept the bounded official conditional-filter character set; the
  exact stock file parses as 19 directives across five sections and all seven
  focused boot tests pass.
- The corrected parser and receiver payload were exercised through the exact
  ARM64 pseudo-flash and the full Noble/systemd host lifecycle described in
  P2.T3. The powered physical Pi remains intentionally unchanged on the older
  generation while the operator is remote; its read-only inspection still
  reports the expected `[pi3+]` parser drift. Physical boot-baseline acceptance
  therefore remains open until a fresh committed artifact is written and booted
  during the final in-office flash cycle.

#### P3.T7: Provision Transactional Operator Networking

**Status: Completed (2026-08-28).**

Description:
Generate first-boot Ethernet/Wi-Fi configuration from safe committed templates
and untracked secret inputs, advertise `iii.local` through mDNS, and provide
receiver-backed network profile management with automatic reversion when the
new configuration is not confirmed. Keep Ethernet DHCP available as physical
recovery and do not initially provision an onboard access point.

Acceptance:

- [x] SD preparation supports Ethernet-only and one-or-more Wi-Fi profiles
      without writing credentials to Git, logs, artifacts, or review files.
- [x] Installed network secrets are root-readable and survive application
      releases independently from release directories.
- [x] `iii.local` resolves on supported operator networks without a fixed IP.
- [x] Network plan/apply reports connectivity-impacting changes before mutation
      and uses the settled 90-second monotonic onboard confirmation deadline
      independent of the CLI process.
- [x] Failure to reconnect/confirm restores the previous working configuration;
      successful confirmation commits the new profile set durably.
- [x] Ethernet DHCP remains usable after broken Wi-Fi configuration.

Tests:

- Cloud-init rendering with redaction, Ethernet-only boot, multiple Wi-Fi
  profiles, mDNS resolution, successful profile switch, CLI/network loss with
  automatic reversion, reboot persistence, and Ethernet recovery.

Notes:

- Implemented root-only, redacted `network-plan -> network-apply ->
  network-confirm` receiver contracts. Connectivity-changing apply requires
  maintenance-safe state, stops runtime, invokes only fixed privileged helpers,
  and holds the receiver lease until confirmation or rollback. The private-
  network receiver cannot write Netplan directly.
- Added exact prior-file backup/restore, fixed 90-second monotonic systemd timer,
  restart/reboot reconciliation, durable confirmation metadata, Ethernet-only
  and multi-Wi-Fi input, duplicate-SSID rejection, and mandatory wildcard
  Ethernet DHCP. SSIDs/passphrases are absent from retained CLI plans/results,
  receiver journals/audits, and imaging evidence; plaintext exists only in
  owner-only local input, root-only claimed state/backups, and the installed
  root-only Netplan file.
- Added the Avahi-owned `iii.local` baseline without a fixed IP, IPv6 claim, or
  onboard AP. Production Ansible lint and four playbook syntax checks pass. The
  privileged Noble/systemd target-equivalent rehearsal passed first convergence,
  zero-drift second pass, injected-drift repair, final zero drift, receiver/Avahi
  host health, and bootstrap finalization in 562.27 seconds. Focused network,
  imaging, receiver, policy, systemd, host-baseline, target-definition, and CLI
  coverage passed (165 tests in 2.81 seconds after the final harness adjustment).
- At the software-boundary implementation checkpoint no physical Raspberry Pi or
  operator LAN was available, so an external workstation lookup of `iii.local`
  was left open rather than inferred from the target-equivalent rehearsal.
- Physical direct-Ethernet acceptance completed on 2026-08-28: the workstation
  served DHCP without a fixed target address, authenticated the Pi's exact MAC,
  and resolved `iii.local` externally to the current lease `10.42.0.70` after
  finalization. Three ICMP samples averaged 0.247 ms, and permanent receiver SSH
  remained functional while the bootstrap identity was denied.

#### P3.T8: Provision The Ground-Control Host Baseline

**Status: In-Progress.** The production software boundary is implemented and
verified on 2026-08-27. Real graphical login/logout and a fresh physical
replacement-laptop import/enrollment drill remain commissioning evidence; those
results are not inferred from containers.

Description:
Create GC-host inventory and idempotent Ansible roles for supported graphical Ubuntu
x86_64 computers. Preserve the ROS-free frontend/proxy boundary. Separate an
operational role (CLI prerequisites, container runtime for GC frontend, user
services, discovery, mirror, clock companion, browser integration, keys/log paths)
from an optional repository/development/cross-build role; current field laptops
receive both because the repository clone remains a prerequisite in this sweep.

Acceptance:

- [x] `iii gc provision` converges stock Ubuntu 22.04/24.04 plus a local clone in
      online or prepared-offline mode and reports excluded disk/OS/vendor prerequisites.
- [x] The operational role installs native user-session owners for frontend/proxy,
      discovery, mirror, clock companion, and browser launcher without installing
      ROS/DDS/MAVSDK into the proxy boundary.
- [x] The development role installs strict submodule tooling, repository-managed
      CLI/Ansible environment, pinned ARM64 builder, and offline caches without
      making container-local state authoritative.
- [x] Secrets, SSH/signing keys, `.iii` state, captures, logs, and mutable settings
      use declared host-user paths/permissions and survive role/application updates.
- [x] A second convergence is idempotent; drift output distinguishes operational,
      development, application, and unmanaged user state.
- [ ] A replacement GC creates fresh machine identity/keys and restores only
      verified non-secret records/caches through P2.T8 before enrollment; no prior
      private key or machine credential is copied.
- [ ] User-login starts required GC companions without opening browser/QGC; logout
      stops only local graphical/user services and never affects the drone.
- [x] Discovery targets only `iii.local`, invokes Q59 clock sync only for `real`,
      and skips Pi clock alignment for `sim`.

Tests:

- Clean 22.04/24.04 operational/development matrices, online/offline convergence,
  second-run idempotence, drift separation, permissions/secrets, login/logout,
  real/sim discovery and clock behavior, and complete replacement-GC rebuild/import.

Implementation notes:

- Added the retained `iii gc provision`/status/lifecycle provider, strict policy
  and plan/report/cache schemas, categorized Ansible recap, stock-Python
  content-addressed controller bootstrap, Python 3.10/3.12 hash locks, exact
  dependency verifier, and safe prepared-offline cache authentication. Local
  Python projects build from isolated copies, so provisioning never dirties or
  depends on writable source trees.
- Added separately reported operational, application, development, and health
  roles. They own private host-user paths, fresh machine/SSH material, preserved
  secret overlays, graphical-session services, exact ROS-free runtime and GC
  images, strict submodules, offline caches, and the definition-labeled ARM64
  builder. Replacement resume reauthenticates every imported file and the fresh
  key ownership/modes without accepting a restored runtime credential.
- Added fixed-`iii.local` discovery, configuration mirror, and real-only clock
  companions plus lifecycle plans that bind every managed unit byte, mode, owner,
  and state. Login excludes browser/QGroundControl; local logout/stop contains no
  aircraft lifecycle command.
- Task verification passed 24 deployment/bootstrap/host tests, 40 CLI contract
  tests, 64 GC tests, five documentation-contract tests, Ansible production lint
  with zero warnings across 21 files, stock Ubuntu 22.04/Python 3.10 and Ubuntu
  24.04/Python 3.12 bootstraps, exact runtime installs on both Python versions,
  and online/offline apply/check/drift/repair matrices (133.43 s and 148.03 s).
  Production frontend/proxy images built with the planned identity label; the
  proxy inventory contained 23 distributions and no ROS/DDS/MAVSDK package.
- A complete authenticated prepared-offline controller wheelhouse was exercised
  with network proxies forced dead. The container matrices validate the offline
  role transaction and idempotence using authenticated role locators; final
  prepared-media contents remain a field-preparation responsibility. Physical
  graphical-session/logout and replacement-computer enrollment are intentionally
  still unchecked above.
Final opt-in matrix rerun (2026-08-27): Ubuntu 22.04 and 24.04 online/offline
apply/check, zero drift, injected operational drift, repair, permissions, and
user-unit ownership passed (`2 passed` in 324.76 s). The replacement plan's
authenticated non-secret record import-before-convergence and fresh-identity
boundary also passed (`1 passed`); this unit-level result does not substitute
for the unchecked physical replacement-laptop enrollment or login/logout drill.

- Live Ubuntu 22.04 GC commissioning on 2026-08-28 exposed and fixed stock
  `/etc/os-release` symlink handling, valid `Documents/QGroundControl` status,
  empty systemd unit-file states, an existing complete Docker CE versus
  `docker.io` package conflict, read-only authentication skipped by Ansible
  check mode, an omitted PX4-parameter companion in lifecycle plan binding, and
  false lifecycle success when a target condition prevented activation. Each
  defect has a focused regression. Provisioning operation
  `iii-bf5f8b2b85214a2e9c0dd03c` converged the physical workstation and proved
  zero drift across all three managed categories; report
  `ddbdfc5f8b2f4add92d51d6d09018c773639c6a8b0403ab68b1596e517a94e4f`.
- Retained start operation `iii-c4898f9e8a2441e9a4f81b72` activated the GC
  target plus discovery, mirror, and clock companions while the absent
  application slot safely conditioned proxy/frontend and browser/QGroundControl
  remained inactive. Retained stop operation
  `iii-2ccd413edcd3452382062e0f` explicitly contained every target member; all
  were inactive afterward and both lifecycle results declared
  `aircraft_mutation=false`. This validates the physical current-session
  lifecycle but does not claim the still-disruptive desktop logout/login event
  or a separate replacement-laptop import/enrollment drill.
- The 2026-08-29 target rerun exposed that the privileged Ubuntu fixture lagged
  the production GC controller contract: it omitted the now-required explicit
  container-runtime and operational-package inputs. Production planning already
  supplied both. The fixture now derives the Ubuntu runtime object and package
  list from the canonical GC policy, preventing a divergent hard-coded package
  model. Ubuntu 22.04 and 24.04 then passed their complete online convergence,
  zero-drift, injected-drift, repair, prepared-offline, permissions, and
  user-session ownership matrices in 132.49 and 152.28 seconds respectively.
  The two physical replacement/login acceptance items remain unchanged.
- The 2026-08-31 no-touch pre-flash audit found two canonical workstation
  invocation gaps: `setup/cli_path.bash` exposed the source CLI without its
  workspace-owned `iii_deployment` library, and the CLI default SSH identity
  disagreed with the key path installed and verified by GC provisioning. The
  setup now exposes `deployment/src`, and the CLI defaults to
  `.config/iii/keys/ssh/id_ed25519`; isolated source-environment and default-key
  regressions plus all 17 focused setup/SSH-manager tests pass. The currently
  enrolled provisioning key remains an explicit override until the physical GC
  key enrollment drill, so no authority was silently transferred.

#### P3.T9: Install And Transactionally Update GC/QGroundControl Applications

**Status: Completed.**

Description:
Install paired GC application artifacts using the workspace release identity while
keeping GC and drone slots independently rollbackable. Retain pinned production
containers for the React/FastAPI frontend/proxy under a host-native updater/service
owner. Install QGroundControl as a host-native pinned AppImage with its own atomic
slots. Connect the devcontainer PX4/Gazebo backend to the same host QGC used in real
operation. Keep host package maintenance separate from application updates.

Acceptance:

- [x] Release-bound frontend/proxy container digests install and roll back offline;
      containers own no QGC binary, host credentials, `.iii` state, signer, Ansible,
      updater, or ARM64 builder control plane.
- [x] QGroundControl is checksum/version pinned, self-update disabled, atomically
      selected, shared by sim/real, and retains a previous known-good binary while
      preserving backed-up user settings/logs outside slots.
- [x] `iii gc open/start/stop/restart/status` owns frontend/proxy/discovery/mirror/
      clock/browser behavior; `iii qgc start/stop/restart/status/config ...` owns
      every QGC operation. Neither namespace silently invokes the other application.
- [x] Simulation launches only PX4/Gazebo in the devcontainer and reaches host QGC
      over explicit tested networking; no devcontainer QGC path/lifecycle remains.
- [x] Paired updates install/health-check compatible GC first, then drone. GC
      failure leaves drone untouched; drone failure retains new GC only when it is
      compatible with the restored drone, otherwise rolls GC back too.
- [x] Connected-real updates drain/reject new browser commands and enforce the
      maintenance-safe gate; disconnected/sim updates are permitted, and recovery
      override is separately confirmed/audited.
- [x] GC slots protect qualified anchor, active, previous field release, and staged
      candidate. Non-protected cache defaults to 50 GiB and reserves at least
      10 GiB or 10 percent free; offline sets and protected domains are not evicted.
- [x] Application update failure/interruption/logout/reboot reaches a compatible
      recorded pair without losing secrets, captures, `.iii`, or QGC user state.

Tests:

- GC-only/drone-only/paired online and offline install, QGC checksum/slot/settings,
  container ownership denial, sim host networking, real safety rejection/override,
  GC-first/drone-failure compatibility rollback, interruption/reboot, cache pressure,
  CLI namespace separation, and native QGC launch.

Implementation notes:

- Added signed, content-addressed GC/QGC application slots with a durable activation
  journal, exact selector and service-state recovery, paired GC-first deployment,
  compatibility-aware reconciliation, protected offline-cache classes, and a
  strict browser drain/safety override contract. QGC settings are transactionally
  backed up and restored as part of the same application transaction.
- Split `iii gc` and `iii qgc` into independent lifecycle owners, installed the
  explicit host-native QGC user unit/desktop launcher, and removed the managed
  devcontainer QGC configuration. The simulation helper owns only PX4/Gazebo and
  reports the host UDP 14550 listener without starting it.
- Verified the actual production GC images and pinned QGC 5.0.8 AppImage
  (`06969c67ef58ea063def0a8271447a1cc385438c4a7df36813315b4475146737`),
  native launch probes on the supported Ubuntu targets, real signed manager
  stage/activate/rollback, cache and power-loss matrices, and isolated SITL host
  networking. Focused application/release/CLI matrices passed, including the real
  Ubuntu 22.04/24.04 Ansible targets.

#### P3.T10: Inventory And Manage PX4/QGroundControl Release Configuration

**Status: Completed.**

Description:
Create release-owned real/sim PX4 parameter manifests and a sanitized managed-key
QGroundControl baseline. Inventory current sources, classify ownership, bind them
to PX4/QGC versions and release identity, and implement explicit backup-first
inspection/application workflows without expanding into PX4 firmware flashing.

Acceptance:

- [x] Complete expected PX4 sets are versioned per real/sim profile with required,
      tunable, and calibration/identity classifications and release hashes.
- [x] Real activation reads the full FMU inventory and fails health on required
      mismatch without silently writing any PX4 parameter.
- [x] `iii px4 params pull/plan/apply/verify` requires disarmed safe state, creates
      a complete restorable backup, presents per-key changes, writes only confirmed
      keys, and verifies readback/recovery.
- [x] The GC companion captures a complete disarmed baseline, observes/reconciles
      MAVLink parameter changes, and mirrors immutable content revisions without
      inventing unavailable operator/transaction provenance for direct QGC edits.
- [x] PX4 event handling uses a two-second debounce followed by a complete-set
      reconciliation, repeats every 60 seconds while connected/disarmed, and runs
      once at clean session end; armed/in-flight monitoring never starts a bulk
      parameter transfer or write.
- [x] Arbitrary named PX4 sets can be captured, described, exported, verified,
      compared, and selectively promoted through the same untracked evidence and
      feature-branch workflow principles as III parameter sets.
- [x] QGroundControl release configuration contains only declared stable managed
      keys; merge is transactional with backup and preserves unowned user state.
- [x] A schema-versioned QGC key policy classifies managed, local-preference,
      generated/cache, sensitive, and prohibited settings.
- [x] Clean-exit/explicit QGC captures are redacted immutable local evidence, and
      per-key promotion writes only reviewed managed keys on a feature branch;
      geometry, host paths, credentials, caches, and unsafe upload changes fail.
- [x] Public telemetry/log upload is disabled by the managed baseline unless the
      operator explicitly opts in outside release defaults.
- [x] QGC ParamCache and equivalent version-coupled generated data is reproducibly
      generated/cached and not treated as hand-maintained policy.
- [x] Sim and real use the same manifest schemas, inventory tooling, release
      provenance, and diff/report formats with profile-specific values.
- [x] The real release owns one authenticated PX4 Ethernet baseline binding the
      static `10.41.10.1/24` companion and `10.41.10.2/24` FMU pair, exact SD-card
      network/startup artifacts, dual MAVLink/uXRCE-DDS transport, and compatible
      firmware identity.
- [x] PX4 automatic Ethernet parameter owners are disabled so MAVLink and
      uXRCE-DDS are started together without an exclusive port-owner collision;
      release assembly rejects any baseline/real-manifest identity mismatch.

Tests:

- Full/partial FMU inventory, required/tunable/calibration drift, disarmed/armed
  apply, backup/restore, interrupted write, failed readback, sim/real manifests,
  QGC clean merge, user-state preservation, settings migration rollback, public-
  upload default, and generated-cache compatibility.

Implementation notes:

- Added schema-validated, release-owned real/sim manifests with 1,023/1,021
  classified parameters, respectively; the decoded reference SITL snapshot is
  retained by content identity and SHA-256. Release assembly authenticates the
  snapshot, both manifest identities, PX4 firmware version/40-bit commit prefix,
  and profile mapping. The reproducible generator rejects lossy/non-integral
  MAVLink integer evidence.
- Added `iii px4 params` pull/plan/apply/verify and named capture/list/show/diff/
  export/import/promote workflows; all writes require disarmed status, a fresh
  complete backup, exact key confirmation, readback, and recovery. Receiver
  activation plans now retain and revalidate complete no-write PX4 evidence bound
  to the staged release manifest before selector mutation.
- Added the login-scoped QGC-forwarded PX4 companion with fixed two-second debounce,
  60-second reconciliation, clean-session-end reconciliation, immutable content
  revisions, and observation-only provenance. Added transactional managed-key QGC
  merge/restore, redacted clean/explicit captures, guarded feature-branch
  promotion, and QGC/PX4/version-bound generated ParamCache storage. Public upload
  remains disabled in the baseline.
- Focused verification passed 127 deployment tests (plus the separately executed
  2/2 Ubuntu target matrix), 59 CLI tests, all 118 Draft-07 schemas, deterministic
  manifest regeneration, and submodule-lock/static checks. Live isolated PX4 SITL
  proved a receiver-valid 1,021-parameter no-write activation inventory, the real
  companion entrypoint, and a backup-first 12.0→11.5→12.0 parameter write/restore
  drill whose final snapshot returned to the original zero-drift identity.
- Added the schema-validated real-aircraft PX4 Ethernet baseline with static
  `10.41.10.2/24`, companion `10.41.10.1/24`, no route/DNS, and authenticated
  `net.cfg`/`extras.txt` renderings. The release now authenticates the baseline as
  its own input, binds its identity to the real parameter manifest, disables the
  colliding automatic Ethernet owners, and explicitly starts MAVLink `14540/UDP`
  plus uXRCE-DDS `8888/UDP`. Focused PX4, release, bundle, QGC, and CLI verification
  passed 77 tests, including exact/idempotent artifact rendering and drift refusal.
  The phase gate passed 661 deployment tests with five explicit opt-in target
  matrices skipped, plus all 230 CLI tests. Physical backup/apply/reboot/transport
  proof remains in P5.T1.

#### P3.T11: Implement Portable Host Backup And Reimage Restore

**Status: Completed.**

Description:
Implement the Q104–Q106 backup contract across receiver, CLI, persistent-state
owners, and commissioning workflows. Seal a coordinated point-in-time portable
state archive, verify and store it content-addressed offboard, and restore only
through staged compatibility/reconciliation after clean reprovisioning. Also add
`iii host salvage --device <explicit-device>` for Q128: with the Pi powered off,
identify the removed SD using Q102's safe block-device proof, mount supported
filesystems read-only in a private temporary mount namespace, inspect a known III
layout, and extract only the portable-state schema into a normal verified backup.
Salvage must never modify the source media, reset credentials, or make it bootable.

Acceptance:

- [x] Backup includes every declared portable state domain and proves a single
      coordinated revision/hash boundary without copying live-changing files.
- [x] Credentials, private keys, network secrets, host identity, active selectors,
      and receiver transaction machinery are structurally excluded and detected if
      accidentally present.
- [x] Real/OptiTrack backup requires maintenance-safe state, briefly quiesces and
      flushes writers, seals locally, and resumes standby before long transfer.
- [x] `.iii/backups/` supports content-addressed list/show/verify/export/import;
      explicit pruning cannot remove referenced restore/audit evidence.
- [x] Planned reimage and host-baseline replacement require a fresh verified backup
      or a separately confirmed, audited unrecoverable-data-loss override.
- [x] Backup freshness is bound to the sealed persistent-state generation and all
      declared invalidating mutations, not timestamp alone; readiness warns when
      no verified external archive has been produced for 30 days.
- [x] Restore requires a clean converged host, compatible deployed release, staged
      schema review/reconciliation, atomic persistent-root activation, and health
      validation without restoring stale machine/transaction identity.
- [x] Salvage refuses the running system disk, mounted/in-use media, unsupported or
      inconsistent filesystems, dirty/mid-transaction state that cannot be safely
      interpreted, and any request for secrets/credentials; source mounts are
      kernel-enforced read-only and unmounted on success, failure, or interruption.
- [x] A successful salvage records source block-device identity, filesystem/layout
      evidence, recoverable domains, omissions, transaction consistency, hashes,
      and a prominent statement that fresh credentials and recommissioning remain
      mandatory.

Tests:

- Concurrent-writer rejection, quiesced snapshot, interrupted transfer, tamper,
  prohibited-secret fixture, export/import, duplicate content, incompatible schema,
  staged restore failure, successful post-reimage restore, data-loss override,
  loopback SD salvage, running/in-use-device rejection, forced read-only proof,
  interrupted salvage cleanup, inconsistent transaction refusal, secret exclusion,
  and salvage-to-reimage-to-recommission acceptance.

Implementation notes:

- Added a tracked portable-state policy spanning configuration, tuning, PX4,
  hardware, activation evidence, deployment audits, and diagnostics. The receiver
  now owns maintenance-safe quiesce/flush/seal/resume, deterministic archives,
  exact policy and per-file hash binding, structural secret rejection, freshness
  state markers, content-addressed retention, and resumable fixed-root transfer.
- Added `iii host backup create/list/show/verify/export/import/restore/prune/status`
  with retained mutation plans and externally verified `.iii/backups/` receipts.
  Reimage and baseline-replacement gates require current state-bound external
  evidence or the existing separately confirmed data-loss override. Restore claims
  immutable receiver input, requires a clean compatible host, reconciles in a
  private generation, atomically selects it, validates health, and rolls back the
  selector without restoring machine identity or receiver transactions.
- Added `iii host salvage --device` and a privileged private-mount-namespace worker.
  It authenticates explicit removable media, rejects the running/in-use/unknown or
  inconsistent source, uses `e2fsck -fn` and kernel `ro,noload,nodev,nosuid,noexec`,
  extracts only policy-declared portable state, guarantees unmount, and emits a
  content-identified salvage record requiring clean reimage, new credentials, and
  full recommissioning.
- Focused verification passed 180 deployment/receiver tests (one opt-in native
  systemd integration test skipped) and 67 CLI tests, all 128 Draft-07 schemas,
  compile/static/submodule-lock checks, and a privileged loopback ext4 salvage
  drill. The drill independently reverified the archive and proved no residual
  mount, loop device, or `/dev/disk/by-id` fixture remained after completion.

### P4: Make Field Tuning Durable And Traceable

Phase acceptance:

- [x] GUI tuning survives runtime restart and release deployment as intended.
- [x] Every captured value can be traced to release, schema, baseline, session,
      and operator action.
- [x] Promotion to tracked configuration is deliberate and reviewable.

Delivery order:

1. P4.T0 establishes immutable installed configuration contracts.
2. P4.T1 implements writable-state reconciliation against those contracts.
3. P4.T2 adds durable transactions/journals against the reconciled state API.
4. P4.T3 captures immutable states from those journals and P2.T8 stores them.
5. P4.T4 promotes only verified P4.T3 captures through P0 governance.

#### P4.T0: Package Immutable Configuration And Compatibility Contracts

**Status: Completed.**

Description:
Give `III-Drone-Configuration` explicit schema versions, supported upgrade/
downgrade ranges, runtime-profile descriptors, exactly one tracked default each for
`real` and `sim`, and immutable package-share manifests. Install these through the
existing `ament_cmake_python` package and expose a side-effect-free planning API.
Remove source-tree preference so every environment consumes the same installed
contract before any writable-state migration begins.

Acceptance:

- [x] Compatibility can be evaluated from old/new installed manifests before
      runtime shutdown or writable-state access.
- [x] Package build/install deterministically includes schemas, profile descriptors,
      migration metadata, and `real`/`sim` tracked defaults without reading or
      mutating a writable runtime root.
- [x] Runtime profile descriptors explicitly map `real -> real`, `sim -> sim`,
      initial `opti_track -> real`, and reserved non-bootable `hil -> sim`; living
      selectors remain runtime-profile-scoped so aliases cannot overwrite each other.
- [x] Runtime package resolution uses the ament index and never silently selects a
      workspace source tree; development edits become inputs only after normal
      colcon build/install.
- [x] The public Python API requires explicit immutable input and writable-state
      roots, returns typed plans/results, and is shared by receiver, configuration
      service, CLI, simulation startup, and tests without duplicate policy logic.
- [x] Tracked-default/capture interfaces remain extensible to future non-default
      tracked sets without implementing that deferred catalog now.

Tests:

- Package-build side-effect isolation, deterministic install, ament-only resolution,
  source-tree shadow rejection, profile mapping/alias isolation, compatibility
  range fixtures, malformed/unknown schema rejection, and API import tests.

Implementation notes:

- Added a schema-versioned, content-identified immutable package contract with
  authenticated parameter schema, migration ranges, profile descriptors, and the
  exact `real`/`sim` tracked defaults. The tracked-set list enforces exactly one
  default per parameter profile while admitting future reviewed non-default sets.
- CMake now captures every immutable input into the build tree before installation;
  even `--symlink-install` points at build-captured bytes rather than editable
  source. A clean non-symlink isolated install reproduced the same authenticated
  bundle and loaded it through the ament index.
- Added typed `load_installed_contract` and `plan_compatibility` APIs with explicit
  absolute old/new immutable roots and writable-state root. Planning authenticates
  all inputs, records but never opens writable state, returns a deterministic
  no-mutation plan, and exposes isolated selectors for `real`, `sim`, `opti_track`,
  and reserved non-bootable `hil`.
- Removed Python and C++ workspace/writable-schema preference. Configuration
  service, Supervision, and Simulation now reach the same installed contract via
  the existing shared schema/seeding helpers; explicit `III_DRONE_SCHEMA_FILE`
  remains test/debug-only. Receiver and CLI mutation consumers use this same public
  plan/result surface in the following reconciliation and promotion tasks.
- Task-specific verification passed all 97 Configuration package tests plus the
  focused six-fixture contract rerun, Draft-07 schema/manifest validation, Python
  compilation/format/diff checks, deterministic rebuild comparison, zero writable-
  state build side effects, and a clean standard-install/API/hash drill.

#### P4.T1: Implement Transactional Parameter Reconciliation And Legacy Shadow

**Status: Completed.**

Description:
Implement the installed writable-state reconciliation API across every applicable
parameter set. Preserve current keys/values, add new keys with release defaults,
retire removed keys into canonical persistent shadow records, and block reintroduced
keys for explicit review. Simulation invokes it automatically before startup;
only the onboard receiver invokes it transactionally during aircraft activation or
rollback. Remove the legacy shell/standalone mutation path after parity.

Acceptance:

- [x] Development/simulation startup reconciles every writable sim set before
      selecting/applying one; failure or unresolved reintroduction prevents launch.
- [x] Aircraft reconciliation is never a build/install/runtime-start side effect;
      only the receiver executes the preplanned transaction against a staged copy.
- [x] Migration never edits the only aircraft copy; activation/rollback either
      proves compatibility or atomically restores the paired checkpoint.
- [x] Existing valid values are never overwritten, every new key receives the
      release default, and every applicable selected/unselected set is normalized.
- [x] Removed keys leave active files only after the selected active-at-retirement
      value/provenance is durable in a release/schema/set-scoped shadow record;
      inactive/snapshot/scattered values never become restoration candidates.
- [x] Shadow data remains outside release/active trees, is never executed as current
      configuration, is included in captures/backups, and supports deterministic
      compatible rollback rehydration.
- [x] Reintroduction produces the bound `.iii/operations/<operation-id>/` review
      showing old canonical value, new default, validation, provenance, and one
      unresolved `use_old|use_new_default` decision per key without mutation.
- [x] Invalid old values stay visible but cannot be selected; incomplete, stale,
      edited, cross-release, cross-manifest, cross-target, or cross-state reviews fail.
- [x] Reconciliation is idempotent, journaled, fsync/atomic-rename power-loss safe,
      and emits a complete per-set plan/result consumed by deployment reporting.
- [x] `iii config sim inspect/checkpoint/reset` operates on the current clone's
      Git-ignored living sim tree; reset is confirmed, first seals a recoverable
      capture, and never edits the tracked default.
- [x] Legacy `scripts/install.sh` and `update_installed_parameters.py` callers are
      migrated, then removed or fail with the canonical replacement next action.

Tests:

- Upgrade/downgrade/incompatible, preserved/new/removed keys, selected and inactive
  sets, shadow corruption, rollback rehydration, valid/invalid/partial/stale review,
  active-at-retirement provenance, scattered-value rejection, repeated/interrupted
  reconciliation, automatic sim blocking/resume, receiver-only aircraft mutation,
  sim inspect/checkpoint/reset/capture, and legacy-entry-point retirement.

Implementation notes:

- Added one typed reconciliation planner/executor shared by installed runtime,
  simulation, CLI, and receiver. It authenticates old/new immutable contracts,
  normalizes every selected and unselected set, preserves valid values, inserts
  defaults, and durably retires removed values before any active-tree rename.
- Canonical legacy shadows are release/schema/profile/set/target bound and keep
  only the selected value that was active at retirement as a restoration
  candidate. Reintroduction produces an operation-bound review; exact complete
  `use_old|use_new_default` decisions are authenticated before execution, while
  invalid old values remain visible but nonselectable.
- Simulation reconciles automatically before active-set selection. Aircraft
  startup is verification-only: activation preflights from an immutable source
  checkpoint, reconciles a receiver-private copy, seals a predicted content-
  addressed checkpoint, and atomically switches the code/configuration/catalog
  tuple. Rollback restores the exact paired checkpoint without re-migration.
- Added `iii config sim inspect|checkpoint|reset|review` and receiver-backed
  `iii deploy continue` review resumption. Reset always captures first, supports
  exact checkpoint restore, is confirmation/plan gated, and never mutates tracked
  defaults. Legacy standalone mutators now fail with the canonical next action.
- Configuration state, checkpoints, retained contracts, journals, and shadows are
  included by the portable-state configuration domain; a focused archive fixture
  proves shadow material is sealed and verifiable.
- Task-specific verification passed 22 Configuration reconciliation/contract
  tests, 48 CLI configuration/deployment contract tests, 74 receiver/deployment
  tests, the focused portable-shadow archive test, all 129 deployment schemas,
  Python/shell compilation, format/diff checks, and an isolated wheel dependency
  and import drill. Full Phase 4 regression is intentionally deferred through
  P4.T4.

#### P4.T2: Implement Tuning Sessions And Change Journaling

**Status: Completed.**

Description:
Implement one profile-parameterized session and transaction engine for simulation
and real targets. Introduce an explicit session identity, immutable baseline,
monotonic revision, target/profile, release/workspace identity, and manifest hash.
Keep this machinery internal: automatically open/resume it on accepted Apply and
do not add tuning-session start/end concepts to the GUI.
Journal accepted and rejected GUI parameter transactions with request identity,
old/new values, restart semantics, validation, readback, operator identity,
timestamp, and result. Replace per-key Apply with validate-all, durable intent,
apply/readback-all, durable commit, and compensating rollback semantics. Use an
append-only checksummed WAL and atomically replaced, file-and-directory-synced
checkpoints/selectors rather than direct YAML writes and process-memory cleanup.

Acceptance:

- [x] Session baseline cannot be confused with current mutable state.
- [x] Constant/restart-required values and pending boot state are represented.
- [x] Pending values become active after whole-graph `system stop`/`system start`
      at the configuration lifecycle boundary or `system restart --cold`; daemon
      shutdown/boot, OS reboot, and power cycling are unnecessary.
- [x] Pending indications clear only after the freshly configured runtime reports
      matching active readbacks; warm or unrelated-node restarts do not clear them.
- [x] A test may restore its baseline without changing code release.
- [x] The GUI receives success only after both journal and active-set mutation
      are durable; failure leaves neither a partial multi-parameter transaction
      nor an unreported value change.
- [x] Request IDs and expected revisions make retries idempotent and reject stale
      concurrent edits.
- [x] A failed distributed node update compensates already-applied nodes; failed
      compensation enters an explicit configuration-divergent fault with exact
      observed values and blocks further writes pending reconciliation.
- [x] Restart and power-loss recovery deterministically complete or abort every
      prepared transaction without inventing an accepted revision.
- [x] Journal compaction retains checkpoints and the complete current session.
- [x] The same conformance suite passes against `sim` and `real` profiles; only
      target adapters and profile data may differ.

Tests:

- Runtime, constant, rejected, repeated, concurrent, restart, rollback, and
  power-loss journal tests.

Implementation notes:

- Added one profile-parameterized, content-identified tuning engine with a
  separately authenticated immutable baseline and mutable state, monotonic
  revisions, target/profile/release/workspace/manifest binding, canonical
  checksummed JSONL WAL, fsynced atomic checkpoints/selectors, idempotent request
  replay, prepared-transaction recovery, baseline restore, and lossless active-
  session compaction.
- Replaced Runtime's per-key Apply loop and legacy snapshot-load mutation with one
  validate-all transaction. The server durably prepares, applies and freshly reads
  every live node, atomically persists the complete active set, then durably
  commits. Failure compensates every prior key/node. Failed compensation records
  exact observations, blocks all parameter/snapshot writes, and is cleared only
  after a later full-graph pass proves exact prior active, persisted, and pending
  state; interrupted reconciliation is WAL-replayable.
- Restart-required values stay distinct as persisted/pending state. A full managed
  stop/start or parameter cold restart starts nodes before confirmation, requires
  fresh matching whole-graph readback, and stops fail-closed on mismatch. Partial
  or warm node starts never confirm pending state.
- Runtime rejects noncanonical or extended session/transaction transport, requires
  both pending-state services to agree, exposes revision/session/baseline/pending/
  divergent status through ROS-free Contracts, and disables divergent writes. GC
  retains its existing edit/Apply workflow, sends expected revision, shows active
  to pending-next-cold-restart values, and adds only a compact divergence warning;
  no session start/end concepts were introduced.
- Real provisioning now owns `/var/lib/iii/tuning`, exports
  `III_TUNING_STATE_ROOT`, and grants only the system-daemon cgroup write access;
  simulation uses the clone-local Git-ignored `.iii/tuning` root. Portable-state
  policy already seals the tuning domain.
- Task-specific verification passed 45 Configuration durability/install tests,
  28 Runtime API/lifecycle tests, five Contracts tests, eight Interfaces manifest
  tests, 11 GC configuration-page tests plus typecheck/generated-contract check,
  20 production-systemd/portable-state tests, targeted four-package Jazzy colcon
  build, wheel-content inspection, Black, Pyflakes, compilation, and diff checks.
  The full Phase 4 regression remains intentionally deferred through P4.T4.

#### P4.T3: Export Provenance-Rich Tuning Captures

**Status: Completed.**

Description:
Publish committed revisions on the existing runtime event stream and add a host-
side III CLI mirror companion, automatically started by GUI field/simulation
workflows. It verifies hash/sequence continuity, backfills gaps, and checkpoints
incrementally under Git-ignored `.iii/operations/<session-id>/`. The GUI preserves
its current edit, edited/unsaved, and Apply interaction; it additionally shows
pending-next-cold-restart values and a compact degraded mirror warning when needed,
automatically rehydrates gaps, and never treats a success toast as state
synchronization. Support selecting and downloading arbitrary saved parameter sets
from either `iii.local` or a simulation runtime. Each selected set becomes an
independent immutable local capture with an operator-provided short name and
description, exact full values, profile, source snapshot and journal revision,
manifest, release/workspace identity, pending boot values, relevant provenance,
timestamps, and integrity hash. Pulls are non-destructive, repeatable, and never
change the active/default set. The target journal remains authoritative during
mirror loss and automatically backfills after reconnection.

Acceptance:

- [x] Capture integrity and release/schema correlation are verifiable offline.
- [x] Repeating capture does not overwrite prior evidence.
- [x] Any saved set, active or inactive, can be downloaded independently or as
      part of a multi-selection without first loading or making it default.
- [x] Each downloaded set receives a short name and description without changing
      its immutable source identity or onboard snapshot.
- [x] Deployment, restart, journal compaction, and generic storage cleanup never
      prune operator-named sets.
- [x] Deletion is blocked for active/default/pending sets and normally requires a
      verified local capture receipt; force deletion is separate and confirmed.
- [x] System-generated checkpoints compact only when no retained state, named set,
      or unexported capture references them.
- [x] Partial or interrupted captures are distinguishable from complete captures.
- [x] Drone and simulation use the same capture format and promotion input contract.
- [x] GUI close, mirror restart, network loss, and target reboot preserve session
      resumability and never silently lose or duplicate accepted revisions.
- [x] Mirror loss is visibly degraded but does not block target-durable tuning;
      detailed mirror/capture state remains CLI-facing rather than becoming a GUI
      tuning-session workflow.
- [x] The GUI exposes no tuning-session start/end vocabulary or controls and
      clearly distinguishes edited/unsaved values from values pending application
      on the next cold restart.
- [x] Snapshot list and parameter state update immediately from authoritative
      revisions, with gap detection and full rehydration fallback.
- [x] GUI download either exports a real sealed capture or is replaced by the
      local mirrored-capture action; it never reports discarded YAML as downloaded.
- [x] Captures are content-addressed under Git-ignored `.iii/captures/`; display
      metadata is separate from immutable identity.
- [x] CLI list/show/diff/verify/export/import supports portable checksummed
      archives, verified deduplication, duplicate display names, and no secrets,
      Git mutation, or parameter-default mutation.

Tests:

- Sim/real protocol conformance, live revision propagation, gap/backfill,
  reconnect, GUI/mirror/target restart, target reboot, mirror-absent tuning,
  capture/export/verify round trip, collision handling, interrupted transfer,
  stale UI prevention, and tampered capture rejection.

Implementation notes (2026-08-27):

- Contracts seal one strict `iii.configuration-capture/v1` content identity over
  exact values, raw snapshot checksum, real/sim logical identity, release,
  workspace and manifest identities, baseline, pending boot values, and the
  checksum-bound current WAL head. Profile/target, timestamp, revision, schema,
  and transaction provenance mismatches fail closed offline.
- Configuration exposes authenticated arbitrary snapshot reads, retained current
  and historical journal batches, implicit session creation, and receipt-bound
  named-snapshot deletion. Saving or reading a named set is non-destructive;
  active/default/persistence-pending references remain protected even under the
  separately confirmed force path. Runtime-snapshot cleanup cannot match named
  operator sets, and active-session compaction remains a validated evidence-
  retaining no-op.
- Runtime emits one authoritative revision event plus full configuration domain
  state after each newly accepted transaction, rejects stale mirror heads, and
  exposes CLI-token-scoped state/journal/capture/delete/ack routes. The GC mirror
  checkpoints every entry, backfills prior sessions and gaps, survives companion/
  target restart and mid-batch loss, and acknowledges only an exact complete head.
  Real provisioning uses `iii.local`; both simulation Compose workflows use the
  identical contract against `localhost`.
- `iii config capture` supplies pull/list/show/diff/verify/export/import/delete.
  Captures and receipts are immutable; display metadata has an independent content
  identity and permits duplicate names. Archives are deterministic, bounded,
  path-safe, unencrypted, checksummed, fully validated before mutation, crash-safe
  on publication, and resumable through verified deduplication. Pull/import
  interruptions retain canonical markers, and secret-bearing parameter names fail
  before publication.
- GC retains the existing edit/Apply and pending-next-cold-restart interaction,
  shows mirror degradation without blocking target-durable work, rehydrates a
  detected revision gap from the full authoritative patch, and replaces the fake
  discarded-YAML download result with an exact local capture command.
- Task-specific verification passed 41 Configuration durability/server tests,
  14 Contracts capture/configuration tests, eight Interfaces manifest tests,
  26 Runtime configuration API tests, nine CLI capture/transport tests, 17 GC
  companion/Compose tests, 18 frontend configuration/state tests, frontend
  typecheck and generated-contract verification, the targeted five-package Jazzy
  build, 30 deployment/systemd/portable-state tests with three environment skips,
  plus Black and diff checks. Phase 4 full regression remains deferred until P4.T4.

#### P4.T4: Implement Configuration Comparison And Promotion

**Status: Completed (2026-08-27).**

Description:
Compare a capture with its recorded baseline and current tracked configuration.
Support explicit classification into the shared tracked default, retained
capture evidence, or rejection. Write promoted values only into the corresponding
`real` or `sim` tracked default in `III-Drone-Configuration`, validate against the
source manifest, and integrate the resulting configuration-submodule commit and
workspace gitlink through the governed feature/stacked-PR workflow. Detect
source changes since the field baseline and require reconciliation instead of
silently overwriting files. Keep capture/export separate from source mutation.

Acceptance:

- [x] Promotion produces a reviewable minimal change.
- [x] Experimental tuning cannot silently become the shared tracked default.
- [x] Wrong schema, baseline, logical target, or release provenance fails closed.
- [x] Promotion has plan/apply modes, supports both `real` and `sim`, and never
      commits directly to protected `develop`, `main`, or `release`.
- [x] This deployment-scope promotion updates the selected profile's release
      default. Its capture and comparison interfaces remain extensible to future
      tracked non-default sets, whose catalog semantics are deferred.
- [x] Git commit identity and the eventual qualified release tag provide the
      version history and release binding for both defaults.
- [x] Successful promotion can create the coordinated configuration-submodule
      and workspace commits/PR metadata needed for inclusion in a later release.

Tests:

- Clean promotion, concurrent source change, schema mismatch, logical target/profile mismatch,
  simulation capture, real/sim default promotion, partial selection,
  stacked-PR integration, deprecated-key policy, and rejection fixtures.

Implementation notes (2026-08-27):

- Added `iii config promotion plan|apply` on the shared result and retained-
  operation-plan surfaces. Planning is side-effect free and requires one verified
  immutable capture, explicit `real|sim` profile, exact release and workspace
  ancestry, source-manifest identity, and per-key
  `shared-tracked-default` classification. Unknown, unchanged, removed,
  deprecated, unselected, cross-profile, and stale-source inputs fail closed.
- Apply performs a line-minimal scalar rewrite of only the selected profile's
  tracked default, preserving comments and all node-specific YAML sections, then
  reseals exactly the affected default identities and contract manifest. It
  refuses protected and non-governed branch names and never changes capture
  evidence or invokes a remote mutation.
- Optional commit mode creates the exact Configuration commit, updates and
  verifies the workspace gitlink lock, creates the coordinated workspace commit,
  and emits authenticated PR metadata for the repository-owned
  `create_stack_prs.sh --base develop --feature deployment-infrastructure-redesign`
  flow. It does not infer push or PR authorization.
- Thirteen focused promotion fixtures passed, covering real and simulation plans,
  read-only planning, partial selection, minimal resealing, real Git commits and
  stack metadata, provenance mismatches, deprecated keys, concurrent source
  reconciliation, schema mismatch, and protected branches. Production-format
  read-only probes for both profiles also passed.
- The final Phase 4 regression passed 754 Jazzy colcon tests across Interfaces,
  Contracts, Configuration, Runtime, and GC; 209 CLI tests; 592 deployment tests
  with five explicit environment/privilege skips; and all 128 frontend tests plus
  typecheck, lint (zero errors, three existing fast-refresh warnings), generated-
  contract verification, and production build. A discovered asynchronous logout
  assertion race was corrected and passed ten consecutive focused runs.

### P5: Validate, Document, Commission, And Retire Legacy Paths

Phase acceptance:

- [ ] Fresh provisioning, repeated field-development deployment, tuning capture,
      qualified release deployment, rollback, and recovery pass end to end.
- [ ] Operators can perform normal workflows through the III CLI without source
      knowledge or direct filesystem mutation on the aircraft.
- [x] Every maintained III document routes humans and AI agents through tested,
      automation-ready canonical commands and the settled branch/CI policy.
- [x] Legacy deployment paths are removed or clearly blocked.

Delivery order:

1. P5.T0 continuously assembles the verification matrix while P1–P4 land; it is
   not deferred until the end.
2. P5.T2 establishes documentation ownership/templates/validation early enough
   that each implementation task updates its own affected docs.
3. P5.T3–P5.T5 migrate and validate maintained documentation against the finished
   interfaces while P5.T1 executes the physical commissioning walkthrough.
4. P5.T6 is the irreversible cutover and runs only after every Q131 matrix row,
   documentation gate, recovery rehearsal, and commissioning check passes.

#### P5.T0: Build The Deployment Verification Matrix

**Status: In-Progress.** Bootstrap implementation started on 2026-08-25; this
task remains open until all software, target-equivalent, and physical acceptance
rows have final retained results.

Description:
Create layered tests for manifest/schema logic, source capture, builder output,
temporary-root release management, fake SSH targets, target-equivalent ARM64
hosts, first-boot images, and physical Raspberry Pi/hardware acceptance. Maintain
one machine-readable matrix mapping every settled decision and task acceptance
criterion to its test level, environment, owner command, required evidence,
blocking status, and most recent result. GitHub runs hardware-independent checks
and verifies signed local attestations; it never pretends to run Gazebo, QGC,
OptiTrack, PX4 hardware, or aircraft tests it cannot host.

Acceptance:

- [x] A repository-owned parser materializes every independent normative clause
      in Q1–Q132 as a stable `Q<question>.c<clause>` identifier; changing clause
      text, splitting/merging clauses, or renumbering a question requires an
      explicit reviewed mapping so traceability cannot silently drift.
- [x] The coverage-index audit resolves every focused-owner reference to exactly
      one extant backlog task, rejects duplicate/missing question rows and stale
      task identifiers, and rejects any clause whose owner task has no matching
      acceptance criterion and test/evidence path.
- [x] Every Q1–Q132 load-bearing contract and every backlog acceptance criterion
      maps to at least one automated check, scripted local check, or explicit
      signed physical acceptance step; uncovered rows fail the matrix audit.
- [x] CI runs all hardware-independent tests.
- [x] Simulation and hardware-required tests are scripted locally, selected by
      Q121 change-impact policy, and produce P2.T8-retained signed evidence that CI
      verifies by source/policy identity without replaying the test.
- [x] Upgrade and rollback cover clean and dirty releases plus tuned configuration.
- [x] `iii field prepare` populates every declared offline dependency and `iii
      field verify --offline` proves representative GC-only, drone-only, and paired
      build/package verification without network or target mutation.
- [ ] A scripted pre-field matrix can cold-switch one deployed release through a
      commissioned OptiTrack profile, collect profile-tagged evidence, return to
      default `real`, and revalidate field readiness without reinstalling artifacts.
- [x] `iii field check` implements the final Q125 connected GC/drone readiness
      contract, seals a non-mutating readiness record, and emits exact next actions
      for every warning/failure.
- [x] Readiness fixtures enforce Q126 stable finding IDs, pass/warn/fail exit
      statuses, signed warning acknowledgement without severity mutation, stale-
      record non-authorization, and unwaivable failure behavior.
- [x] Release-status tests cover Q127 withdrawal/unsafe propagation online and
      offline, no automatic in-operation switch, blocked flight, retained evidence,
      last-resort maintenance recovery, and qualified replacement.
- [x] Credential tests cover Q128 surviving-computer enrollment, signing-only loss,
      complete SSH-authority loss, mandatory reimage, state restore, and
      recommissioning with no hidden bypass.
- [x] P2.T8 record/archive tests prove a clean replacement GC can recover all
      declared non-secret local state from a verified external archive while
      generating new identity and keys.
- [ ] Q131 has a versioned cutover matrix row for every factory, release, field,
      failure, configuration, evidence, offline, documentation, and retirement
      scenario; all rows reference one exact candidate set and pass before cutover.

Tests:

- A repository-owned `iii verify deployment` entry point emits human summary,
  versioned JSON, JUnit where applicable, evidence paths, skipped-with-reason rows,
  and Q112 next actions. Unit fixtures validate matrix completeness; a signed local
  acceptance run validates the final physical matrix.

Implementation notes (software boundary, 2026-08-27):

- The deterministic matrix now contains 1,197 reviewed definitions: every parsed
  Q1-Q132 clause, every task acceptance criterion, and nine explicit Q131 factory,
  release, field, failure, configuration, evidence, offline, documentation, and
  retirement scenarios. Each row binds exact owner acceptance/test references,
  execution level, argv-safe owner command, evidence class, CI eligibility, and
  Q121 category. The current split is 923 host-independent, 155 target-equivalent,
  and 119 physical rows.
- Clause digests remain stable through a separately reviewed old/new migration
  map; coverage parsing rejects duplicate/missing questions, duplicate owners,
  unknown tasks, and tasks without acceptance/tests. Matrix and verification
  policy identities make definition drift fail closed, including drift in the
  bound Q121 change-impact policy.
- Added canonical `iii verify deployment` audit/evidence evaluation with human and
  `iii.command-result/v1` output, a versioned result payload, atomic JSON/JUnit,
  explicit not-run/skipped rows, required-level/complete gates, and contextual
  next actions. CI audits the exact definitions before its hardware-independent
  suites; audit-only success never upgrades missing execution to pass.
- Target-equivalent and physical recorders bind one clean candidate set, Q121
  selection, canonical result rows, Ed25519 `workstation-field` authority, and
  path-safe hashed artifacts. Host-independent evidence uses `ci-qualified`
  authority and JUnit. Evidence from stale policy/matrix, mixed candidates,
  wrong levels/categories, unknown rows, missing signatures, symlink escapes, or
  altered artifacts is rejected.
- Added `iii field verify --offline` after `iii field prepare`; it proves GC-only,
  drone-only, and paired cached packaging without network or target mutation.
  Added a read-only-plan/apply pre-field runner for the same deployed release's
  `real -> opti_track -> real` cold cycle. Any intermediate failure retains logs
  and forces a real-profile recovery attempt without converting the run to pass.
- Focused P5.T0 verification passed 60 deployment matrix, field, governance,
  release, portable-state, credential, and pre-field tests plus 71 CLI result,
  verification, field, access, records, and release tests. The two remaining
  unchecked criteria require the actual commissioned OptiTrack cycle and all nine
  signed Q131 scenarios against one clean qualified physical candidate; no such
  evidence is available in this environment, so P5.T0 remains In-Progress.
The final software-only phase audit also exercised every feasible opt-in target
gate: target-equivalent aircraft convergence, native systemd recovery/switching,
both supported GC Ubuntu matrices, and replacement import/fresh-identity
ordering all passed. These results remain target-equivalent evidence and do not
satisfy or reclassify the 119 physical matrix rows.
- The r11 pseudo-flash continuation reran the focused application activation,
  automatic rollback, field workflow, profile cycle, qualified-release,
  receiver-clock, portable-state, and verification-storage batch: 175 tests
  passed. Native systemd boot, service restart, failed-release recovery, and
  release switching passed separately in 12.80 seconds. The batch exposed a
  stale committed matrix: the canonical generator preserved all 1,197 rows and
  the policy identity while updating 145 `test_refs`; the matrix audit and the
  original batch then passed. This is target-equivalent evidence only.
- The 2026-09-04 software acceptance boundary completed under a hard 16-of-32 CPU
  limit. The canonical Jazzy phase suites passed 711 deployment tests with five
  explicit physical/privileged skips, 246 CLI tests, 761 selected III package
  tests, 17 simulation tests, 44 workspace transport/bag/GUI helper tests, three
  top-level integration tests, 30 smoke-runner tests, 128 GC frontend tests, and
  the frontend contract, zero-warning lint, typecheck, production-build, and
  zero-vulnerability audit gates. `iii verify deployment --audit-only` validated
  all 1,214 governed rows; the submodule lock and documentation gates passed.
- Live simulation then exercised canonical retained-plan boot/start/shutdown,
  every read domain, runtime lifecycle, simulated gripper open/close, mapper
  start/pause/freeze/stop, overview update, configuration and rosbag queries,
  custom-operation validation/start/cancel, authentication, proxy selection, and
  browser hydration without issuing Arm, takeoff, or other flight commands.
  All 39 non-flight mutating/read steps returned HTTP 200, and an independent
  read-only 25-domain run captured the authenticated Dashboard before logout.
  The run exposed and fixed stale Dashboard hydration selectors, a vulnerable
  Browserslist lock, root-owned generated frontend/build artifacts, Docker
  fallback writes into the source tree, a slow implicit install-time audit, and
  simulation format/CMake-policy warnings. These results close no physical Q131
  row; signed aircraft evidence and the commissioned OptiTrack cycle remain open.
- The 2026-09-04 R57 aircraft-connected five-area acceptance exercised the
  non-arming path for configuration management, field deployment, cross-build,
  mission catalog control, and real-profile lifecycle. Live configuration apply
  and snapshot save/load passed; a missing snapshot-download return was fixed and
  covered by a concrete adapter regression. System shutdown/boot, selected
  configuration/control startup, and a cold mission-executor restart passed on
  the Pi. Both `inspection-production` and the explicitly included
  `reach-charge-leave-experimental` mission were listed and inspected. The
  initial `opti_track -> real` configuration alias now consumes the reconciled
  receiver-owned real selector when no independent OptiTrack selector exists,
  without copying or mutating state; its focused tests passed.
- The same run fixed mDNS advertisement selecting `127.0.1.1`, restricted
  selected-node failure reports to their requested scope, started/stopped a real
  20,299-byte vehicle-status rosbag, completed a 79-file 3.66 MB immutable log
  pull, and inventoried 470 local records. Signed field release
  `800622360d9b4c64c8ca1b9c592335740a5a0bffefa3fd325bcc094a7b1707e4`
  binds the two mission entries and exact cached PX4 build
  `0e8ead95d3dc1fa425f2dcd8f1b51c745da918697e1f87a356d6df9d2c77950e`.
  Retained operation `five-area-r57-stage-enrolled` staged GC then drone in
  about 80 seconds, made no PX4 write, and deliberately skipped activation, so
  the older active flight runtime remained unchanged. Missing local trust and a
  non-enrolled SSH identity each failed closed before transfer and were corrected
  by binding the target-enrolled trust projection and provisioning key.

#### P5.T1: Commission The First Aircraft From Raw Image

**Status: In-Progress.** The complete fail-closed software, runbook, matrix, and
signed-evidence boundary is ready; execution requires the intended Raspberry Pi,
attached flight hardware, native GC/QGC, controlled power interruption, and one
exact qualified physical candidate.

Description:
Exercise the complete factory path on the intended Raspberry Pi and hardware:
image, first boot, Ansible convergence, identity/secrets, release install,
hardware readiness, field update, tuning capture, rollback, reboot, and recovery.

Acceptance:

- [ ] A wiped target reaches a healthy inactive real-profile runtime using only documented inputs.
- [ ] PX4, mmWave, camera, charger/gripper, runtime API, logs, and configuration pass acceptance.
- [ ] Recovery from deliberately interrupted activation is demonstrated.
- [ ] Power interruption is injected at each durable receiver/application selector
      boundary and boot reconciliation always reaches the specified old/new pair.
- [ ] Receiver A/B failure, withdrawn/unsafe anchor handling, low-storage rejection,
      network rollback, and `DEGRADED_CLOCK` recovery are physically demonstrated.
- [ ] A native-QGC GC performs qualified deployment, dirty/untracked field
      deployment, parameter/PX4/QGC capture, log/diagnostic pull, and fully offline
      rollback using only prepared caches.
- [ ] Provisioned and commissioned states are distinct and the final Q108
      commissioning evidence cannot be produced from a field-development bundle.
- [ ] Commissioning records and live validity follow Q109–Q110, including signed
      local/onboard evidence, invalidation by contract-changing maintenance, and a
      visible field-development overlay that preserves the qualified anchor.

Tests:

- Signed commissioning and Q131 acceptance records with exact artifact/source IDs,
  captured structured command output, power-cycle evidence, and external P2.T8
  archive receipt.

Implementation notes (software boundary, 2026-08-27):

- Raw imaging and provisioned-state finalization, hardware-role inspection,
  commissioning evaluation, release/field/readiness operations, receiver A/B and
  power-loss reconciliation, clock recovery, portable backup/restore, and the
  fail-safe real/OptiTrack/real cycle all have canonical scripts and immutable
  signed evidence contracts. A field-development bundle is structurally unable
  to produce release-commissioning evidence.
- `deployment/scripts/commission_aircraft.py` accepts only physical matrix rows,
  exact Q121 impact categories, artifact hashes below the evidence root, one
  exact candidate set, and a workstation-field Ed25519 signature. Final
  `iii verify deployment --require-level physical --require-complete` refuses
  partial or mixed-candidate evidence.
- Focused commissioning-support verification passed 113 hardware-role, field,
  pre-field profile, receiver transaction/clock, portable recovery, imaging,
  verification-matrix, and CLI tests. No physical target is reachable in this
  environment, so none of the eight physical acceptance items is marked complete
  and no commissioning/Q131 evidence has been fabricated.
- The physical factory walkthrough started on 2026-08-27. The source card's
  legacy `/home/iii` was preserved before erasure and raw-image operation
  `iii-image-aircraft-20260827-r3` completed full stream/readback identity,
  deterministic NoCloud seeding, flush, and hardware eject. This establishes the
  wiped-media starting point only; none of the eight acceptance items is checked
  until first boot, convergence, hardware acceptance, interruption/recovery, GC,
  and signed commissioning evidence pass on the same candidate.
- On 2026-08-28 the first boot and direct-link bootstrap access were proven, and
  a real three-run host transaction reached finalization. Physical execution
  found and fixed contract drift in `iii.host-provisioning-run/v1` plus an
  OpenSSH ownership defect in the permanent credential projection. Focused unit
  tests pass, and the full Noble/systemd/Ansible target-equivalent regression now
  proves a new permanent forced-command SSH session after bootstrap deletion.
  Fresh signed ARM64 provisioning artifacts were materialized from committed
  source as record `be08ec46a0a2b9bc772ac368aeb8f85951199191f8c3b3ff3260e1e115f93733`.
  The physical host remains deliberately inaccessible until the card is
  reimaged; no commissioning acceptance item is claimed from this failed first
  attempt.
- The second raw-image attempt reached a fully converged, zero-drift,
  finalized host and proved direct Ethernet plus `iii.local`, but physical
  testing found that signed receiver slot permissions prevented the `iii`
  forced-command account from executing the gateway after successful key
  authentication. The mode policy, pre-revocation finalizer check, and
  post-finalization functional SSH assertion are now red/green tested; the full
  target-equivalent lifecycle passed in 704.82 s. No commissioning criterion is
  upgraded from the failed attempt, and the next physical run must start from a
  newly governed image built from the committed fix.
- With the operator remote, the next destructive flash was explicitly deferred.
  An exact ARM64 pseudo-flash instead installed the corrected generation-1
  receiver, performed the signed generation-2 A/B switch, retained the fallback,
  executed every launcher as root without mutating either slot, and passed the
  complete target-equivalent Noble/systemd provisioning lifecycle. This expands
  preflight evidence but does not satisfy any physical commissioning item. The
  current onboard generation is unchanged, and the final physical run must use a
  newly materialized artifact from committed source.
- Clean-source r11 is now materialized and has passed the exact ARM64 pseudo-flash
  and downstream systemd/application/field/recovery gates recorded in P2.T3 and
  P5.T0. It remains provisioned-but-not-commissioned evidence: no attached-device,
  power-interruption, native-QGC, flight, or signed physical Q131 row is claimed.
- A 2026-08-31 authenticated no-touch inspection retained under
  `.iii/evidence/preflash-20260831/` proved Ethernet/mDNS access, one active
  machine credential, no pending host-maintenance transaction, and the expected
  old-generation `[pi3+]` parser rejection. Only charger/gripper was attached;
  camera, FMU, and both mmWave interfaces were correctly reported missing. The
  r11 receiver bundle and target-bound dry-run plan verified without apply. A
  portable-backup plan refused the old receiver's noncanonical live state, so no
  backup, receiver switch, reboot, configuration change, or other aircraft
  mutation was claimed. The pinned Ubuntu image, documentation, 1,197-row matrix,
  local GC boundary, and legacy-retirement audit all passed read-only inspection.
- Clean-source r12 then passed the isolated signed generation-1 install,
  generation-2 A/B commit, fallback retention, complete slot verification,
  native AArch64 launcher execution, and zero-bytecode mutation gate described
  in P2.T3. The drone was not contacted during this pseudo-flash, and r12 now
  replaces r11 as the exact candidate reserved for the deferred physical flash.
- After live topology discovery removed the false PX4 USB-role requirement,
  clean-source r13 repeated the signed generation-1 install, generation-2 A/B
  commit, verified fallback retention, native AArch64 launcher execution, and
  zero-bytecode gate described in P2.T3. The drone was not mutated; r13 now
  supersedes r12 as the candidate for the final physical flash.
- The no-touch phase regression passed all 655 deployment tests with the five
  explicitly gated target/systemd matrices skipped, plus all 230 CLI tests after
  the III configuration package was built and its installed ament index sourced.
  The same 655/5 and 230 phase gates passed again after r13 materialization and
  pseudo-flash. Focused documentation, verification-matrix, legacy-retirement,
  workspace source-setup, and SSH-manager coverage passed 49/49; the submodule
  lock and whitespace audits also passed.
- Physical PX4 connection discovery on 2026-09-02 found a host-networking defect:
  the image requested DHCP on every Ethernet device and therefore never assigned
  the Pi built-in link the PX4-default peer address. The corrected v2 cloud-init
  profile and converged Ansible baseline reserve USB Ethernet (`enx*`) for
  DHCP operator/recovery access, reserve built-in `eth0` as `10.41.10.1/24`
  toward PX4 `10.41.10.2`, allow only MAVLink `14540/UDP` and uXRCE-DDS
  `8888/UDP` from that subnet, and bind the runtime MAVSDK listener explicitly.
  The repair also closed stale host-unit/target identity propagation exposed by
  the runtime environment change. Focused red/green verification passed 38
  imaging/network/Ansible tests and 76 unit-contract/target/maintenance/release
  tests. The old onboard image cannot consume host-level Netplan or firewall
  changes through an application receiver update, so live IP/protocol evidence
  remains pending the newly governed physical flash; no PX4 connection or
  commissioning acceptance is claimed yet. The phase gate then passed all 656
  deployment tests with five intentionally opt-in system/GC matrices skipped.
  The relevant ARM64 target-equivalent first convergence, zero-drift repeat,
  injected-drift repair, finalization, and permanent receiver-access matrix
  passed all three scenarios in 667.87 seconds.
- Clean-source r14 then passed the isolated signed generation-1 install and
  generation-2 A/B commit with verified fallback retention. All receiver
  launchers executed from both immutable slots under native AArch64 emulation,
  all native extensions were AArch64, and neither slot gained bytecode. The
  retained evidence and identities are recorded in P2.T3. This closes the final
  target-equivalent gate for the PX4-Ethernet correction, but the old onboard
  host still cannot acquire that root-owned Netplan/firewall baseline. A physical
  canonical reimage remains required before ping, MAVLink heartbeat, uXRCE-DDS,
  or any physical Q131 row may be claimed.
- The release-owned PX4 network correction at workspace commit `3c1294c` now
  binds the static `10.41.10.1/24` companion and `10.41.10.2/24` FMU pair, exact
  `net.cfg`/`extras.txt` artifacts, dual MAVLink/uXRCE-DDS startup, firmware
  compatibility, and the real parameter manifest under one authenticated
  baseline. An authenticated read-only pull through the still-running old
  receiver reached no PX4 snapshot within 45 seconds, confirming that the
  unfixed onboard host cannot provide transport evidence through the new path.
- The first clean-source artifact attempt (`.iii/host-provision-r15`, record
  `6ae4fb84e08bd8bd6e87bd1e5592a5fbfef7b98e10605139a7c774d0f59dc508`)
  is permanently rejected: the host's legacy pip frontend emitted an
  `UNKNOWN-0.0.0` wheel and omitted required receiver dependencies. Commit
  `5323500` adds fail-closed exact local-distribution and runtime-closure checks;
  focused provisioning coverage passed 11 tests. A pinned pip 26.2/setuptools
  80.9.0/wheel 0.45.1 builder then produced complete clean-source r15b:
  generation-1 provisioning record
  `8cedcf67ff9dd2d4ca3e39821864ec51de3abe9222208e60c35e64e15bdd1793`
  with receiver `a6e09fe2a50d1566cad6aa45292e59e05994018b5ab68c6f7db52e83e53b199b`,
  and generation-2 update record
  `2c79a28ba512ab7de1cd34e83830160128c20389f3d6c110932cb1b6a152a5d4`
  with receiver `923023f4463d8c53fc09814cb5eb51f8849b6c631b163a8834c4198577b7080d`.
  R15b supersedes r14 for the physical flash, but has not been written to media;
  no physical PX4 connection or commissioning row is claimed.
- The corrected account model supersedes r15b with clean-source r17: `iii` is
  the key-only interactive field/development administrator with unrestricted
  passwordless sudo, `iii-deploy` is the unprivileged forced-command receiver
  transport without sudo/PTY/forwarding, and temporary `iii-bootstrap` is
  deleted after convergence. The exact signed generation-1 and generation-2
  artifacts passed the ARM64 pseudo-flash retained in P2.T3, and the full Noble
  target-equivalent lifecycle proved convergence, zero drift, drift repair,
  bootstrap removal, receiver-command restriction, and `sudo -n id -u` returning
  zero for `iii`. The physical card and aircraft remain unchanged; none of the
  physical commissioning acceptance items is upgraded by this evidence.

#### P5.T2: Establish Automation-Ready Documentation Architecture And Validation

**Status: Completed (2026-08-27).** The governed inventory, hierarchy, generated
references, and offline documentation gate are implemented. P5.T3-P5.T5 own the
content migration through this completed contract.

Description:
Inventory every maintained Markdown/reStructuredText document in the workspace and
editable III submodules, then classify it in a committed documentation manifest as
canonical, generated reference, contextual design, ADR, runbook, test/qualification
procedure, historical record, or excluded generated/vendor/third-party artifact.
Define one information architecture rooted at `docs/README.md` and
`CONTEXT-MAP.md`, with context ownership and explicit links from root/package
READMEs and `AGENTS.md`. Add a repository-owned `iii docs check` (or equivalent
shared library plus thin CLI command) so documentation drift is an enforceable
build/PR/release property rather than a manual cleanup exercise.

Acceptance:

- [x] A versioned documentation manifest lists every maintained document's owner,
      context, audience, authority/canonical status, lifecycle, source-of-truth,
      generated status, and qualified-release inclusion policy.
- [x] Generated, vendored, third-party, dependency-cache, build/install/log,
      dataset/artifact, and sealed historical-evidence trees are excluded by
      explicit rules and cannot accidentally become migration targets.
- [x] One authoring contract requires executable workflows to state purpose,
      scope/authority, prerequisites, supported host/profile, safety state,
      plan/dry-run, exact mutation, human and structured results, stable exit
      statuses, evidence, interruption/resume, rollback/recovery, and Q112 next
      commands. Non-runnable architecture docs link to owning contracts instead
      of duplicating operational steps.
- [x] `AGENTS.md`, `docs/agents/*`, `CONTEXT-MAP.md`, ADR indexes, root docs, and
      package docs form a non-cyclic discoverable hierarchy with one canonical
      location per rule. Agent instructions are concise routers, not a divergent
      copy of the operating manual.
- [x] The editable III repository inventory is generated or validated against the
      governed submodule policy and includes Contracts, Runtime, GC, CLI, and all
      other workspace-owned III repositories while excluding forks/third parties.
- [x] `iii docs check` validates manifest coverage, internal links/anchors, command
      existence and help signatures, JSON-schema references, file ownership,
      duplicate canonical rules, forbidden legacy terms/paths, and generated-
      reference freshness with deterministic human/JSON output.
- [x] Documentation validation runs in local preflight and required CI without
      network or aircraft access and is included in qualified-release evidence.

Tests:

- Manifest coverage fixtures, generated/vendor exclusion, broken links/anchors,
  missing command, stale help/schema output, duplicate authority, missing editable
  repository, forbidden legacy path/branch term, deterministic regeneration, and
  clean offline validation.

Implementation notes (2026-08-27):

- Added `iii.documentation-policy/v1` and a content-bound
  `iii.documentation-manifest/v1` covering all tracked Markdown/reStructuredText
  in the workspace and ten editable III repositories. Each row carries ownership,
  context, audience, classification, lifecycle, source-of-truth, release
  inclusion, and exact SHA-256; the manifest identity changes with any document.
- Added non-cyclic root, agent, and ADR indexes; exact editable-repository/lock
  reconciliation; explicit exclusions; unique authority/router validation; local
  links/anchors; parser-derived command existence/help; forbidden current paths
  and branch patterns; and deterministic generated CLI/schema references.
- Added read-only `iii docs check` with canonical human/JSON results and stable
  exit status. Dependency-governance CI stores its report and qualified-release
  CI retains it as qualification evidence, both fully offline.
- Focused verification passed 42 deployment/CLI documentation and result-contract
  tests. A real workspace invocation returned `III_DOCS_OK` for 140 governed
  documents, 89 maintained documents, and two generated references.

#### P5.T3: Migrate Deployment, Field, Recovery, And Operator Documentation

**Status: In-Progress.** The software and documentation acceptance boundary is
complete; the independent clean-computer and physical Q131 walkthrough remains.

Description:
Rewrite the canonical operator manual and affected package runbooks around the III
CLI workflows delivered by P1–P4. Cover the complete lifecycle from stock Ubuntu
and raw SD through commissioning, home release preparation, native GC/QGC setup,
dirty field iteration, tuning/capture, maintenance, diagnostics, disaster recovery,
and final retirement. Keep concise happy paths linked to detailed failure runbooks;
do not make an operator reconstruct a workflow from architecture notes or shell
implementation details.

Acceptance:

- [x] Canonical runbooks cover builder/GC provisioning, SD imaging, first boot,
      Ansible convergence, provisioned/commissioned states, qualified release
      fetch/deploy, dirty/untracked field deploy, component selection, profile/
      mission selection, field prepare/check, clock sync, and offline operation.
- [x] Configuration runbooks cover live GUI edit/unsaved/apply semantics, pending
      cold restart, sim/real reconciliation, removed/reintroduced values, named
      parameter-set capture, compare/promotion, PX4 parameter capture/apply, and
      QGC managed-setting capture without inventing a separate “GUI tuning” mode.
- [x] Recovery runbooks cover activation/receiver/network failure, client loss and
      reattachment, onboard automatic rollback, low disk, log pull/prune, unsafe
      release withdrawal, host maintenance, backup/reimage/restore, surviving-key
      enrollment, total credential loss, replacement GC, and physical SD recovery.
- [x] Every command block is executable from its declared environment, matches
      tested CLI help, identifies whether source checkout is required, and shows
      representative result/next-action structure without embedding volatile IDs,
      secrets, machine paths, or stale screenshots as normative truth.
- [x] Safety-critical procedures state exact stop conditions and never normalize
      maintenance overrides, credential bypasses, direct filesystem mutation,
      onboard compilation, or flight while readiness/clock/commissioning is failed.
- [x] The offline operator subset and matching generated command/schema reference
      ship with qualified GC assets/GitHub Release so field use does not depend on
      internet access; release manifests identify the documentation revision.

Tests:

- Independent human walkthrough and clean-computer scripted walkthrough from raw
  provisioning through Q131 cutover; documentation command extraction/help checks;
  offline rendering/search; and deliberate failure-recovery exercises.

Implementation notes (software boundary, 2026-08-27):

- Added the canonical deployment/field operations manual with native GC and raw
  SD paths, provisioned-versus-commissioned state, qualified and dirty field
  deployment, component/mission/profile choice, configuration/PX4/QGC capture,
  offline preparation, diagnostics, rollback, backup, credential-loss, and exact
  safety stop/recovery boundaries. Detailed runbooks remain linked authorities.
- Documentation validation now checks every fenced `iii` option against the
  selected parser help in addition to command paths. That review found and fixed
  stale backup `--apply` syntax and a retired shutdown option. CLI wrapper help is
  now side-effect free; focused regression proves `gc provision --help` never
  bootstraps the controller.
- Qualified manifests bind the exact documentation manifest/policy, operator
  manual, generated references, and included-document hashes. The signed release
  publishes a deterministic `iii-offline-documentation.tar.zst`; the signed
  release record and publication bind the asset. Tampered source is rejected.
- Focused P5.T3 verification passed 75 documentation, CLI wrapper, release
  pipeline, bundle, and qualified-publication tests; `iii docs check` returned
  `III_DOCS_OK` for 141 documents and 90 maintained documents. The independent
  clean-computer and physical Q131 walkthrough still requires intended hardware,
  so P5.T3 remains In-Progress rather than claiming production evidence.
- The r11 continuation reran the canonical offline documentation gate. Its first
  pass correctly detected that this backlog's retained evidence changed the
  governed document hash. Regeneration proved the inventory remained exactly 144
  documents with no additions/removals, both generated references were
  byte-identical, and only this reviewed backlog entry changed. The manifest and
  migration review were renewed, after which `iii docs check` returned
  `III_DOCS_OK` for 92 maintained documents and two generated references. The
  independent physical Q131 walkthrough remains open.
- The 2026-09-04 continuation repeated the executable documentation and generated
  reference checks after the field-build and live-GC corrections. The governed
  inventory remains 144 documents with 92 maintained documents and two generated
  references. The independent clean-computer/physical Q131 walkthrough remains
  the only documentation acceptance boundary not exercised autonomously.

#### P5.T4: Migrate CI, Branch Hygiene, Release, And AI-Agent Instructions

**Status: Completed (2026-08-27).** Branch, CI, release, agent, PR-template, and
stacked-operation instructions now share the enforced policy and retained-plan
boundary; the declared live GitHub rulesets have been reconciled and audited.

Description:
Replace current `staging`, ambiguous temporary “release branch,” manual pointer
refresh, and advisory-only governance prose with an exact automation-first model
shared by root docs, editable submodule docs, GitHub workflows/templates, scripts,
and agent instructions. Document the solo-maintainer policy without weakening PRs,
checks, provenance, or change authority. Optimize instructions for Codex/AI agents:
small discoverable contracts, deterministic commands, explicit mutation boundaries,
structured outputs, resumable operation IDs, and evidence-bearing handoffs.

Acceptance:

- [x] A canonical branch matrix defines allowed source/base pairs and exact chain:
      feature/work-sweep -> `develop` -> `promote/develop-to-main/*` -> `main` ->
      workspace-only `release` -> immutable `vX.Y.Z`; editable submodules stop at
      `main`, and no maintained instruction refers to `staging` as a live branch.
- [x] Docs explain strict gitlink/lock governance, linked submodule PR markers,
      post-merge gitlink refresh, content-identity equivalence, Q118 local evidence,
      Q121 impact categories, Q122 waiver limits, Q120 evidence reuse, SemVer,
      release notes, signed status withdrawal, and protected tag/publication flow.
- [x] Root and editable-repository `AGENTS.md`/agent docs state safe read-only work,
      allowed edit boundaries, dirty-worktree preservation, no-touch forks/third
      parties, required checks, plan/apply authority, external mutation rules, and
      how to resume partial PR/release/deploy operations through stable operation IDs.
- [x] PR templates, generated summaries, and workflow docs use machine-readable
      markers only as untrusted transport; signatures, refs, schemas, and queried
      GitHub state remain authoritative.
- [x] Human and agent workflows invoke the same P0.T11 primitives; no documentation
      instructs an AI to parse decorative output, bypass confirmation, push directly
      to protected branches, fabricate evidence, or assume a second reviewer.
- [x] Current `README.md`, `docs/dependency-governance.md`,
      `docs/repo-boundary-map.md`, `scripts/README.md`, workflow descriptions,
      promotion helper help, and agent-routing docs are reconciled in coordinated
      workspace/submodule PRs.

Tests:

- Policy-table fixtures for every allowed/rejected PR pair; documentation scans for
  stale `staging`/legacy promotion language; agent dry-run scenarios for feature PR,
  develop promotion, release publication, partial retry, stale ref, dirty tree, and
  permission denial; and live read-only ruleset audit comparison.

Implementation notes (2026-08-27):

- Reconciled root, dependency-governance, repository-boundary, scripts, agent,
  and PR-template documentation around the exact feature -> `develop` ->
  `promote/develop-to-main/*` -> `main` -> workspace-only `release` -> immutable
  SemVer chain. The solo-maintainer policy retains PRs/checks/evidence while
  correctly requiring zero invented second-party approvals.
- Added a shared editable-repository agent contract for safe reads, owned edit
  boundaries, dirty-tree preservation, fork/third-party exclusion, task/phase
  test cadence, explicit external mutation, and evidence-bearing handoff.
- Hardened `create_stack_prs.sh`: it compares exact local/remote feature SHAs,
  pushes a stale remote head instead of trusting branch existence, carries a
  stable operation ID, treats PR markers as untrusted transport, and retains an
  `iii.automation-plan/v1`. Apply refuses a missing or stale dry-run plan.
- The live ruleset audit initially found the workspace's declared immutable
  release-record tag ruleset missing. The retained reconciliation contained one
  create and 24 no-ops; apply created ruleset `21646100`. The post-apply audit
  `affc732f08e42bf98471880b091ae91269e0ee4a81a9f20a809ba0e56281dd84`
  passed all 25 rulesets across 11 repositories with no findings.

#### P5.T5: Reconcile Remaining Maintained III Documentation And Release References

**Status: Completed (2026-08-27).** Every maintained document is bound to an
explicit passed migration review; historical records, standalone package links,
environment boundaries, generated references, and release bindings are enforced.

Description:
Migrate all other maintained workspace and editable-III documentation through the
P5.T2 contract. Reconcile architecture, build/environment, runtime, supervision,
configuration, mission, simulation, GC, testing, operations, package README, ADR,
and context documents with the installed native runtime, host-QGC model, profile
composition, canonical III CLI, and repository boundaries. Preserve sealed research
evidence and historical documents as immutable history with clear status rather than
rewriting their past commands into present recommendations.

Acceptance:

- [x] Every document classified as maintained passes its assigned migration review;
      every intentionally historical document is visibly labeled/indexed and cannot
      be mistaken for current instructions.
- [x] Build/environment docs distinguish host, devcontainer simulation, pinned ARM64
      builder, native aircraft, and native GC/QGC paths; production instructions do
      not source `/home/iii/ws`, Humble, mutable sysroots, or onboard Docker.
- [x] Runtime/mission/configuration docs use installed ament resources, catalog IDs,
      explicit profile composition, durable parameter state, and daemon/runtime API
      ownership without duplicating the canonical supervision graph.
- [x] Simulation docs retain `/home/iii/ws` only where it is explicitly the
      devcontainer workspace, launch QGC natively on the host, skip clock alignment,
      and do not imply that deferred HIL or profile-specific OptiTrack work exists.
- [x] Generated CLI command reference and structured-output/schema reference are
      reproducible from source and included in docs/release checks; handwritten docs
      link to them rather than copy option lists that can drift.
- [x] Cross-repository links resolve at the governed source revision or use stable
      repository-relative locations; package docs remain useful when read from their
      own repository and from the composed workspace.
- [x] A qualified release records the exact documentation manifest/revision and
      publishes the offline operator manual plus generated references beside its
      artifacts and release notes.

Tests:

- Full `iii docs check`; built-document link/reference validation; generated-help
  diff; forbidden production-path scan; maintained/historical classification audit;
  package-standalone link checks; and qualified-release documentation assembly.

Implementation notes (2026-08-27):

- Added a content-bound `iii.documentation-review/v1` ledger with an explicit
  approval switch and exact coverage of all 92 maintained documents. The audit
  rejects missing, stale, duplicate, unapproved, or incomplete review rows.
- Added the historical-record index for all 23 immutable backlogs, plans, and
  evidence logs. The checker requires every historical record to remain indexed
  and keeps those past commands outside current-instruction validation.
- Package-local relative links may no longer escape their repository. Ground
  Control and Runtime references now use stable governed repository URLs, so the
  docs resolve both standalone and in the composed workspace.
- Production-path validation allows `/home/iii/ws` only in an exact reviewed
  development/simulation allowlist. The environment matrix now distinguishes the
  host, Jazzy devcontainer, pinned ARM64 builder, native aircraft, and native
  GC/QGC; simulation explicitly skips clock alignment and keeps HIL non-bootable.
- Qualified release manifests bind the review identity/hash and ship the review
  beside the policy, manifest, manual, and generated references. Focused tests
  passed 24 documentation/release cases; `iii docs check` returned
  `III_DOCS_OK` for 144 documents and 92 maintained documents; generated
  references regenerated byte-identically.

#### P5.T6: Remove Legacy CLI And Deployment Behavior

**Status: In-Progress.** Reversible removal and fail-closed retirement checks are
in progress. Repository archival and clean physical no-legacy acceptance remain
gated by the signed Q131 candidate.

Description:
After replacement acceptance, remove branch-based remote install, onboard
Docker image deployment, password SSH, source/build/log synchronization, stale remote branch
variables, Humble/sysroot runtime assumptions, devcontainer-owned QGroundControl
binary/configuration/lifecycle paths, and documentation references.
Archive the old deployment repository according to P0.T4 and Q131. Preserve its
history read-only and add a final migration pointer; do not copy its onboard
Compose-per-node runtime ownership into the replacement or delete historical
evidence. The Q61–Q63 pinned GC frontend/proxy containers remain host-managed by
the native GC updater and user-session services; they are not onboard runtime
containers and are not a legacy path.

Acceptance:

- [x] No supported CLI path can perform the retired destructive workflow.
- [x] Dependency and documentation searches find no accidental old entry point.
- [x] Historical migration notes identify the last legacy version and replacement commands.
- [x] `setup/remote.bash`, old deployment variables, password SSH, mutable branch
      checkout, `latest` image publication, `rsync --delete`, onboard build/image
      commands, onboard production containers, and devcontainer-owned QGroundControl
      entry points are removed or fail with exact replacement next actions. The
      host-managed GC frontend/proxy container path remains supported and tested.
- [ ] The legacy repository is archived only after the signed Q131 matrix passes;
      its archive metadata names the final commit/branch, replacement release/docs,
      and recovery location, and no active workflow depends on write access to it.
- [ ] A clean workstation and commissioned target complete normal operation after
      all legacy local clones/caches are removed from the test environment.

Tests:

- CLI regression tests; repository/documentation retired-pattern audit; clean-host
  run with the legacy repository absent; archived-repository link verification; and
  final signed Q131 no-legacy-dependency rerun.

Implementation notes (reversible boundary, 2026-08-27):

- Removed `setup/remote.bash`, its legacy repository URL/branch/directory
  variables, `iii build container`, `iii build cross-compile`, and bare
  `iii config`. The retired CLI paths included mutable `latest` publication,
  rsync-built emulation trees, privileged build containers, and arbitrary SSH;
  current release/configuration commands remain parser-inventoried.
- `scripts/remote/install_remote.bash` is retained solely as a no-mutation
  compatibility tombstone. It exits 64 with `iii gc provision --help` and
  `source setup/setup_dev.bash` next actions; a temporary-HOME test proves it
  writes nothing. Host-managed GC frontend/proxy Compose remains present and
  covered separately.
- Added a repository-owned active-tree retirement policy/audit and typed archive
  metadata. The audit rejects reintroduced repository variables, password SSH,
  destructive synchronization, mutable image tags, retired deploy/pull commands,
  and removed paths. The archive schema refuses `archived` until exact qualified
  release, documentation manifest, and signed Q131 retirement evidence IDs exist.
- Live read-only GitHub inspection confirmed the legacy repository is still
  unarchived, as required. Focused legacy, parser/result, SSH, GC application,
  target-definition, documentation/release, and policy tests passed 110 cases;
  the retirement audit passed with no findings. Stacked promotion wrappers now
  retain and revalidate exact automation plans before PR mutation.
- The Phase 5 software regression passed 754 Jazzy colcon tests, all 128 frontend
  tests plus generated-contract, lint, typecheck, and production-build gates, all
  three workspace integration tests, 221 CLI tests, and 626 deployment tests with
  five explicit environment/privilege skips. The gate found and corrected stale
  launch fixtures that had shared operation journals or expected runtime seeding
  of receiver-owned real configuration; targeted reruns and the phase suites pass.
- Repository archival, the clean-workstation/commissioned-target run with legacy
  clones and caches absent, and the final signed Q131 no-legacy rerun remain
  physical gates. The last two acceptance items stay unchecked and the task
  remains In-Progress.
- The 2026-08-29 active-tree retirement audit passed again with zero findings,
  while the full verification CLI deliberately rejected completion with 1,197
  `not_run` rows when no signed execution evidence was supplied. The legacy
  repository therefore remains unarchived and the two Q131-dependent acceptance
  items remain open, as required.
- The final remote software phase gate passed 654 deployment tests with five
  explicit opt-in target skips and all 229 CLI tests. The skipped aircraft
  Ansible, GC Ubuntu, and native systemd tests had already been run explicitly in
  this phase: aircraft convergence passed in 513.38 seconds, both GC matrices
  passed in 132.49/152.28 seconds, and systemd release switching/recovery passed
  in 12.80 seconds. No physical result is inferred from these suites.

## In-Progress

#### P3.T12: Bind And Verify The Exact PX4 Release

**Status: In-Progress.** Added 2026-09-03 after physical USB inventory showed
that compatible-version ranges alone cannot prove the FMU has the release-owned
firmware, DDS interface, network baseline, and complete parameter defaults.

**Description:**

Make the real-aircraft PX4 firmware a first-class, exact companion of every III
release without allowing an ordinary III deployment to write the flight
controller. Build and cache the pinned V6X firmware offboard, bind its full Git
commit/version/board/artifact identity, normalized uXRCE-DDS topic set, network
baseline, and complete real parameter manifest into the signed release. After
the III component is staged, query the FMU through the Raspberry Pi's dedicated
Ethernet link. Activation proceeds only when the FMU is reachable, disarmed,
and matches every observable release-owned invariant. Otherwise retain exact
read-only evidence and return a dedicated PX4-release-required result with a
separate, explicit manual firmware/configuration remediation workflow. Rerunning
the same III deployment must be idempotent for an already-staged/current Pi
release while always repeating the PX4 audit.

Acceptance:

- [x] One canonical PX4 release contract binds the exact 40-character Git commit,
      semantic firmware version, V6X board target, firmware artifact hash, build
      recipe/cache identity, normalized uXRCE-DDS publications/subscriptions,
      network baseline identity, and complete real parameter-manifest identity.
- [x] The qualified release pipeline builds or reuses a content-addressed cached
      PX4 firmware artifact and rejects stale, dirty, wrong-board, wrong-commit,
      malformed, or unbound build evidence.
- [x] The signed III release manifest and bundle carry the exact PX4 contract and
      flashable artifact; version ranges cannot substitute for exact identity.
- [x] A receiver-owned, read-only Ethernet audit distinguishes unreachable PX4,
      firmware/version mismatch, topic-contract mismatch, network mismatch, and
      complete parameter drift; it performs zero FMU writes and retains evidence.
- [x] III activation fails closed with a stable PX4-release-required result and
      actionable separate-flow instructions, while a matching rerun is a Pi
      no-op and repeats the PX4 audit before activation.
- [x] Operators can pull a complete disarmed FMU parameter inventory and promote
      it as reviewed repository defaults only when its exact firmware contract
      matches; calibration identity remains explicitly preserved.
- [x] The separate PX4 release flow prepares authenticated firmware, `net.cfg`,
      `extras.txt`, and parameter-default artifacts. USB is the canonical
      firmware/configuration maintenance path; microSD copying is recovery-only,
      and the result is verified read-only before III activation.
- [x] Focused unit/integration tests cover cache hits, all mismatch classes,
      hostile evidence, no-write behavior, idempotent redeploy, and generated
      artifact drift; the Phase 3 suite and target-equivalent checks pass.
- [ ] Final physical acceptance applies the prepared PX4 candidate through USB,
      reruns the already-staged III release, and records a healthy Pi-to-PX4
      Ethernet audit plus fresh uXRCE-DDS delivery while disarmed.

Tests:

- Focused PX4 contract, build-cache, release-media, receiver-protocol, deployment,
  parameter, network, documentation, and qualified-release tests.
- Full deployment and CLI phase regression suites after the implementation is
  complete.
- Real `px4_fmu-v6x_multicopter` build at the pinned source commit, followed by
  first-build/cache-hit and prepared-media verification.
- Final physical USB update and Pi-to-PX4 Ethernet audit remain required before
  the final acceptance item can close.
- 2026-09-03 field USB exercise: the exact prepared V6X firmware SHA-256
  `f6ac33e8d5372bc7884e15d07e9e448211cd301baea56809a1cb985c91532620` was
  flashed and booted disarmed as PX4 v1.16.1 at commit prefix `7f41496535`.
  Exact `net.cfg` and `etc/extras.txt` bytes were written and read back through
  USB MAVLink FTP; PX4 then emitted 14541/UDP to the Pi at `10.41.10.1`.
  The release parameter export contains 176 names absent from this v1.16.1 FMU
  schema (largely legacy/simulation parameters), so 739 matching values were
  applied and persisted but the final audit remains deliberately open. The
  receiver also exposed an SD-read timeout crash; it is now covered by a
  fail-closed regression test and must be included in the next receiver update.
- Passed the real `px4_fmu-v6x_multicopter` build at
  `7f41496535c54924dfb33a25a27be88b4b134a30`; the resulting 1,771,162-byte
  image has SHA-256 `f6ac33e8d5372bc7884e15d07e9e448211cd301baea56809a1cb985c91532620`.
  A clean-cache build reported `cache_hit=false`; an identical second build
  reported `cache_hit=true` with the same build identity.
- Exercised `iii px4 release prepare` against that exact image. It created the
  firmware, 915-value QGroundControl parameter export (with 108 calibration and
  identity values preserved rather than overwritten), `net.cfg`,
  `etc/extras.txt`, canonical release record, and instructions with zero FMU
  writes.
- Passed 682 deployment tests with five explicit target opt-ins skipped and all
  232 CLI tests. The Docker-capable host then passed all three aircraft
  target-equivalent first-convergence, idempotence, and drift-repair checks in
  623.98 seconds.
- The 2026-09-04 release/field dry run bound one signed paired drone/GC bundle to
  the exact component source manifests, PX4 contract, selected real profile,
  `inspection-production`, and the explicitly included
  `reach-charge-leave-experimental` mission. Planning succeeded only for that
  exact selection; omitting the experimental mission failed closed before any
  staging or activation. The full field command retained and validated its
  operation plan without contacting the aircraft. Final physical USB application,
  Pi-to-PX4 Ethernet verification, and fresh uXRCE-DDS delivery remain the sole
  acceptance item left open for this task.

## Completed

### P0: Resolve Architecture And Contracts

#### P0.T0: Complete The Deployment Design Interview

Description:
Used the `grill-me` decision tree and delegated closure authority to resolve target
hardware, platform, image, release, transport, safety, configuration, security,
fleet, recovery, evidence, documentation, and operational policy, then reconciled
the resulting decisions into the implementation tasks.

Acceptance:

- [x] The open decision register contains no implementation-blocking ambiguity.
- [x] Q1–Q132 are settled or explicitly rejected with a retained reason; no entry
      remains `Unanswered`.
- [x] Rejected alternatives and their load-bearing reasons are retained.
- [x] Dependencies between decisions are reflected in phase/task ordering.

Tests:

- Completed manual backlog review against the settled decision record.
- Passed `! rg -n '^- \*\*Q[0-9]+ .*Unanswered' codex-backlogs/deployment-infrastructure-redesign.md`.
