# Controlled Aircraft Host Maintenance

This runbook covers the only supported in-place workflow for Ubuntu snapshot,
kernel, ROS Jazzy, system-package, bundle-trust, and release-status-trust
maintenance on the shared `real`/`opti_track` aircraft host. Ordinary qualified
and field deployments cannot run a package manager, rewrite apt sources, replace
host trust, or modify the fixed maintenance playbook.

Major Ubuntu or ROS transitions, substantial unexplained host drift, an invalid
protected recovery anchor, or an unrecoverable maintenance transaction require
SD-card reprovisioning through `iii host image` and `iii host provision`.

## Safety And Authority Preconditions

Before a material change:

1. Land and disarm the aircraft. Ensure there is no mission, custom/direct
   operation, reference owner, or other mutation in progress.
2. Retain a fresh verified `iii.host-backup-receipt/v1` whose
   `target_state_hash` still equals the receiver's authenticated live state.
3. Preserve at least one active enrolled operator computer. SSH and Runtime API
   credentials are rotated separately with `iii access`; host trust maintenance
   never edits them.
4. Confirm that the onboard qualified anchor is staged, immutable, signed,
   classified `qualified` by the current signed status chain, and compatible
   with the installed host baseline.

The receiver plans against the exact package/platform snapshot, policy and
playbook hashes, host contract, access state, trust identity, target state,
operation ID, and a five-minute single-use nonce. Apply consumes the same
retained plan under the target-wide mutation lease. Stale state requires a new
plan.

## Package Maintenance

The committed policy is
`deployment/host-maintenance/host-maintenance-policy.json`. It fixes Ubuntu
24.04 Noble, ROS Jazzy, ARM64, signed Ubuntu/ROS snapshots, the host contract,
the governed package set, and automatic-reboot prohibition.

Inspect a no-change or update plan with the exact backup receipt:

```bash
iii host maintenance check \
  --kind packages \
  --backup-record .iii/backups/<backup>/receipt.json \
  --target real
```

Retain the exact mutation plan, review its permissions and before snapshot, then
resume it explicitly:

```bash
iii host maintenance apply \
  --kind packages \
  --backup-record .iii/backups/<backup>/receipt.json \
  --target real \
  --operation-id aircraft-packages-2026-08 \
  --dry-run

iii host maintenance apply \
  --kind packages \
  --backup-record .iii/backups/<backup>/receipt.json \
  --target real \
  --operation-id aircraft-packages-2026-08 \
  --resume --confirm
```

The receiver stops the runtime only after a fresh maintenance-safe check, then
starts only the root-owned `iii-host-maintenance@<maintenance-id>.service`
oneshot. This separates package-manager privilege and online snapshot access
from the otherwise read-only/private-network receiver sandbox. The unit runs
the fixed local Ansible playbook with transaction-bound extra variables.
Ansible disables unattended apt mutation, converges only the policy package
list through an isolated source directory with the global source list disabled,
forbids autoremove, and commits the exact retained policy. The durable
transaction records complete before/after platform, package, trust, boot and
host-contract snapshots plus the exact changed package names.

For a prepared offline cycle, add `--offline` to both invocations. Planning
simulates the change and uses apt's `--no-download` cache preflight. Any missing
archive fails before the runtime is stopped, apt sources are changed, or Ansible
runs. Prepare and verify the cache online before travelling; offline mode never
silently reaches a repository.

## Reboot And Post-Boot Validation

Maintenance never reboots automatically. A kernel/firmware update or
`/var/run/reboot-required` leaves the transaction at `reboot-required` and keeps
other mutations blocked. Review the retained after report, then plan and apply
the separate reboot:

```bash
iii host maintenance reboot \
  --maintenance-id <maintenance-id> \
  --target real \
  --operation-id aircraft-reboot-2026-08 \
  --dry-run

iii host maintenance reboot \
  --maintenance-id <maintenance-id> \
  --target real \
  --operation-id aircraft-reboot-2026-08 \
  --resume --confirm
```

