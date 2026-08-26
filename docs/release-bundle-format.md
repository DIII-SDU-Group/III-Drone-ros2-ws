# Release Bundle Format

III releases contain independently portable `drone/` and `gc/` component
directories. Both components carry the same canonical release record and bind
the same compatibility identity and paired payload identities. Either component
can be inspected, verified, installed, retained, or rolled back without the
other archive being present.

## Fixed component layout

Each component directory contains exactly:

```text
bundle.tar.zst
bundle.manifest.json
release-manifest.json
bundle.sha256
bundle.sig.json
```

Names outside this fixed directory layout are rejected. Names supplied by a
release asset, download, or operator are never used as identity. The signed
canonical documents supply release, component, target, compatibility, payload,
and signer identity.

The archive is deterministic USTAR compressed as one Zstandard frame at level
19 with a frame checksum and no declared content size. Entries are sorted by
UTF-8 path bytes; uid, gid, user/group names, and modification time are
normalized; modes are limited to `0644` and `0755`. The first two entries are:

```text
META/bundle-manifest.json
META/release-manifest.json
```

All remaining entries are signed content-index paths below `payload/`. Links,
device nodes, FIFOs, sockets, PAX extensions, absolute or escaping paths, archive
hooks, extra files, and source/build roots are rejected.

## Limits and atomicity

Packaging and streaming verification enforce both signed actual limits and the
host policy ceiling in `deployment/operational-policy.json`:

- 20 GiB unpacked bytes
- 200,000 entries
- 255 UTF-8 bytes per archive path
- depth 32

Detached identity, schema, authority, checksum, declared-limit, and trust checks
complete before an extraction staging directory is created. Files are streamed
through size and SHA-256 checks. Only a complete verified staging directory is
renamed to the requested destination. Failure removes staging and never replaces
an existing destination.

## Signer lifecycle

Never put private signer material in this workspace, a release, a record archive,
or an operator handoff. The signer tool refuses generation below the workspace
root. The public descriptor and proof-of-possession are safe to transfer.

Example qualified signer creation using paths outside Git:

```bash
scripts/release/manage_release_signers.py --json generate \
  --authority ci-qualified \
  --private-key /secure/iii-signing/qualified.pem \
  --public-descriptor /tmp/qualified.public.json

scripts/release/manage_release_signers.py --json prove \
  --private-key /secure/iii-signing/qualified.pem \
  > /tmp/qualified.proof.json

scripts/release/manage_release_signers.py add \
  --store /etc/iii-deployment/trusted-signers.json \
  --public-descriptor /tmp/qualified.public.json \
  --proof /tmp/qualified.proof.json
```

For rotation, add and prove the replacement first, list the store, then revoke
the old public identity. Revocation refuses to remove the final active signer for
an authority:

```bash
scripts/release/manage_release_signers.py list \
  --store /etc/iii-deployment/trusted-signers.json

scripts/release/manage_release_signers.py revoke \
  --store /etc/iii-deployment/trusted-signers.json \
  --signer-id OLD_SIGNER_SHA256
```

Qualified releases require `ci-qualified` authority. Field-development releases
require `workstation-field` authority. Unknown, revoked, mismatched-authority, or
invalid signatures fail closed.

## Package, inspect, verify, and extract

The input roots must already be component-complete installed payloads. The drone
root contains the native ARM64 install tree, runtime assets, and migrations. The
GC root contains its pinned image/application/CLI payload and host metadata.

```bash
scripts/release/package_release_bundles.py \
  --release-manifest /records/release-manifest.json \
  --component drone=/artifacts/drone-install \
  --component gc=/artifacts/gc-install \
  --private-key /secure/iii-signing/release.pem \
  --output /artifacts/RELEASE_ID.iii-release-v1
```

Inspection verifies signed sidecars and reports compressed size, content, and
limits without opening the archive. Full verification streams every archive
entry without materializing it. Extraction uses the same verification plus the
atomic staging transaction:

```bash
scripts/release/verify_release_bundle.py inspect \
  --bundle /artifacts/RELEASE_ID.iii-release-v1/drone \
  --trusted-signers /etc/iii-deployment/trusted-signers.json --json

scripts/release/verify_release_bundle.py verify \
  --bundle /artifacts/RELEASE_ID.iii-release-v1/drone \
  --trusted-signers /etc/iii-deployment/trusted-signers.json

scripts/release/verify_release_bundle.py extract \
  --bundle /artifacts/RELEASE_ID.iii-release-v1/drone \
  --trusted-signers /etc/iii-deployment/trusted-signers.json \
  --destination /var/lib/iii-deployment/releases/RELEASE_ID/drone
```

Generated bundle sets and signer private keys are operational artifacts and must
remain outside Git.
