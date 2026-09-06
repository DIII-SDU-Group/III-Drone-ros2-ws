# Raspberry Pi 5 Boot Baseline And SD Recovery

This runbook defines the source-controlled boot boundary for the shared
Raspberry Pi 5 aircraft host. The profile records what III must inspect and own;
it is not a replacement for Canonical's stock Ubuntu Raspberry Pi firmware
configuration. Provisioning preserves the upstream partition layout,
`config.txt`, command line, firmware defaults, and device-tree overlays unless a
reviewed hardware requirement is added to the profile.

The canonical profile is
`deployment/boot/raspberry-pi-5-noble-arm64.json`. Its content identity binds:

- the Raspberry Pi 5 model and ARM64 architecture;
- active firmware sections (`global`, `all`, and `pi5`);
- the absence of unsupported overclocking and voltage/turbo directives;
- explicitly III-managed settings and overlays, currently empty;
- required and forbidden kernel command-line tokens, with sensitive values
  redacted from inspection output; and
- ownership: Ansible provisions the profile, application releases cannot mutate
  boot, and any setting repair/change is a retained host-maintenance operation.

`deployment/ansible/roles/boot_baseline` installs the exact profile and a small
ownership record under `/etc/iii`. It only reads the stock boot inputs during
ordinary convergence. A missing, linked, unexpectedly writable, wrong-model, or
wrong-architecture input fails the production baseline instead of inventing a
replacement default.

## Inspect Without Mutation

Use the authenticated composite inspection for either shared runtime profile:

```bash
iii host inspect --target real --json
iii host inspect --target opti_track --capture .iii/inspections/pi-host.json --json
```

The receiver reports the running kernel release/version/architecture, Pi model
and revision, effective active firmware directives (including bounded includes),
boot configuration hash/mode, redacted command-line tokens and hash, profile
identity, boot ID, and sorted drift. The same capture also contains the
independent hardware-role inspection. The receiver rejects a composite assembled
across different boots; the CLI verifies the report against its trusted local
boot and hardware profiles and never overwrites an existing capture.

An application release cannot repair boot drift. `/boot`, the installed boot
profile, and the Ansible-owned baseline record are forbidden by both normal
release and receiver self-update policy. The privileged host-maintenance
executor is fixed, root-owned, content-bound into the retained plan, and is the
only supported in-place writer.

## Repair Or Change A Boot Setting

First make the aircraft landed, disarmed, owner-free, and maintenance-safe.
Create a fresh portable host backup whose receipt is bound to the receiver's
current target-state hash. Review the source change to the canonical boot profile
and its new content identity before using it; never construct an undocumented
profile beside the repository.

Plan the exact change:

```bash
iii host maintenance check \
  --kind boot-settings \
  --boot-profile deployment/boot/raspberry-pi-5-noble-arm64.json \
  --backup-record .iii/backups/<backup>/receipt.json \
  --target real

iii host maintenance apply \
  --kind boot-settings \
  --boot-profile deployment/boot/raspberry-pi-5-noble-arm64.json \
  --backup-record .iii/backups/<backup>/receipt.json \
  --target real \
  --operation-id aircraft-boot-<date> \
  --dry-run
```

Review the retained old/new profile identities, each setting/overlay delta,
drift, boot-file hashes and modes, permissions, backup receipt, and operation
ID. Resume only that plan with `--resume --confirm`. Immediately before Ansible,
the receiver rechecks the live snapshot and copies every governed boot file into
the owner-only transaction directory. A failed convergence restores the exact
bytes and modes from those internal copies and retains a failed recovery record.

Successful convergence installs only the III-managed block, refuses unsupported
tuning or other unowned drift, installs the retained profile, records the post-change inspection, marks
commissioning stale, and stops at `reboot-required`. It never reboots implicitly:

```bash
iii host maintenance reboot \
  --maintenance-id <maintenance-id> \
  --target real \
  --operation-id aircraft-boot-reboot-<date> \
  --dry-run
```

Resume the exact reboot plan. After reconnecting, run `iii host maintenance
status --target real --json` and `iii host inspect --target real --json`. The
receiver completes the transaction only after observing a new boot ID, an
accepted exact boot profile, and the protected qualified release. Boot-policy
changes require the affected commissioning and power-cycle acceptance subset.

## Physical SD Repair And Reprovision Rehearsal

There is intentionally no A/B boot or root filesystem. Rehearse this procedure
on a sacrificial SD card before relying on it in the field, and record the card
identity, imaging record, host-provision result, post-boot inspection IDs,
power-cycle result, and restore/commissioning evidence. Never practice against
the workstation system disk or the only current aircraft card.

1. While the source host is readable, create and verify a fresh portable host
   backup. Export the latest commissioning and inspection records. Shut the Pi
   down cleanly and remove power before removing the SD card.
2. Attach the card to the operator computer. Record `lsblk --fs --paths` and run
   read-only diagnosis against explicit unmounted partitions, for example
   `sudo fsck.vfat -n /dev/disk/by-id/<card>-part1` and
   `sudo e2fsck -fn /dev/disk/by-id/<card>-part2`. These commands diagnose; they
   do not authorize repair or prove the card bootable.
3. If the filesystems are readable, mount them read-only to recover evidence and
   compare boot files with the retained maintenance backups. Do not copy an
   unknown `config.txt`, kernel, firmware, credential, or release tree onto the
   card. A narrowly understood filesystem repair may be performed only after a
   separate raw image of the card is retained and the exact device is rechecked;
   otherwise treat the source as unrecoverable.
4. Prefer deterministic reprovisioning for unexplained boot/root corruption.
   Follow [Host Imaging And First Boot](host-imaging-and-first-boot.md): run
   `iii host image inspect`, then plan `iii host image write` against the stable
   `/dev/disk/by-id/...` card identity. The typed physical-device confirmation,
   state-bound backup receipt, exclusive open, full raw readback hash, and
   NoCloud readback remain mandatory. Use `--accept-data-loss` only when the
   source is explicitly declared unrecoverable; it is never a rehearsal shortcut.
5. Boot first with Ethernet attached and run the resumable provisioning workflow:

   ```bash
   iii host provision check \
     --target iii.local \
     --inventory .iii/host-provision/inventory.yml \
     --inputs .iii/host-provision/inputs.json
   iii host provision apply \
     --target iii.local \
     --inventory .iii/host-provision/inventory.yml \
     --inputs .iii/host-provision/inputs.json \
     --operation-id aircraft-reprovision-<date> \
     --dry-run
   ```

   Do not narrow bootstrap access
   manually. Provisioning must prove permanent operator/receiver recovery,
   secret sanitization, the boot profile, and host inspection before reporting
   `provisioned`.
6. Restore only verified non-secret portable records through the governed restore
   workflow. Reinstall a qualified protected anchor, reconnect every shared
   hardware role, run `iii host inspect`, activation/rollback and power-cycle
   acceptance and PX4/QGC compatibility checks. Create a fresh signed commissioning record.
   A reimaged host is never commissioned merely because
   SSH or the application starts.

Abort the rehearsal if device identity changes, a partition is mounted/in use,
the backup or imaging record cannot be verified, Ethernet recovery fails, boot
inspection drifts, or any protected-anchor/commissioning proof is absent. Retain
the evidence and use another known-good card rather than bypassing a gate.
