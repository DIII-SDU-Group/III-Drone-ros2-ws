# Configuration System

## 1. Purpose

`iii_drone_configuration` provides schema-backed parameter governance for the whole stack:
- file-backed parameter definitions
- runtime declaration and distribution
- validation and constraints
- persistence and profile switching

## 2. Key Components

1. `configuration_server_node.py`
- Optional coordination authority while running.
- Provides manifest, snapshot, durable batch-Apply, and session-status services.
- Handles profile-scoped active parameter-set selection for `real` and `sim`.

2. `tuning.py`
- Owns immutable baselines, monotonic revisions, canonical checksummed WALs,
  atomic checkpoints/selectors, idempotent retries, and crash recovery.
- Keeps the session internal; GUI workflows do not start or end sessions.

3. `parameter_handler.py`
- Parses YAML into structured parameter entries.
- Validates types, ranges, options, and expression-based constraints.
- Detects changed parameters when loading new files.

4. `configurator` abstractions (Python and C++)
- Client-side utilities to declare/get parameters by bundle.
- Used extensively by C++ nodes through `Configurator<T>` patterns.

5. `configuration_client_node.py`
- Client utility (text UI style) for interacting with config services.

## 3. File Model

### 3.1 Profile-Scoped Parameter Sets

Tracked defaults are `config/parameter_sets/{real,sim}/tracked/default.yaml`.
Living selectors and snapshots are rooted below
`$CONFIG_BASE_DIR/iii_drone/`; every set is a standalone ROS parameter file.

### 3.2 Schema Manifest (`config/parameters/parameter_manifest.yaml`)
The schema manifest stores managed parameter definitions with:
- `type`
- `value`
- optional: `constant`, `min`, `max`, `options`

### 3.3 Snapshot Files (`$CONFIG_BASE_DIR/iii_drone/parameter_sets/<profile>/snapshots/*.yaml`)
Saved runtime snapshots use the normal ROS parameter-file format and are managed by the optional configuration server.

## 4. Runtime Selection Logic

`SIMULATION` selects the runtime profile, whose living selector names the active
set. The installed immutable contract maps runtime profiles to parameter profiles
and authenticates the schema and tracked defaults before writable-state use.

Paths are resolved under:
- `$CONFIG_BASE_DIR/iii_drone/...`

## 5. Service Contract Role

Configuration server services are used by:
- core nodes during configuration
- mission and control nodes via configurator access
- supervision/GC tools for live parameter management and profile switching

High-value services in operations:
- `load_parameters` (load snapshot)
- `save_parameters` (persist current)
- `apply_configuration_transaction` (revision-bound atomic operator batch)
- `get_configuration_session` (durable baseline/revision/pending/fault status)
- `get_configuration_journal` (cursor-bound authoritative WAL backfill)
- `get_parameter_file` (non-destructive arbitrary-set retrieval)
- `delete_parameter_file` (receipt- or force-confirmed inactive-set deletion)

Batch Apply validates every edit before a durable prepare record, applies and
reads back all live values, atomically persists the active set, then durably
commits. Failure compensates prior updates; failed compensation enters an
explicit divergent fault and blocks writes. Restart-required values stay pending
until fresh whole-graph readback after full stop/start or a cold restart.

## 6. Capture And Mirror Contract

Accepted tuning revisions are published as `configuration_revision` events and
full configuration-domain patches. A missed revision is therefore detectable,
and the next full patch rehydrates parameter and snapshot state without treating
a success toast as synchronization.

The target WAL remains authoritative. The GC mirror authenticates sequence and
checksum continuity, writes each immutable entry beneath
`.iii/operations/<session-id>/journal/`, checkpoints its cursor after every
entry, and acknowledges only the exact target head. Release turnover does not
hide the prior session: retained target sessions remain readable for backfill.
Mirror loss is reported as degraded but does not block a target-durable Apply.

Use the CLI to seal saved sets locally; retrieval never loads the set or changes
the active/default selector:

```bash
iii config capture pull --target sim \
  --snapshot snapshots/tuned.yaml --name tuned-hover \
  --description "Stable hover tuning after indoor trial"

iii config capture list
iii config capture show <capture-id>
iii config capture diff <capture-id> --against baseline
iii config capture verify <capture-id>
iii config capture export --capture-id <capture-id> --archive tuning.zip
iii config capture import tuning.zip
```

Captures are content-addressed under Git-ignored `.iii/captures/<capture-id>/`.
The immutable source binds the complete semantic ROS parameter document (including
wildcard, nested namespace, and node-specific sections), source YAML hash, target/profile,
release/workspace and manifest identities, baseline/session, current WAL head
entry (including its operator transaction), pending-boot state, and timestamps.
Operator short names/descriptions are separate content-addressed metadata, so
duplicate display names and repeated pulls never overwrite source evidence.
Portable archives authenticate every member and reject secret-bearing parameter
names, unsafe paths, tamper, and partial content.

Named snapshots are not generic cache. Deployment, restart, reconciliation, and
runtime-snapshot cleanup do not prune them. Normal deletion is allowed only for
an inactive, non-default, non-pending named set and requires its verified local
capture receipt. Force deletion is a separate planned operation with the exact
`delete:<snapshot-id>` confirmation. Current-session journal compaction is a
validated no-op; complete WAL/checkpoints and retained sessions remain available
while mirrors or captures may reference them.

## 7. Capture Comparison And Source Promotion

Capture/export is always non-destructive. Promotion is a separate, explicit
feature-branch workflow. It requires the capture's exact release ID, real/sim
profile, current source manifest, workspace commit ancestry, and field baseline;
source drift returns a reconciliation requirement instead of overwriting it.
Every promoted key is named individually and classified as
`shared-tracked-default`. Other differences remain `retained-capture-evidence`;
removed, missing, or unknown keys are classified `rejected` and cannot be selected.

```bash
iii config promotion plan --capture-id <capture-id> --profile sim \
  --release-id <release-id> --classification shared-tracked-default \
  --key /control/example_gain

iii config promotion apply --capture-id <capture-id> --profile sim \
  --release-id <release-id> --classification shared-tracked-default \
  --key /control/example_gain --commit \
  --operation-id promote-sim-example --confirm --non-interactive
```

Apply performs a minimal scalar-line edit only in the selected profile's
`config/parameter_sets/<profile>/tracked/default.yaml`, then re-seals the two
corresponding hashes and `manifest_id` in the configuration package manifest.
It refuses `develop`, `main`, `release`, `promote/*`, and `codex/*` branches.
Optional `--commit` creates exact Configuration and workspace gitlink/lock commits
and emits `iii.configuration-promotion-pr-metadata/v1` for the canonical
`create_stack_prs.sh` flow. A qualified release tag later binds those Git commits;
promotion itself neither publishes nor qualifies a release.

## 8. Validation Semantics

`ParameterHandler` enforces:
- strict parameter naming rules
- declared type/value consistency
- range checks and options checks
- cross-parameter expression references (e.g., min/max based on other values)

Implication:
- Parameter files encode both values and constraints, not only flat config values.

## 9. Operational Importance

This subsystem is foundational. Failures here can block bringup of nearly all dependent nodes because configurator lookups and parameter declarations are deep in startup paths.
