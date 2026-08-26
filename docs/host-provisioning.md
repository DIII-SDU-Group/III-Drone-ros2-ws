# Aircraft Host Provisioning

This runbook continues from an authenticated, SSH-reachable bootstrap host and
ends at a provisioned-but-not-commissioned III aircraft host. The workspace-owned
Ansible project under `deployment/ansible/` owns the Ubuntu/ROS/hardware baseline,
fixed receiver and recovery substrate, permanent forced-command access, firewall,
time policy, and first-boot authority removal. Application releases do not own or
modify those host resources.

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
Install the hash-pinned controller dependencies in an isolated environment:

```bash
python3 -m venv testing/ansible-venv
testing/ansible-venv/bin/pip install \
  --require-hashes \
  --requirement deployment/ansible/controller-requirements.txt
```

Prepare these controller-side artifacts outside Git with owner-only permissions:

- a separately signed initial receiver bundle;
- a complete offline wheelhouse and `receiver-requirements.txt` with hashes;
- bundle, release-status, and receiver-update public trust stores;
- one or more canonical OpenSSH Ed25519 public keys for permanent operators;
- the runtime API secret environment file.

The receiver bundle and wheelhouse are recursively content-addressed in the plan.
Symlinks, special files, empty trees, input files writable by another identity,
and changed content between plan and apply are rejected.

## Owner-Controlled Input

Create the input under the Git-ignored `.iii/` tree or another owner-controlled,
Git-ignored directory. Relative artifact paths are resolved against this file.

```json
{
  "schema": "iii.host-provisioning-input/v1",
  "target_class": "raspberry-pi-5-noble-arm64",
  "logical_target": "iii",
  "profile": "real",
  "operator_cidr": "192.168.10.0/24",
  "receiver_bundle_source": "artifacts/receiver-bundle",
  "receiver_wheelhouse_source": "artifacts/receiver-wheelhouse",
  "bundle_trust_source": "trust/bundle-signers.json",
  "release_status_trust_source": "trust/release-status-signers.json",
  "receiver_update_trust_source": "trust/receiver-update-signers.json",
  "operator_public_keys_source": "access/operator-keys",
  "runtime_api_secret_source": "secrets/runtime-api.env",
  "offline": false
}
```

```bash
chmod 0700 .iii .iii/host-provision
chmod 0600 .iii/host-provision/inputs.json \
  .iii/host-provision/trust/*.json \
  .iii/host-provision/access/operator-keys \
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

The host uses UTC and normal chrony synchronization, with stepping disabled after
the receiver clock gate. Correctness-critical state is bound to boot identity and
monotonic time. The runtime API is exposed only on its declared operator-LAN TCP
port; receiver deployment authority remains the forced-command SSH/Unix-socket
path and is never exposed through that API.

Finalization preserves the cloud-init netplan as root-only
`/etc/netplan/90-iii-operator.yaml`, validates it, disables cloud-init reruns,
removes seed/instance/log secrets, validates sudoers, force-removes the bootstrap
account, and verifies that permanent receiver-gateway keys remain. There is no
default password or bootstrap-key reinjection recovery.

## Evidence And Recovery

Successful convergence leaves canonical evidence at:

- `/var/lib/iii/deployment/host-package-policy.json`
- `/var/lib/iii/deployment/host-baseline-report.json`
- `/var/lib/iii/deployment/host-provisioning-report.json`
- `/run/iii/receiver-readiness.json` while the receiver is active
- the local operation record containing all three authenticated Ansible recaps

The independent stable bootstrap and receiver A/B fallback remain outside
application releases. If finalization fails, do not manually delete bootstrap
state; correct the failing proof and resume the same retained plan only when its
content bindings remain current. If all authenticated SSH access is lost, inspect
backup evidence and physically reimage/restore/recommission; no remote bypass is
provided.
