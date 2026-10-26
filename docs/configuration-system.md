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

Batch Apply validates every edit before a durable prepare record, applies and
reads back all live values, atomically persists the active set, then durably
commits. Failure compensates prior updates; failed compensation enters an
explicit divergent fault and blocks writes. Restart-required values stay pending
until fresh whole-graph readback after full stop/start or a cold restart.

## 6. Snapshot Retrieval And Mirror Contract

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

A named snapshot stays on the aircraft. To keep a copy, use **Download** on the
GUI Configuration page. It sends the runtime API's read-only
`configuration.snapshot.download` command, which returns the snapshot YAML after
the runtime has checked its content SHA-256, and saves the file under the
snapshot's file name. Retrieval never loads the set or changes the
active/default selector. A downloaded set becomes a tracked default only through
a reviewed change to `config/parameter_sets/{real,sim}/tracked/default.yaml`
and its contract hashes (see section 3).

Named snapshots are not generic cache. Deployment, restart, reconciliation, and
runtime-snapshot cleanup do not prune them, and no operator command deletes
them. Current-session journal compaction is a validated no-op; complete
WAL/checkpoints and retained sessions remain available while mirrors may
reference them.

## 7. Editing Configuration During Development

Configuration is ordinary editable source. Make a focused change in the selected
profile, rebuild the affected III package if required, and use `iii deploy dev`
to synchronize it to the Pi. Keep normal Git history when it is useful, but the
onboard workflow does not require a release ID, source seal, promotion command,
or a signed configuration artifact.

### 7.1 Living Parameters And The Tracked Default

Each runtime keeps a living parameter set per profile. A start only adds new
parameters to it and keeps every value that already exists, so a deployment
never changes an aircraft tuned in the field. A changed tracked default
therefore does not reach an aircraft that already has a living set until it is
synchronized on purpose:

```bash
iii config sync --host <pi> --profile opti_track --dry-run
iii config sync --host <pi> --profile opti_track --mode defaults
```

- `--mode keep` (the default) writes nothing and lists every living value that
  differs from the tracked default.
- `--mode defaults` writes the tracked default's values into the living default
  set (`tracked/default.yaml`) and selects it. `--parameter NAME` (repeatable)
  limits it to those parameters; without it every parameter is set.
- Nothing is overwritten without a copy: the set that is replaced is first
  saved as `snapshots/pre_sync_<UTC time>.yaml` in the same profile, where the
  operator interface can load it again.
- The system must be shut down to write, because the configuration server
  would write its own values back. The values take effect at the next start.

From a ground computer the command runs on the Pi and this checkout's tracked
default travels with it, so an edited default needs no build. A parameter that
is new in the default but not yet in the Pi's installed schema is reported and
left out: it arrives with `iii deploy dev --build`. On the runtime host itself
(the Pi, or the devcontainer for `sim` and `hil`) run it without `--host`.

`iii deploy dev --parameters defaults` runs the same synchronization for the
Pi's provisioned profile after the restart; `--parameters keep` is the default.

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
