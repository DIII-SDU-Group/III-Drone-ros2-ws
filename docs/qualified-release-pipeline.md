# Qualified Release Pipeline

The `Qualified Release` workflow is the only deployable qualified-artifact
producer. It starts from an immutable strict `vMAJOR.MINOR.PATCH` tag whose
commit is reachable from workspace `release`. The tag is an attempt trigger,
not evidence of success. A failed attempt retains `iii-attempt-vMAJOR.MINOR.PATCH`
evidence, exposes no deployable release, and permanently consumes that version.

## Protected configuration

Configure these GitHub Actions environments and repository variables before the
first release. Never place a private key or an exported secret value in Git,
workflow artifacts, job summaries, or pull-request code.

| Boundary | Required configuration | Authority |
|---|---|---|
| `qualified-signing` environment | `III_QUALIFIED_SIGNING_KEY_PEM` secret | Ed25519 `ci-qualified` signer; read-only checkout, no publication permission |
| `release-status` environment | `III_RELEASE_STATUS_SIGNING_KEY_PEM` secret | independent Ed25519 `release-status` signer; serialized status publication only |
| Repository variable | `III_QUALIFIED_SIGNERS_JSON` | public trusted-signer store used to verify qualified bundles |
| Repository variable | `III_RELEASE_STATUS_SIGNERS_JSON` | public trusted-signer store used to verify status statements and indexes |
| Repository variable | `III_PROMOTION_SIGNERS_JSON` | signer-ID to public-key mapping used for promotion attestations |

Both protected environments require maintainer review and restrict deployment
branches/tags to the trusted release path. Pull-request workflows receive none of
the private material. The signing job has `contents: read`; the separate
publisher has `contents: write` but no signing secret.

The workspace rulesets protect `v*`, `iii-attempt-v*`, and `iii-status-*` tags
against deletion and force-update without bypass actors. Reconcile and audit the
declared rules before qualification:

```bash
python scripts/governance/manage_github_rulesets.py
python scripts/governance/manage_github_rulesets.py --apply
python scripts/governance/audit_github_rulesets.py --json
```

Planning is read-only. `--apply` is an explicit GitHub mutation and must use the
retained automation plan and current authenticated references.

## Qualification record

Every release must retain these exact passing checks: dependency lock,
governance audit, promotion evidence, deployment contracts, GC tests, GC build,
ARM64 build, ARM64 target tests, and the exact PX4 firmware build. The PX4 job
normalizes and checks the uXRCE-DDS generator input, verifies the clean pinned
PX4 commit and nested-submodule state, builds `px4_fmu-v6x_multicopter`, validates
the firmware container metadata, and reuses only a cache keyed by the complete
source/specification/toolchain identity. The pipeline packages one pinned ARM64 drone
tree and two pinned x86_64 GC OCI images. It then records source and dependency
identity, check logs and output hashes, build records, manifests, paired artifact
identity, signer identity, run identity, and exact publication bytes.

Publication first creates a draft, uploads only missing assets, compares every
remote byte, and publishes only the exact machine-derived release notes. A rerun
is a no-op when all bytes match and fails closed on changed or extra bytes. The
single clean pinned build is the qualification result; any later reproducibility
comparison is diagnostic and cannot retroactively authorize or reject it.

GitHub-hosted CI never contacts an aircraft. Local retrieval and handoff remain
operator actions:

```bash
iii release list
iii release show v1.2.3
iii release fetch v1.2.3
iii release verify v1.2.3
iii release cache v1.2.3
iii release deploy v1.2.3 --destination /secure/handoff/v1.2.3
```

The separately published PX4 artifact can be converted into reviewable update
media without contacting the flight controller:

```bash
iii px4 release prepare \
  --release-directory /secure/handoff/v1.2.3 \
  --destination /secure/handoff/v1.2.3/px4-media \
  --dry-run --operation-id prepare-px4-v1-2-3 --output=json
iii px4 release prepare \
  --release-directory /secure/handoff/v1.2.3 \
  --destination /secure/handoff/v1.2.3/px4-media \
  --operation-id prepare-px4-v1-2-3 --confirm --output=json
```

The output contains the exact custom firmware, a USB-applicable non-calibration
parameter file, the two USB-written microSD network/startup files, hashes, and
an offline procedure. USB is the canonical PX4 maintenance path; removable-media
copying is retained only for recovery. Preparation performs zero flight-controller
writes.

`fetch` verifies current signed status and refuses withdrawn or unsafe releases.
Explicit offline cache/deploy verifies the complete cached status chain and
reports that no online refresh occurred; it never converts stale state into a
claim of current safety.

## Status transitions

`iii release status set` authenticates the current publication and predecessor,
retains a canonical CLI operation plan, and dispatches only `release-status.yml`
on `release`. The protected workflow rechecks the predecessor immediately before
mutation, signs one monotonic transition, and publishes immutable
`iii-status-N` statement/index assets under a global serialization lock.

```bash
iii release status set v1.2.3 \
  --status withdrawn \
  --reason 'Superseded after field validation' \
  --superseding-version v1.2.4 \
  --confirm
```

Allowed transitions are `qualified -> withdrawn|unsafe` and
`withdrawn -> unsafe`. `unsafe` is terminal. Tags and qualified assets are never
edited to represent status.

## Key rotation and revocation

Generate replacement keys outside the repository with
`scripts/release/manage_release_signers.py`. Add and prove the new public signer
before changing the protected environment secret. Update the corresponding
public trust variable, run contract tests, then revoke the old public signer.
Keep old public identities needed to verify retained artifacts; revocation blocks
new trust but must not make historical audit records unverifiable. Bundle and
status authorities rotate independently and never share private material.

After rotation, qualify a new version or publish a harmless planned status only
after verifying the protected environment uses the expected new signer ID. Do
not test private signing authority on pull requests.
