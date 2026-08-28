# Aircraft Host Provisioning

This runbook continues from an authenticated, SSH-reachable bootstrap host and
ends at a provisioned-but-not-commissioned III aircraft host. The workspace-owned
Ansible project under `deployment/ansible/` owns the Ubuntu/ROS/hardware baseline,
fixed receiver and recovery substrate, permanent forced-command access, firewall,
time policy, production daemon/API units, selector-aware launcher, and first-boot
authority removal. Application releases do not own or modify those host resources.

Provisioning is a retained three-run transaction:

1. converge the pinned host baseline and signed initial receiver;
2. run the complete convergence playbook in check/diff mode and require zero
   predicted changes;
3. finalize only after receiver, access, network, package, and recovery proofs
   pass, then remove bootstrap authority and secret-bearing cloud-init state.

A failed or interrupted run never implies commissioning. The final onboard
`iii.host-provisioning-report/v1` explicitly records `commissioned: false`.

## Controller Prerequisites

Use the same reviewed workspace and CLI branches throughout planning and apply.
Install the hash-pinned controller dependencies in an isolated environment.
Use the lock matching stock Ubuntu 22.04/Python 3.10 or Ubuntu 24.04/Python
3.12; any other controller Python fails closed:

```bash
python3 -m venv testing/ansible-venv
case "$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')" in
  310|312) controller_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')" ;;
  *) echo "unsupported controller Python" >&2; exit 1 ;;
esac
testing/ansible-venv/bin/pip install \
  --require-hashes \
  --requirement "deployment/ansible/controller-requirements-py${controller_python}.txt"
```

Prepare these controller-side artifacts outside Git with owner-only permissions:

- a separately signed initial receiver bundle;
- a complete offline wheelhouse and `receiver-requirements.txt` with hashes;
- bundle, release-status, and receiver-update public trust stores;
- one public `iii.machine-enrollment/v1` record for the provisioning computer;
- the runtime API secret environment file containing only the solo-operator
  browser password.

Generate the provisioning computer's independent SSH key, encrypted field
signing key, Runtime API token, and public-only enrollment outside the repository:

```bash
iii access enroll prepare \
  --directory "$XDG_CONFIG_HOME/iii/credentials/provisioning" \
  --label provisioning \
  --signer-passphrase-file /owner-only/path/field-signing.passphrase \
  --dry-run --operation-id iii-access-prepare-provisioning
```

Review the retained plan, then execute only the exact confirmed apply command
returned by the CLI. Planning never creates credentials.

The signing passphrase may instead be stored without entering it in shell
history and retrieved from the desktop OS keyring:

```bash
secret-tool store \
  --label='III field signing - provisioning' \
  service iii-field-signing account provisioning
```

Use the same account with `--keyring-account provisioning`; `secret-tool` prompts
for the secret and the CLI never prints it. The generated directory is
owner-only. Only `enrollment.json` is copied into provisioning input; never copy
`ssh_ed25519`, `field-signing-key.pem`, `runtime-api-token`, a passphrase, or an
SSH agent socket.

Materialize the signed initial receiver, complete ARM64 wheelhouse, three trust
stores, public enrollment, runtime secret, input, and strict inventory with the
repository-owned controller builder. Inspection is read-only; `--apply` writes a
new owner-only output tree and refuses to replace an existing tree:

```bash
deployment/scripts/prepare_host_provisioning_artifacts.py \
  --output .iii/host-provision \
  --workspace-root . \
  --enrollment "$XDG_CONFIG_HOME/iii/credentials/provisioning/enrollment.json" \
  --runtime-token "$XDG_CONFIG_HOME/iii/credentials/provisioning/runtime-api-token" \
  --ssh-private-key "$XDG_CONFIG_HOME/iii/credentials/provisioning/ssh_ed25519" \
  --known-hosts .iii/ssh/first-boot-known-hosts \
  --target 192.168.10.42 \
  --operator-cidr 192.168.10.0/24 \
  --python testing/ansible-venv/bin/python \
  --operation-id iii-host-provision-artifacts-REPLACE_ME
# Review the canonical inspection result, then repeat the exact command with --apply.
```

