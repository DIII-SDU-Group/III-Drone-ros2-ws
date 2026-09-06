# Local Record Registry And Portable Archives

The III CLI keeps operator-owned, non-secret local state in one schema-versioned
registry. The registry is never a Git repository, a container filesystem, or a
substitute for an external backup. Its root is selected in this order:

1. `III_REGISTRY_ROOT` when explicitly configured;
2. `$WORKSPACE_DIR/.iii` for a sourced workspace profile;
3. `$XDG_STATE_HOME/iii`, or `~/.local/state/iii` when XDG state is unset.

Any registry inside a Git worktree must be below its ignored `.iii/` directory.
The CLI refuses symbolic-link roots, unsafe parents, special files, and roots
owned by another user. Release caches now default to
`<registry>/cache/releases`; operations, verified log pulls, readiness records,
release evidence, and signed status indexes use their corresponding registry
domains. Shared payloads are deduplicated under `blobs/sha256/`.

## Inventory And Integrity

```bash
iii records inventory --json
iii records verify --json
```

`inventory` derives record descriptors from current content. Every descriptor
binds its domain, safe relative locator, file and directory topology, SHA-256
content identities, original creation source, logical target/profile when
present, cross-record references, retention protections, and integrity state.
`verify` compares that derived state with the atomic index and reports corrupt
metadata, blobs, or incomplete staging. The index is derived acceleration state,
not authority; verified archive/import operations rebuild it atomically, and
`inventory` remains available if it is missing or stale.

## Full And Incremental Archives

Choose the destination yourself. It must be outside the registry; the CLI does
not select a cloud service or storage device.

```bash
iii records archive /media/operator/iii-records-full.tar --dry-run --json
iii records archive /media/operator/iii-records-delta.tar \
  --base /media/operator/iii-records-full.tar --dry-run --json
```

The retained preflight reports selected domains, record and blob counts, logical
content bytes, exact projected USTAR bytes, available capacity, omitted or
corrupt inputs, destination state, and full versus incremental mode before any
write. Apply only the exact next action emitted by the dry run. A changed source,
base archive, destination, or capacity requires replanning.

Archives contain a canonical manifest followed by sorted content-addressed
blobs. Header ownership, modes, timestamps, ordering, and padding are fixed, so
identical inputs produce identical bytes. The CLI fsyncs, hashes, reopens, and
fully verifies the archive before retaining its receipt. Reusing an identical
destination is idempotent; conflicting content is never overwritten.

An incremental archive depends on its declared base chain. Keep the full archive
and every required delta together and import them in order:

```bash
iii records import /media/operator/iii-records-full.tar --dry-run --json
iii records import /media/operator/iii-records-delta.tar --dry-run --json
iii records verify --json
```

Import validates the complete tar structure, canonical manifest, checksums,
secret policy, topology, and local conflicts before materializing anything.
Writes use a registry-wide lock, content-addressed staging, fsync, atomic rename,
and deterministic partial-file recovery. Matching local content is idempotent;
different content at any destination is a hard conflict. Import also rebuilds a
missing derived index, enabling recovery on a replacement workstation or GC.

## Secret Boundary

Archives fail closed if any selected schema or file attempts to include SSH or
signing private keys, passwords, API/runtime tokens, bearer credentials, Wi-Fi
secrets, machine identity, credential-bearing CLI arguments, secret assignment
files, or paths that name protected secret stores. Redacted placeholders are
allowed. An archive failure is a request to correct the owning record producer,
not permission to weaken the scanner or manually edit the archive.

## Explicit Retention And Field Readiness

There is no automatic record or blob pruning. To request deletion, first select
exact record identities from `iii records inventory`:

```bash
iii records prune --record <record-sha256> --dry-run --json
```

The preflight separates eligible records from protected records and explains
references, retained-release, restore, commissioning, promotion, unresolved
review, failure acknowledgement, and external-archive protections. Apply
re-verifies both registry state and the external archive bytes. Shared blobs are
not garbage-collected by this operation.

`iii field check --json` reports the most recent internally valid archive receipt,
coverage of current irreplaceable records, age, and whether its recorded path is
currently available. Missing or older-than-policy coverage is a warning, not an
authorization failure and not a requirement for ordinary development operation.
Deletion uses the stricter live archive verification regardless of that warning.
