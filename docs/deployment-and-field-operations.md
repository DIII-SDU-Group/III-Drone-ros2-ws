# Deployment And Field Operations Manual

This is the canonical happy-path manual for provisioning and operating the
III-Drone deployment system. It routes exceptional cases to focused runbooks and
uses only the public `iii` command surface. The generated
[command reference](generated/iii-command-reference.md) is authoritative for
options; the [schema reference](generated/deployment-schema-reference.md) is
authoritative for structured payload fields.

## Scope, Authority, And Safety

Use this manual from a native Ubuntu 22.04 or 24.04 x86_64 ground-control (GC)
computer. A source checkout is required only for first GC provisioning, SD-image
preparation, aircraft Ansible provisioning, source-based field builds, and
repository/qualification work. Installed `iii` owns ordinary field operation.
The aircraft is a native Ubuntu 24.04 ARM64 host; never source a development
workspace, build onboard, edit release trees/selectors, or run production in a
container there. QGroundControl is the pinned native GC application.

Every mutating command follows the same boundary:

1. run it with `--dry-run --operation-id <stable-id> --output=json`;
2. retain and review old/new identities, required checks, permissions, and exact
   mutation list;
3. execute only the returned resume/apply command; and
4. keep the structured terminal result and evidence paths.

Planning is read-only and is never authority to apply. A changed ref, input,
target state, or expired nonce requires replanning. Exit `0` is success, `10` is
warning, `20` is a policy/safety rejection, and `30` is an execution/load failure.
The JSON result's `next_actions` contain argv arrays; do not parse decorative
human text or copy a `shell_command` without reviewing its arguments.

Stop immediately if the target is armed, airborne, owns a flight reference, has
stale/disagreeing safety state, reports failed clock/readiness/commissioning, or
cannot authenticate the qualified anchor. Do not normalize an override,
credential bypass, direct filesystem repair, or flight under a failed gate.

## 1. Provision The Native GC

Start from a normal graphical Ubuntu installation. The repository wrapper builds
the pinned controller in a content-addressed user-local environment; it does not
make the devcontainer authoritative. Prime sudo separately so no password enters
arguments or retained records:

```bash
sudo -v
tools/III-Drone-CLI/bin/iii gc provision --dry-run \
  --operation-id gc-provision-initial --output=json
```

Review the result and run only its confirmed resume command. Then verify:

```bash
iii gc status --output=json
iii qgc status --output=json
iii docs check --root . --output=json
```

For a prepared disconnected installation, add `--offline --offline-cache
<verified-cache>`. For a replacement computer, instead supply
`--replacement-archive <verified-portable-archive>` before creating fresh local
identity and credentials. Full prerequisites, application-slot behavior,
graphical login/logout, and native QGC ownership are in
[GC host provisioning](gc-host-provisioning.md).

## 2. Write And Inspect The Aircraft SD Card

Download only the checksum-pinned image named by the governed image definition.
Prepare the owner-only NoCloud bootstrap input outside Git. Inspect before any
device write:

```bash
iii host image inspect \
  --image <pinned-image.img.xz> \
  --bootstrap-input <owner-only-bootstrap.json> \
  --output=json
```

Resolve the exact removable device through `/dev/disk/by-id/`, unmount its
partitions, and retain the typed pre-write proof. A normal rewrite requires a
verified backup receipt:

```bash
iii host image write \
  --image <pinned-image.img.xz> \
  --bootstrap-input <owner-only-bootstrap.json> \
  --device /dev/disk/by-id/<exact-removable-device> \
  --evidence-directory <owner-only-evidence-directory> \
  --backup-record <verified-backup-receipt> \
  --dry-run --operation-id image-aircraft-initial --output=json
```

`--accept-data-loss` is a separate, visible last resort for genuinely
unrecoverable source media; it is not a convenience substitute for backup. Follow
[host imaging and first boot](host-imaging-and-first-boot.md) for checksum,
device-proof, Ethernet-first discovery, and first-boot diagnostics.

