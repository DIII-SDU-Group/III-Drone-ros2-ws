# Host Imaging And First Boot

This runbook creates Raspberry Pi 5 media from the pinned official Ubuntu
Server image and stops at an SSH-reachable, Ansible-ready bootstrap host. It
does not install the III application, change the upstream partition layout, or
claim that onboard state survives a physical reimage.

The source contract pins Canonical's Ubuntu Server 24.04.4 preinstalled ARM64
Raspberry Pi image:

- file: `ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz`
- Canonical SHA-256: `790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37`
- source: `https://cdimage.ubuntu.com/releases/24.04/release/`

The imaging command verifies the compressed file against that source-controlled
identity and hashes the complete decompressed image before offering any target
for selection. An alternate checksum/profile cannot be supplied through the
field CLI.

## Prepare Owner-Only Bootstrap Input

Create an untracked file outside committed paths or under the Git-ignored
`.iii/` directory. It may contain only the first SSH public key, bootstrap
identity, a one-time Ansible credential, and initial network profiles. Never put
a private SSH/signing key, runtime credential, or reusable login password in it.

```json
{
  "schema": "iii.cloud-init-bootstrap-input/v1",
  "hostname": "iii",
  "ssh_public_key": "ssh-ed25519 REPLACE_WITH_PUBLIC_KEY operator@computer",
  "bootstrap_credential": "REPLACE_WITH_RANDOM_ONE_TIME_VALUE_AT_LEAST_32_CHARS",
  "network": {
    "ethernet_dhcp4": true,
    "wifi": [
      {
        "ssid": "REPLACE_WITH_SSID",
        "password": "REPLACE_WITH_WIFI_PASSPHRASE",
        "hidden": false
      }
    ]
  }
}
```

```bash
chmod 0600 .iii/bootstrap-input.json
```

Ethernet DHCP is always rendered as the physical recovery path, including when
Wi-Fi profiles are present. The command rejects non-owner input permissions and
private-key markers. Structured output contains hashes and a boolean indicating
whether network secrets exist, never SSIDs, passphrases, or the one-time value.

## Download, Inspect, And Select Media

Download the exact pinned filename without renaming it, then inspect before any
write:

```bash
mkdir -p .iii/images .iii/imaging-records
chmod 0700 .iii .iii/images .iii/imaging-records
curl --fail --location --output \
  .iii/images/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz \
  https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz

iii host image inspect \
  --image .iii/images/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz \
  --bootstrap-input .iii/bootstrap-input.json
```

Inspection lists stable `/dev/disk/by-id` path, kernel path, model, serial, size,
transport, mount state, system-disk relationship, and rejection reasons. The
writer refuses mounted/in-use devices, the running system disk, unresolved
device-mapper/holder ancestry, read-only or non-removable media, targets without
a stable path, and targets below the pinned 8 GiB minimum. Never choose a
transient `/dev/sdX` name.

Physical reimage destroys `/opt/iii`, `/var/lib/iii`, `/var/log/iii`, and every
other file on the card. Use a fresh verified portable-host backup record when
the source is readable. `--accept-data-loss` is reserved for already
unrecoverable source media and changes the independent typed proof; it is not a
backup shortcut.

Plan and review an exact retained operation before applying it:

```bash
iii host image write \
  --image .iii/images/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz \
  --bootstrap-input .iii/bootstrap-input.json \
  --device /dev/disk/by-id/REPLACE_WITH_INSPECTED_ID \
  --evidence-directory .iii/imaging-records \
  --backup-record .iii/backups/REPLACE_WITH_VERIFIED_RECORD.json \
  --dry-run --operation-id iii-image-REPLACE_ME
```

On hosts whose removable block-device policy does not grant the logged-in
operator raw write and mount authority, invoke both the plan and its generated
resume command through the same `sudo iii ...` entry point. The command accepts
only owner-only input/evidence paths belonging to root or the invoking
`SUDO_UID`, and returns the final record to the evidence-directory owner. Never
grant a general-purpose operator account permanent membership in the `disk`
group merely to avoid this explicit elevation.

Apply only the exact retained plan shown by the CLI, using the generated resume
command. The command always requires an interactive typed phrase containing the
stable device identity. `--confirm`, `--non-interactive`, environment variables,
and automation cannot supply or bypass that physical-device proof.

