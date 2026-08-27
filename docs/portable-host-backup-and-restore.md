# Portable Host Backup, Restore, And Salvage

Portable backup is a receiver-owned operation. It is not a recursive copy of an
aircraft filesystem and it never carries credentials, network secrets, machine
identity, release selectors, or receiver transaction state.

## Declared Boundary

`deployment/portable-state-policy.json` is the only portable-state inventory. It
declares configuration/checkpoints, tuning journals and captures, PX4 inventories
and backups, hardware evidence, deployment activation evidence, selected
deployment audits, and selected diagnostics. Every sealed manifest contains a row
for every declaration, including optional domains that were absent. The manifest
also binds the policy identity, state marker, per-file hashes, domain hashes,
release identity, profile, structural exclusions, and every declared invalidating
mutation.

The scanner rejects symbolic links, hard-link archive entries, special files,
secret-shaped paths and JSON keys, private-key markers, and plaintext secret
assignments. `/etc/iii`, home directories, active selectors, receiver access and
network transactions, incoming uploads, and machine identity are outside the
portable schema by construction.

## Normal Backup

Use:

```bash
iii host backup create --target real --dry-run
iii host backup create --target real --operation-id <operation-id> --resume --confirm
iii host backup verify
iii host backup status --target real
```

The retained receiver plan acquires the same mutation lease used by activation,
rollback, networking, and host maintenance. Real and OptiTrack targets must be
maintenance-safe. The receiver stops and flushes declared writers, copies and
hashes the policy domains into private local staging, proves the state marker did
not change, seals and verifies the deterministic archive, and resumes standby.
Only then does the CLI perform the longer chunked transfer. Interrupted local
partials are removed; a repeated identical backup converges on the same content
identity.

Verified external copies live under `.iii/backups/<backup-id>/` with the archive
and a content-identified receipt. `list`, `show`, `verify`, `export`, `import`, and
explicit `prune` operate on this store. Prune re-evaluates all retained JSON
references and refuses restore/audit evidence still cited elsewhere. General
`.iii` record archives remain the independent media-level archive layer; field
readiness warns when no verified external archive receipt is recent within 30
days.

## Reimage And Restore

Image writes and governed host-baseline replacement accept only a fresh,
content-identified, externally verified backup receipt. The separately confirmed
data-loss path remains available only for genuinely unrecoverable media and stays
visible in the destructive imaging record.

After clean imaging, provisioning, credential enrollment, and deployment of a
compatible release:

```bash
iii host backup restore <backup-id> --target real --dry-run
iii host backup restore <backup-id> --target real \
  --operation-id <operation-id> --resume --confirm
```

The CLI resumably uploads the exact archive to the confined incoming root. The
receiver independently verifies it, extracts into a private versioned generation,
runs schema reconciliation, atomically replaces only the portable-state selector,
and validates protected-release/host health. Failure restores the prior selector.
Machine identity, credentials, and receiver transactions are never materialized.

## Powered-Off Removed-Media Salvage

`iii host salvage --device /dev/disk/by-id/<exact-device>` is only for a removed
SD card while the Pi is powered off. The retained plan rejects the running system
disk, mounted/in-use media, ambiguous devices, and anything except the known
single-ext4 Ubuntu Raspberry Pi layout. The worker runs in a private mount
namespace, checks the ext4 journal without repair, mounts with
`ro,noload,nodev,nosuid,noexec`, and always unmounts on success or failure. It
passes recovered files through the normal portable-state scanner and verifier.

A salvage record binds stable/resolved block-device identity, layout and
filesystem evidence, transaction consistency, recoverable domains, omissions,
and hashes. It explicitly states that no credentials were recovered and that a
clean reimage, fresh credentials, and full recommissioning remain mandatory.
Salvage never repairs the card, resets credentials, or produces bootable media.