## 3. Converge The Provisioned Aircraft

Use the temporary bootstrap account only with the owner-controlled inventory and
host-provisioning input. Authenticate inputs and inspect predicted convergence:

```bash
iii host provision check \
  --target iii.local \
  --inventory <owner-only-inventory.yml> \
  --inputs <owner-only-provisioning-input.json> \
  --ansible-playbook <pinned-ansible-playbook> \
  --output=json

iii host provision apply \
  --target iii.local \
  --inventory <owner-only-inventory.yml> \
  --inputs <owner-only-provisioning-input.json> \
  --ansible-playbook <pinned-ansible-playbook> \
  --dry-run --operation-id provision-aircraft-initial --output=json
```

Apply only the retained resume command. Successful finalization removes bootstrap
authority and records `commissioned: false`: **provisioned is not commissioned**.
The complete three-run convergence, access enrollment, fixed service/network
boundary, and failure recovery are in [aircraft host provisioning](host-provisioning.md).

### Attended Root Maintenance Shell

The provisioned aircraft has two permanent SSH identities with deliberately
different authority. `iii-deploy@iii.local` is the non-interactive receiver
gateway used by canonical automation. `iii@iii.local` is the separately
keyed attended development and field-research shell with unrestricted
passwordless sudo. It is not used by Ansible, the receiver, or routine CLI
commands.

```bash
ssh -i "$XDG_CONFIG_HOME/iii/credentials/maintenance/ssh_ed25519" \
  -o IdentitiesOnly=yes iii@iii.local
sudo -n id -u
```

The second command must print `0`. Password/root login, TCP/agent/X11 forwarding,
and tunnels remain disabled, and the firewall accepts SSH only from the operator
CIDR. Treat every command in this shell as a potential commissioning-invalidating
host mutation: keep the aircraft physically safe, record changes, and return to
the retained Ansible/commissioning flow before flight. Never point an Ansible
inventory at this account.

### Update The Deployment Receiver

Receiver updates are signed host-control artifacts, not application releases.
Use this workflow only on a provisioned aircraft that is landed, disarmed, not
executing a mission or custom operation, reachable through the permanent
forced-command gateway, and has enough free storage to preserve the configured
reserve. The candidate must use the separately governed `receiver-update` trust
authority and declare compatibility with every retained application,
configuration, journal, audit, bootstrap, CLI, and request format.

From a source checkout, first materialize a new signed candidate from the exact
retained host-provisioning artifact. Inspect before creating any files, then run
only the same command with `--apply` after review:

```bash
deployment/scripts/prepare_receiver_update_artifact.py \
  --output .iii/receiver-update-<generation> \
  --provisioning-artifacts .iii/host-provision \
  --workspace-root . \
  --generation <generation> \
  --version v<major>.<minor>.<patch> \
  --operation-id receiver-update-artifact-<generation>
```

Retain its `iii.receiver-update-artifact/v1` record. It binds the source
provisioning record and receiver identity, signer, exact schema/policy inputs,
new receiver identity/generation, and hashes of all signed bundle files. Use the
generated `bundle/` directory in the following inspection and apply commands.

Verify the exact bundle locally without contacting or mutating the aircraft:

```bash
iii deploy receiver-update inspect .iii/receiver-update-<generation>/bundle \
  --trust <receiver-update-trust-store> --output=json
```

Retain a normal dry-run operation plan before transfer or selector mutation:

```bash
iii deploy receiver-update apply <signed-receiver-bundle> \
  --trust <receiver-update-trust-store> --target real \
  --dry-run --operation-id receiver-update-<generation> --output=json
```

Review the returned mutation, target binding, permissions, and exact retained
operation ID, then execute only its confirmed resume command. Success means the
upload and receiver mutation were durably accepted; it does not mean the new
generation has committed. The stable Ansible-owned bootstrap independently
prepares the inactive slot, switches the selector, starts the candidate, and
requires its Unix socket, self-tests, journal compatibility, identity, generation,
and protocol readiness within 30 monotonic seconds. Client, SSH, or network loss
does not disable that deadline or automatic fallback.