The writer rechecks image, input, device topology, device fingerprint, and
capacity immediately before opening the block device exclusively. It streams
the upstream image, flushes it, hashes the complete raw readback, mounts only the
upstream FAT boot partition to install the three NoCloud files, reads those files
back, flushes block buffers, and requests device power-off/eject. Only then does
it create a content-addressed `iii.host-image-record/v1` file.

## First Boot And Recovery Boundary

Boot the Pi with Ethernet attached. The bootstrap account has only the first
public key and temporary passwordless Ansible authority; password login and root
login are disabled. A successful first boot writes:

- `/var/lib/iii/bootstrap/cloud-init-status.json`
- `/var/log/iii/bootstrap-cloud-init.log`

For a failure, inspect the local console plus `/var/log/cloud-init.log`,
`/var/log/cloud-init-output.log`, and the III bootstrap log. Retry SSH through
Ethernet before changing network assumptions. There is no default password,
hidden credential, boot-partition key-injection recovery, or receiver bypass. If
cloud-init remains unreachable without authenticated access, preserve the local
imaging record and reimage.

NoCloud Wi-Fi input is temporarily readable to anyone with physical access to
the unencrypted boot media. Keep the card controlled until convergence. The
host convergence workflow must copy required network state to root-only host
configuration, verify the permanent operator key, receiver, and independent
recovery path, remove or overwrite secret-bearing seed/instance data where the
filesystem permits, disable unintended cloud-init reruns, revoke bootstrap-only
authority, and emit a residual-secret inspection report. An interruption resumes
those steps; a failed sanitization cannot produce a provisioned or commissioned
host.

Continue with the authenticated [aircraft host provisioning](host-provisioning.md)
runbook. Do not narrow bootstrap authority by hand; the retained provisioning
transaction does so only after permanent receiver access and recovery proofs pass.

## Transactional Operator Network Changes

After provisioning, do not edit Netplan over SSH. Create a separate owner-only,
Git-ignored input. It contains no bootstrap credential or hostname:

```json
{
  "schema": "iii.operator-network-input/v1",
  "ethernet_dhcp4": true,
  "wifi": [
    {"ssid": "REPLACE_WITH_SSID", "password": "REPLACE_WITH_PASSPHRASE"},
    {"ssid": "REPLACE_WITH_SECOND_SSID", "password": "REPLACE_WITH_PASSPHRASE", "hidden": true}
  ]
}
```

An empty `wifi` list is the supported Ethernet-only profile. Duplicate SSIDs,
disabled Ethernet DHCP, extra fields, non-owner permissions, tracked inputs, and
an onboard access-point configuration are rejected. Plan, review, and apply:

```bash
chmod 0600 .iii/operator-network.json
iii host network apply \
  --input .iii/operator-network.json \
  --target real \
  --dry-run --operation-id iii-network-REPLACE_ME
# Run the exact resume command emitted by the plan.
```

The retained plan and result expose only content hashes, hashed Wi-Fi profile
identities, profile count, declared host paths, and whether connectivity changes.
They never contain SSIDs or passphrases. Apply copies the candidate into a
root-only receiver transaction, preserves the exact prior Netplan file, runs the
fixed privileged apply helper, and arms `iii-network-revert@.timer` for exactly
90 monotonic seconds. The timer and rollback live onboard and do not depend on
the CLI or SSH session remaining alive. A connectivity-changing apply is allowed
only while landed, disarmed, and maintenance-safe; the runtime graph remains
stopped until confirmation or restoration completes.

Reconnect through Ethernet or the candidate Wi-Fi and immediately plan and run
the emitted `iii host network confirm` command. Confirmation is authenticated,
nonce-bound to the original pending operation, stops the timer, and records the
current profile identity durably. Timeout, link loss, receiver restart after the
timeout, or reboot before confirmation restores the prior file and reapplies it.
Every candidate and restored profile retains wildcard Ethernet DHCP as the
physical recovery path. Inspect the durable outcome with:

```bash
iii host network status \
  --network-operation-id iii-network-REPLACE_ME \
  --target real --json
```

Host convergence installs `avahi-daemon`, fixes the host name to `iii`, and
publishes its active interface addresses as `iii.local` on mDNS. It assigns no
fixed address and provisions no onboard AP. Test `getent hosts iii.local` from a
supported operator LAN; if multicast is filtered, use Ethernet and correct the
LAN rather than adding an unmanaged static address.

The upstream Raspberry Pi boot partition and auto-expanded ext4 root filesystem
remain unchanged. This design intentionally adds no data partition, LVM,
encryption, or A/B root filesystem in this sweep.