The resulting `artifact-record.json` binds every wheel, signer, receiver,
enrollment, inventory, and input identity without retaining secret values. Keep
that record with the provisioning operation evidence. The receiver bundle and
wheelhouse are recursively content-addressed again in the retained host plan.
Symlinks, special files, empty trees, inputs writable by another identity, and
changed content between plan and apply are rejected.

## Owner-Controlled Input

Create the input under the Git-ignored `.iii/` tree or another owner-controlled,
Git-ignored directory. Relative artifact paths are resolved against this file.

```json
{
  "schema": "iii.host-provisioning-input/v1",
  "target_class": "raspberry-pi-5-noble-arm64",
  "logical_target": "drone",
  "profile": "real",
  "operator_cidr": "192.168.10.0/24",
  "receiver_bundle_source": "artifacts/receiver-bundle",
  "receiver_wheelhouse_source": "artifacts/receiver-wheelhouse",
  "bundle_trust_source": "trust/bundle-signers.json",
  "release_status_trust_source": "trust/release-status-signers.json",
  "receiver_update_trust_source": "trust/receiver-update-signers.json",
  "operator_enrollment_source": "access/provisioning-enrollment.json",
  "runtime_api_secret_source": "secrets/runtime-api.env",
  "offline": false
}
```

```bash
chmod 0700 .iii .iii/host-provision
chmod 0600 .iii/host-provision/inputs.json \
  .iii/host-provision/trust/*.json \
  .iii/host-provision/access/provisioning-enrollment.json \
  .iii/host-provision/secrets/runtime-api.env
```

The inventory is non-secret and may be reviewed separately. Limit it to the
temporary `iii-bootstrap` account and exact target:

```yaml
all:
  children:
    aircraft:
      hosts:
        iii.local:
          ansible_user: iii-bootstrap
          ansible_ssh_private_key_file: /owner-only/path/bootstrap-key
```

## Check And Apply

Authenticate every input and run the complete playbook in check/diff mode first:

```bash
iii host provision check \
  --target iii.local \
  --inventory .iii/host-provision/inventory.yml \
  --inputs .iii/host-provision/inputs.json \
  --ansible-playbook testing/ansible-venv/bin/ansible-playbook \
  --json
```

Check mode is read-only. On a fresh host it predicts convergence, but package
dependency closure may require a real first convergence before every later task
can be predicted. The mutating path always performs its own complete zero-drift
check after that first convergence.

Retain and review the exact apply plan:

```bash
iii host provision apply \
  --target iii.local \
  --inventory .iii/host-provision/inventory.yml \
  --inputs .iii/host-provision/inputs.json \
  --ansible-playbook testing/ansible-venv/bin/ansible-playbook \
  --dry-run \
  --operation-id iii-host-provision-REPLACE_ME \
  --json
```

Use only the resume command returned by the CLI. The retained plan binds the
workspace and CLI branches/SHAs, inventory and input hashes, every artifact file,
the Ansible tree, executable, required checks, declared permissions, and mutation
list. A stale ref or changed byte requires a new plan.

## Governed Baseline And Network Boundary

The Raspberry Pi 5 hardware class is data-driven by
`deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml`. It pins the date-addressed
Ubuntu and ROS snapshots, exact ROS packages, signing key fingerprint/hash,
hardware packages, runtime UID/GID, service set, ports, and filesystem layout.
Package mutation is separate from application deployment, and unattended apt
timers are disabled.

The baseline also binds `deployment/systemd/unit-contract.json`, which hashes the
stable real-profile environment template, fixed daemon/API/target units, and
`/usr/libexec/iii/iii-release-launch`. Release manifests declare the exact required
host-unit contract. Staging remains safe, but activation fails before selector
mutation when the converged host report differs. The launcher also authenticates
its installed bytes and all installed unit bytes before every process start. The
remedy for either mismatch is an explicit retained Ansible host-maintenance
transaction, never a release-bundle unit overwrite.

The host uses UTC and normal chrony synchronization, with stepping disabled after
the receiver clock gate. Correctness-critical state is bound to boot identity and
monotonic time. The runtime API is exposed only on its declared operator-LAN TCP
port; receiver deployment authority remains the forced-command SSH/Unix-socket
path and is never exposed through that API.

