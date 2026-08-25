# III-Drone Deployment

This bounded context defines release construction, host convergence, activation,
configuration persistence, recovery, and operator evidence. Runtime process and
control ownership remain defined by the workspace Operations context and existing
ADRs; deployment never recreates the canonical supervision graph.

## Language

**Qualified Release**:
A clean, evidence-bearing, signed paired release created only by protected CI from
an immutable strict-SemVer tag whose workspace commit belongs to `release` and
whose governed dependency state satisfies qualification policy.
_Avoid_: Stable build, release image, production branch build

**Field-Development Release**:
A signed immutable release built offboard from any non-qualified source state,
including dirty, untracked, or modified-submodule content, with complete source
provenance and no ability to claim or replace qualified status.
_Avoid_: Dirty deploy, rsync build, latest

**Release Pair**:
Independently installable GC and drone component artifacts sharing one release
identity and explicit API/schema compatibility ranges.
_Avoid_: Monolithic image

**Aircraft Configuration**:
Persistent, writable, profile-scoped III parameter state reconciled against an
immutable installed release manifest. It lives outside release slots and is
versioned independently from code activation.
_Avoid_: Config files in the release

**Tuning Session**:
An internal, target-authoritative, resumable sequence of atomic parameter
transactions with an immutable baseline, monotonic revision, target/profile,
manifest and release/workspace identities, and operator provenance.
_Avoid_: GUI tuning mode

**Capture**:
A sealed, content-addressed and checksummed export of a named parameter set or
other portable evidence. Mutable display name and description are not part of its
immutable identity.
_Avoid_: Downloaded YAML

**Activation**:
The receiver-owned, maintenance-safe transaction that binds a staged release,
configuration checkpoint, and mission catalog, switches the active selector, and
enters bounded health evaluation.
_Avoid_: Restart, install

**Acceptance**:
The durable receiver decision that a candidate has satisfied every required
identity, configuration, hardware, service, ROS lifecycle, PX4, ownership, and
stability gate. Only acceptance ends automatic rollback authority.
_Avoid_: Process started, deploy command returned

**Rollback**:
A safety-gated or transaction-internal restoration of a previously accepted,
schema-compatible code/configuration/catalog pair. It does not rewrite releases or
silently run after a candidate was durably accepted.
_Avoid_: Checkout, downgrade branch

**Deployment Receiver**:
The independently supervised host component outside application release slots that
owns verification, staging, privileged selectors, durable operations, health
deadlines, boot reconciliation, and rollback.
_Avoid_: Remote deploy script

**Protected Qualified Anchor**:
The newest accepted qualified release retained as the recovery baseline and never
replaceable or collectable by field-development deployment.
_Avoid_: Previous build

**Qualified-Tag Preflight**:
A read-only, fail-closed proof that a proposed strict-SemVer tag identifies the
exact clean workspace `origin/release` head, the dependency lock verifies, and
complete retained check evidence is bound to that commit, version, and lock.
_Avoid_: Tagging from a clean-looking checkout

**Commissioned**:
A provisioned shared hardware-class target with a qualified anchor and signed,
current hardware/PX4/activation/rollback/GC/recovery evidence. A provisioned host
or a field-development release alone cannot establish this state.
_Avoid_: Installed

**Readiness Record**:
A sealed, non-mutating observation of local GC and connected-target state using
stable PASS/WARN/FAIL findings. It becomes stale on relevant state changes and is
never a reusable authorization token.
_Avoid_: Safety approval

**Release Status Statement**:
An append-only, independently signed statement classifying an exact qualified
release as `qualified`, `withdrawn`, or `unsafe` without changing its tag or assets.
_Avoid_: Deleting a bad release

**Local Record Registry**:
The Git-ignored, host-user-owned `.iii/` registry for content-addressed releases,
operations, captures, backups, commissioning/readiness records, evidence, status
indexes, and archive receipts. It explicitly excludes every private credential.
_Avoid_: Build cache

**Portable Record Archive**:
A deterministic checksummed export of selected non-secret local records and
referenced blobs for operator-managed offline disaster recovery.
_Avoid_: Repository backup

**Cutover**:
The evidence-gated retirement of all legacy deployment paths after one exact
candidate set passes the Q131 acceptance matrix. Cutover archives legacy history;
it does not delete or rewrite it.
_Avoid_: Removing old scripts first

## Relationships and invariants

- Builds run only on supported operator/CI builders, never on the aircraft.
- Host systemd owns the receiver, daemon, and runtime API. The daemon remains the
  sole owner of the canonical ROS process graph.
- Releases are immutable and side by side; mutable aircraft state lives under
  persistent host paths outside release directories.
- The CLI plans and submits operations. Once accepted, the receiver owns their
  completion, deadline, reconciliation, and rollback without a connected client.
- Qualified and field-development releases use the same signed artifact and
  activation path, but their classification and retention authority differ.
- Qualified tag publication defaults to a plan. Apply pushes one previously
  unused version ref only after exact release-head, recursive cleanliness, lock,
  and evidence checks pass. Tag-triggered CI independently rechecks tag identity
  and release reachability before it can classify a manifest as qualified.
- Only a receiver-accepted qualified activation updates the protected qualified
  anchor. Staging, failed/rolled-back activation, and every field-development
  result preserve the existing anchor.
- Deployment activation, configuration reconciliation, and source promotion are
  separate transactions with distinct authority and evidence.
- Total SSH-credential loss has no software bypass. Recovery is read-only salvage,
  physical reimage, restore of portable state, fresh enrollment, and recommissioning.
- The operating manual, CLI schemas, CI, and agent instructions share one policy
  implementation; PR text and decorative output are never trusted inputs.
