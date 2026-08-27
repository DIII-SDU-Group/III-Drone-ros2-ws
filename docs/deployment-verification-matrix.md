# Deployment Verification Matrix

## Purpose And Authority

The versioned matrix at `deployment/verification/matrix.json` is the sole
traceability index from Q1-Q132 decision clauses and backlog acceptance criteria
to execution level, owner command, Q121 impact category, and required evidence.
It is an audit and evidence-verification surface. It does not authorize a build,
deployment, flight, publication, cutover, or repository mutation.

The reviewed inputs are:

- `deployment/verification/clause-baseline.json` and the explicit
  `clause-migrations.json` history;
- `deployment/verification/policy.json`, including the authenticated Q121
  change-impact policy and nine Q131 cutover scenarios;
- the deployment backlog and its focused-owner coverage index; and
- signed local evidence records or CI/JUnit evidence for one exact candidate set.

## Prerequisites And Supported Environments

Run definition audits from a workspace checkout on any supported development or
GC host. Host-independent tests run in CI. Target-equivalent evidence requires a
provisioned Ubuntu/Jazzy target. Physical evidence requires the intended Raspberry
Pi, aircraft hardware, native GC/QGC, a commissioned safe maintenance state, and
an active `workstation-field` signing key outside the repository.

Never mark unavailable hardware, skipped tests, warnings, or partial outcomes as
passing. A signed record is evidence only; it is not a future-operation token.

## Read-Only Audit

```bash
iii verify deployment --root . --audit-only \
  --report .iii/operations/verification/report.json \
  --junit .iii/operations/verification/report.xml
```

The command validates clause stability, coverage owners, acceptance/test
references, matrix and policy identities, Q121 selection categories, and Q131
scenario coverage. `--output=json` returns `iii.command-result/v1` containing an
`iii.deployment-verification-result/v1` payload. Audit-only success is exit 0 and
means definitions match; it deliberately leaves unexecuted rows as `not_run` in
the report and JUnit skips.

Use `--require-level target-equivalent`, `--require-level physical`, or
`--require-complete` only with matching authenticated `--evidence` records.
Missing, skipped, failed, stale-policy, mixed-candidate, unsigned, path-escaping,
or artifact-hash-mismatched evidence is rejected. Warnings remain warnings.

## Candidate And Local Evidence

A clean qualified candidate is materialized once:

```bash
python3 deployment/scripts/materialize_verification_candidate.py \
  --release-id <sha256> --release-version vX.Y.Z \
  --output <external-evidence-root>/candidate-set.json
```

The candidate binds the workspace commit, submodule lock, release, documentation
manifest, and verification policy. The command refuses any dirty or untracked
workspace and never overwrites an existing candidate.

Q121 selects the required local layer. Target-equivalent and physical runners
record explicit row results and hash artifacts under the evidence directory:

```bash
python3 deployment/scripts/run_target_equivalent_acceptance.py \
  --candidate-set <external-evidence-root>/candidate-set.json \
  --started-at 2026-01-01T00:00:00Z \
  --result <row-id>=pass \
  --artifact <row-id>=<external-evidence-root>/target.log \
  --impact-category provisioned-drone-bench-smoke \
  --signing-key <workstation-field-key.pem> \
  --output <external-evidence-root>/target-evidence.json
```

Use `commission_aircraft.py` with `field-flight` or `opti-track` for physical
rows. Non-pass results require `--reason <row-id>=...`. Artifacts must already be
inside the output directory; symlinks and replacement of an existing record are
refused. CI verifies current matrix/policy binding, Ed25519 authority, candidate
identity, row level/category, canonical JSON, and every artifact hash.

The Q74/P5.T0 pre-field profile row has a dedicated fail-safe runner. Inspect its
non-mutating plan first, then apply only in a safe commissioned maintenance state:

```bash
python3 deployment/scripts/run_pre_field_profile_matrix.py \
  --expected-release-id <sha256> \
  --output-dir <external-evidence-root>/pre-field-profile

python3 deployment/scripts/run_pre_field_profile_matrix.py \
  --expected-release-id <same-sha256> \
  --output-dir <external-evidence-root>/pre-field-profile --apply
```

It proves real-profile readiness and release identity, cold-switches the already
deployed release to `opti_track`, captures authenticated status, and returns to
`real` before rechecking readiness. Any intermediate failure triggers the same
stop/real-boot recovery in `finally`, and the run remains failed even when recovery
succeeds. Logs, hashes, state, plan, and report stay visibly retained for later
physical evidence sealing; the script never deploys or reinstalls an artifact.

## Interruption, Recovery, And Next Actions

Definition audits are repeatable and do not mutate source. Report/JUnit writes are
atomic and may be retried to a new path. Evidence records and candidate sets are
immutable: after interruption or an incorrect result, preserve the partial logs,
choose a new output filename, and seal a new record. Never edit or resign an old
record to change its result.

If clause text or numbering changes, add a reviewed digest-to-digest mapping before
updating the baseline and regenerate the matrix with
`deployment/scripts/update_verification_matrix.py`. Policy or candidate drift
requires a new exact candidate and re-execution; evidence is never silently reused
across identities. The structured result's `Next:` command identifies the first
missing level without executing it.