Finalization preserves the cloud-init netplan as root-only
`/etc/netplan/90-iii-operator.yaml`, validates it, disables cloud-init reruns,
removes seed/instance/log secrets, validates sudoers, force-removes the bootstrap
account, and verifies that permanent receiver-gateway keys remain with mode
`0600` and the configured runtime UID/GID so OpenSSH can read them after dropping
privileges. It also resolves the fixed gateway through the active signed receiver
slot and verifies that the runtime identity can traverse every directory and
execute the root-write-protected gateway before bootstrap authority is revoked.
Target-equivalent acceptance opens a new permanent forced-command SSH session and
requires the gateway itself to execute after bootstrap removal. There is no
default password or bootstrap-key reinjection recovery.

## Evidence And Recovery

Successful convergence leaves canonical evidence at:

- `/var/lib/iii/deployment/host-package-policy.json`
- `/var/lib/iii/deployment/host-baseline-report.json`
- `/etc/iii/host-unit-contract.json`
- `/var/lib/iii/deployment/host-provisioning-report.json`
- `/run/iii/receiver-readiness.json` while the receiver is active
- the local operation record containing all three authenticated Ansible recaps

The independent stable bootstrap and receiver A/B fallback remain outside
application releases. If finalization fails, do not manually delete bootstrap
state; correct the failing proof and resume the same retained plan only when its
content bindings remain current. If all authenticated SSH access is lost, inspect
backup evidence and physically reimage/restore/recommission; no remote bypass is
provided.

## Add, Prove, And Revoke A Ground-Control Computer

Prepare fresh credentials on the ground-control computer itself. Do not export a
private SSH/signing key or reuse the provisioning computer's Runtime token:

```bash
iii access enroll prepare \
  --directory "$XDG_CONFIG_HOME/iii/credentials/gc-primary" \
  --label gc-primary \
  --keyring-account gc-primary \
  --dry-run --operation-id iii-access-prepare-gc
```

From an already active provisioning computer, authorize only the new public
record with `iii access enroll add --enrollment <gc-enrollment.json>`. Plan each
mutation first and execute only its returned confirmed apply command. Then switch
to the new computer's `III_SSH_IDENTITY_FILE` and run
`iii access enroll prove --enrollment <gc-enrollment.json>`. Proof must arrive in
a new SSH session authenticated by the pending key. Verify both machines with
`iii access list` and authenticate the Runtime API using each computer's own
`runtime-api-token`. Only after the replacement passes SSH, Runtime API, and
field-signer checks may an active computer run
`iii access revoke --machine-id <old-machine-id>`.

The receiver keeps one state machine but reports the authorities independently:

- SSH stores only public keys in forced-command `authorized_keys`;
- the Runtime API stores only SHA-256 token verifiers for active machines;
- field signing stores public Ed25519 verifiers and active/revoked state.

Revocation removes SSH and Runtime access for that machine and revokes its field
signer without rewriting historical signatures. The final usable SSH machine
cannot be revoked. Signing-only key loss does not remove SSH or Runtime access:
use `iii access signer revoke --signer-id <lost-signer-id>` to remove only that
signing authority, then use the remaining SSH authority to enroll and prove fresh
machine credentials. The field signing agent decrypts its key only into
memory, defaults to an 8-hour unlock, refuses TTLs above 24 hours, and signs only
schema-validated manifest, release-status, promotion/qualification evidence, or
field-readiness digests.

For computer replacement, restore only verified P2.T8 non-secret records and
caches. Generate fresh SSH, Runtime, and signing credentials on the replacement;
never restore a machine identity or private credential from an archive. Prove
the replacement before revoking the old machine.

If every authorized SSH private key is unavailable, stop. Passwords, default
credentials, cloud-init/bootstrap resurrection, boot-partition key injection,
receiver bypass, and Runtime-token escalation are unsupported and rejected.
Power the aircraft down, remove the SD card, inspect/salvage it read-only, verify
the latest backup, write a fresh governed image, run retained Ansible provisioning
with a fresh enrollment, restore only verified backup domains, and repeat complete
commissioning before flight. Losing all SSH authority is a physical reimage path;
losing only field signing authority is not.