Connection loss after receiver acceptance is expected. On boot, the receiver
reconciles the durable reboot journal before application autonomy starts. It
authenticates the current signed release-status chain, re-hashes the complete
immutable protected release tree, and checks its release manifest against the
maintained host report. Only then is the transaction completed.

Inspect authenticated state at any time:

```bash
iii host maintenance status --target real --json
```

If validation fails, the status contains the retained failure and recovery
recommendation. Keep the aircraft non-operational, inspect the transaction and
activation diagnostics, restore the protected qualified anchor through the
receiver where compatible, or reimage/reprovision when the host contract cannot
be restored. Do not bypass the receiver or edit release state by hand.

## Independent Trust Rotations

Bundle trust and release-status trust use separate operations. Each requires an
exact state-bound verified backup, preserves the previous store in the receiver
transaction, retains every old signer entry, names each active-to-revoked
transition, and leaves at least one active replacement. A successful trust
change writes `recommission-required` evidence. Re-run the affected signed
commissioning and recovery subset before flight-capable readiness.

Bundle signer rotation uses:

```bash
scripts/release/manage_release_signers.py --json prove \
  --private-key /secure/iii-signing/new-bundle.pem \
  > /tmp/new-bundle-signer-proof.json

iii host maintenance apply \
  --kind bundle-trust \
  --backup-record .iii/backups/<backup>/receipt.json \
  --trust-store <reviewed-bundle-trust.json> \
  --replacement-proof <new-bundle-signer-proof.json> \
  --retire-signer <old-signer-id> \
  --target real --operation-id rotate-bundle-trust-2026-08 --dry-run
```

Resume the identical command with `--resume --confirm` after review.

A compromised release-status signer requires two coordinated public artifacts:

- a reviewed replacement trust store where the compromised entry remains
  visible as `revoked` and pins the exact last trusted global statement through
  `trusted_through: {sequence, statement_id}`; and
- a replacement index signed by an active new release-status signer, containing
  the byte-for-byte same append-only statement chain and resolved statuses.

Apply them atomically through:

```bash
scripts/release/manage_release_signers.py --json prove \
  --private-key /secure/iii-signing/new-release-status.pem \
  > /tmp/new-status-signer-proof.json

iii host maintenance apply \
  --kind release-status-trust \
  --backup-record .iii/backups/<backup>/receipt.json \
  --trust-store <reviewed-release-status-trust.json> \
  --release-status-index <replacement-signed-index.json> \
  --replacement-proof <new-status-signer-proof.json> \
  --retire-signer <compromised-signer-id> \
  --target real --operation-id rotate-status-trust-2026-08 --dry-run
```

The new trust policy accepts signatures by the revoked key only inside the exact
commissioned history prefix. Rewritten, missing, or conflicting prefix
statements fail verification; new index/status signatures must use an active
replacement. Historical statements and installed releases are never deleted,
moved, or rewritten.

Rotate operator-machine SSH, Runtime API, and workstation field-signing
credentials with `iii access enroll ...`, `iii access revoke`, and
`iii access signer revoke`. Those operations have independent retained plans and
cannot be smuggled into host package or release-trust maintenance.

## Retained Evidence

For every operation, retain:

- CLI JSON plan/result and operation ID;
- verified backup receipt and bound target-state hash;
- before/after snapshot and package delta;
- installed policy ID plus fixed playbook and privileged-executor SHA-256 values;
- Ansible result and any failure tail;
- trust-before copy and exact trust/index identities when applicable;
- reboot boot IDs and protected-release validation identity; and
- recommissioning marker plus the succeeding signed commissioning record.

No physical Raspberry Pi result should be claimed from local/container tests.
Physical package, power-cycle, recovery, and recommission evidence belongs to
the signed commissioning/acceptance run.