Reattach and inspect both remote state and the retained local actual record:

```bash
iii deploy status --target real \
  --operation receiver-update-<generation> --output=json
```

The retained `receiver-update-actual.json` uses
`iii.receiver-update-actual/v1` and binds the verified receiver identity,
generation, transfer record, exact mutation plan, and durable acceptance. Keep it
with the later terminal receiver journal and physical validation evidence. If the
operation remains interrupted, rerun status with the same operation ID; do not
upload different bytes under that ID. A failed candidate automatically restores
the prior receiver slot before application activation. A failed or unavailable
stable bootstrap, corrupt selector, or loss of all authenticated access is a stop
condition requiring the physical SD recovery/reprovision procedure, followed by
recommissioning. Application rollback does not roll back a successfully committed
compatible receiver generation.

## 4. Commission Before Flight

Commissioning binds the physical target, host report, hardware evidence, exact
qualified release/configuration, enrolled computer, and signed acceptance rows.
Start with authenticated inspection and a non-mutating readiness record:

```bash
iii host inspect --target real --output=json
iii host hardware inspect --target real --output=json
iii field check --target real --output=json
iii verify deployment --require-level physical --require-complete --output=json
```

PX4, mmWave, camera, charger/gripper, runtime API, logs, configuration, power-loss
reconciliation, rollback, offline operation, and recovery must all pass the
versioned commissioning script. Warning acknowledgement retains severity and
cannot waive failures. The exact evidence boundary is in the
[verification matrix](deployment-verification-matrix.md). Do not claim a
field-development bundle or container test as commissioning evidence.

## 5. Prepare And Deploy A Qualified Release

At home with network access, fetch, authenticate, and cache the qualified release
and signed status chain. Then prove the same cache works without network or target
mutation:

```bash
iii field prepare vX.Y.Z --target real \
  --dry-run --operation-id prepare-vX-Y-Z --output=json
iii field verify vX.Y.Z --offline --target real --output=json
iii release verify vX.Y.Z --offline --output=json
```

Run connected readiness immediately before deployment:

```bash
iii system clock sync --target real --profile real \
  --dry-run --operation-id field-clock-real --output=json
iii field check --target real --output=json
iii release deploy vX.Y.Z --destination real \
  --dry-run --operation-id deploy-vX-Y-Z --output=json
```

Qualified deployment uses the signed paired GC/drone set and current
configuration checkpoint. Select GC-only, drone-only, or paired content only when
the release declares that component boundary; select missions through the
manifest-backed include/exclude controls, never by copying files onboard. Release
withdrawal/unsafe status blocks new flight and activation without automatically
switching an aircraft already in operation. See the
[qualified release pipeline](qualified-release-pipeline.md) and
[release bundle format](release-bundle-format.md).

## 6. Perform A Dirty Or Untracked Field Deployment

Field deployment is explicitly non-qualified. It inventories every dirty,
untracked, ignored, and submodule input into a signed field bundle while
preserving the visible qualified anchor. Plan the exact component and mission
selection from the source checkout:

```bash
iii deploy plan \
  --bundle-set <signed-field-bundle-set> \
  --component both \
  --include-mission <mission-id> \
  --target real --output=json

iii deploy field \
  --bundle-set <signed-field-bundle-set> \
  --configuration-checkpoint-id <current-checkpoint-id> \
  --component both \
  --include-mission <mission-id> \
  --activate --target real \
  --dry-run --operation-id field-deploy-iteration --output=json
```

Do not update the dependency lock merely to hide a dirty submodule. A field
overlay cannot produce qualified or commissioning evidence. If activation asks
for a removed/reintroduced configuration decision, inspect the retained review
and continue the same operation explicitly:

```bash
iii deploy continue <review-operation-id> \
  --decision <set>:<parameter>=use_old \
  --target real --dry-run --operation-id <same-operation-id> --output=json
```

## 7. Change, Capture, Compare, And Promote Configuration

The GUI edits one typed server-owned session. Unsaved browser values are local;
**Apply** commits one atomic durable transaction. Constant values become pending
until an explicit cold restart while landed/disarmed. There is no separate GUI
tuning mode.

Capture named aircraft state and compare it without mutating source:

```bash
iii config capture pull --target real \
  --name <capture-name> --description <purpose> \
  --dry-run --operation-id capture-aircraft-config --output=json
iii config capture show <capture-id> --output=json
iii config capture diff <capture-id> --against <baseline-capture-id> --output=json
```

Promotion is a reviewed source change on a normal feature branch:

```bash
iii config promotion plan \
  --capture-id <capture-id> --profile real --release-id <release-id> \
  --key <parameter-key> --classification shared-tracked-default \
  --base develop --output=json
```

PX4 parameters and QGC managed settings have separate typed capture/diff/apply
paths because their owners and safety semantics differ:

```bash
iii px4 params capture --snapshot <px4-snapshot> \
  --name <capture-name> --description <purpose> \
  --dry-run --operation-id capture-px4-params --output=json
iii px4 params plan --profile real --snapshot <px4-snapshot> --output=json
iii qgc config capture --release-id <release-id> \
  --qgc-version <pinned-version> --clean-exit \
  --dry-run --operation-id capture-qgc-settings --output=json
iii qgc config diff --capture-id <capture-id> --output=json
```

The [configuration runbook](configuration-system.md) defines live edit,
reconciliation, named set, reintroduction, PX4, and QGC promotion semantics.

### PX4 Ethernet baseline

The canonical real-aircraft network contract is
`deployment/px4/network-baseline.json`:

- Raspberry Pi built-in `eth0`: `10.41.10.1/24`;
- PX4 Ethernet: `10.41.10.2/24`, static, with no gateway or DNS;
- MAVLink: PX4 `14540/UDP` to Pi `14540/UDP`;
- uXRCE-DDS: PX4 client to Pi agent `8888/UDP`.

The PX4 SD card carries the rendered files at `/fs/microsd/net.cfg` and
`/fs/microsd/etc/extras.txt`. The latter starts both transports explicitly. Do
not set both `MAV_2_CONFIG=1000` and `UXRCE_DDS_CFG=1000`: PX4's automatic
port-configuration mechanism gives one application exclusive ownership, so the
release requires both values to be `0` and owns startup through `extras.txt`.
Render a reviewable, idempotent staging tree with:

```bash
PYTHONPATH=deployment/src python3 \
  deployment/scripts/render_px4_network_baseline.py \
  --output .iii/evidence/px4-network-staging
```

The renderer authenticates the baseline and refuses to overwrite any file whose
contents have drifted.

Applying this baseline is a separate flight-controller maintenance operation,
not a side effect of Pi provisioning or release activation. With the aircraft
landed, disarmed, and propulsion made safe: capture the complete PX4 parameters
and existing SD-card files first; compare their hashes with the release; copy
only the reviewed rendered files; apply the separately planned exact PX4
parameter changes; then reboot the flight controller. Acceptance requires all of
the following—not merely a successful ping: `10.41.10.2` reachability from the
Pi, a fresh MAVLink heartbeat on `14540/UDP`, uXRCE-DDS vehicle messages through
the agent on `8888/UDP`, compatible firmware and exact required parameters, and
fresh fused landed/disarmed telemetry. Restore the captured files and parameter
backup if any post-reboot check fails.

## 8. Operate Offline And Switch Profiles

Before leaving connected infrastructure, prepare every intended qualified release
and verify the GC-only, drone-only, and paired cache paths:

```bash
iii field prepare vX.Y.Z vX.Y.W --target real \
  --dry-run --operation-id prepare-offline-field-set --output=json
iii field verify vX.Y.Z vX.Y.W --offline --target real --output=json
iii field check --target real --output=json
```

The commissioned default is `real`. OptiTrack is a cold, explicitly selected
profile using the same installed release, not a separate artifact installation.
The pre-field rehearsal performs `real -> opti_track -> real`, tags evidence by
profile, and always attempts fail-safe recovery to `real` after an intermediate
failure:

```bash
python3 deployment/scripts/run_pre_field_profile_matrix.py \
  --candidate <exact-candidate.json> \
  --output <owner-only-evidence-directory> \
  --dry-run
```

Simulation remains a devcontainer profile; it skips aircraft clock alignment and
uses native host QGroundControl. Never treat simulation, deferred HIL, or an
uncommissioned OptiTrack setup as physical acceptance.

## 9. Diagnostics, Rollback, And Recovery

First inspect the retained operation and pull evidence; do not manually mutate
the target to “unstick” it:

```bash
iii deploy status --target real --operation <operation-id> --output=json
iii deploy diagnostics pull --destination <owner-only-diagnostics> \
  --target real --dry-run --operation-id pull-deploy-diagnostics --output=json
iii logs pull --destination <owner-only-logs> --target real \
  --dry-run --operation-id pull-runtime-logs --output=json
```

If the receiver reports a resumable interrupted operation, resume only its exact
retained command. If policy calls for rollback, preserve the checkpoint pair:

```bash
iii deploy rollback <qualified-anchor-release-id> \
  --configuration-checkpoint-id <paired-checkpoint-id> \
  --target real --dry-run --operation-id rollback-field-release --output=json
```

Receiver/activation failure, client loss and reattachment, automatic onboard
rollback, low disk, network rollback, degraded clock, unsafe release withdrawal,
host maintenance, and physical SD repair have different authorities. Follow
[host maintenance](host-maintenance.md), [portable backup and restore](portable-host-backup-and-restore.md),
and [the Raspberry Pi boot baseline](raspberry-pi-boot-baseline.md). Prune logs
only with the exact retained pull receipt:

```bash
iii logs prune --pulled <verified-pull-receipt-id> \
  --target real --dry-run --operation-id prune-pulled-logs --output=json
```

## 10. Back Up, Replace, Or Recover Credentials

Create and externally archive portable non-secret state before maintenance,
reimage, or GC replacement:

```bash
iii host backup create --target real \
  --dry-run --operation-id backup-before-maintenance --output=json
iii records archive <external-archive-directory> \
  --dry-run --operation-id archive-field-records --output=json
iii records verify --output=json
```

A surviving authorized computer enrolls fresh credentials generated on the new
computer, proves them in a new session, and only then revokes the old identity:

```bash
iii access enroll prepare --directory <owner-only-new-machine-directory> \
  --label <new-machine-label> --keyring-account <new-machine-label> \
  --dry-run --operation-id prepare-new-machine --output=json
```

Signing-key-only loss uses signer revocation while SSH remains available. Loss of
every SSH authority has no remote bypass: power down, remove the SD card, salvage
read-only if useful, reimage, provision with fresh enrollment, restore verified
non-secret state, and recommission. Never restore private credentials or machine
identity from the archive.

## Evidence, Interruption, And Final Handoff

Retain command JSON, operation ID, candidate/source/release IDs, before/after
selectors, readiness/commissioning records, pulled diagnostics, backup receipts,
and external archive receipts. Re-run `iii field check` after any contract-changing
maintenance. Qualified release evidence also binds the exact documentation
manifest, generated references, and offline operator subset.

The legacy branch/remote-copy/container deployment path is unsupported. Its
last-known history and the signed cutover gate are documented in
[legacy deployment retirement](legacy-deployment-retirement.md). If any normal
workflow still depends on that repository, direct filesystem mutation, password
SSH, onboard builds, or unpinned images, stop: the retirement/cutover gate has not
passed.
