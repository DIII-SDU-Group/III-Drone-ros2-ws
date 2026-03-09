# Dependency Governance

This workspace uses a lock-file based governance model for git submodule dependencies.

## Why

Submodule refs can drift silently and make builds/deployments non-reproducible.
The lock file ensures everyone uses the same dependency commits unless a change is intentional and reviewed.

## Files

- Lock file: `deps/submodule-lock.txt`
- Verify script: `scripts/verify_submodule_lock.sh`
- Update script: `scripts/update_submodule_lock.sh`
- Local III branch policy script: `scripts/iii_branch_guard.sh`
- CI III branch policy script: `scripts/verify_iii_submodule_branch_policy_ci.sh`
- CI workflow: `.github/workflows/dependency-governance.yml`

## Team Workflow

### 1. Normal feature work

Do not edit `deps/submodule-lock.txt` unless intentionally updating dependency versions.

### 2. Intentionally bumping dependencies

1. Update submodule refs as needed.
2. Regenerate lock file:
   ```bash
   ./scripts/update_submodule_lock.sh
   ```
3. Verify:
   ```bash
   ./scripts/verify_submodule_lock.sh
   ```
4. In PR description, explain:
- which submodules changed
- why the bump is needed
- risk/compatibility notes

### 3. CI behavior

PR/push CI runs `verify_submodule_lock.sh`.
If actual submodule commits differ from `deps/submodule-lock.txt`, CI fails.

For pull requests, CI also runs `verify_iii_submodule_branch_policy_ci.sh`, which enforces:
- only III submodules (`src/III-*`, `tools/III-*`) are checked
- each pinned III submodule commit must be reachable from the allowed branch stack:
  `base -> ... -> feature` (for PR: `${base_ref} -> ${head_ref}`)

## Suggested Policy

1. Only bump submodule refs via dedicated PRs (or clearly isolated commits).
2. Require passing dependency governance check before merge.
3. Deploy robots from tags on stable branches so lock state is immutable.

## Useful Commands

Current submodule refs:
```bash
git submodule status --recursive
```

Check lock integrity locally:
```bash
./scripts/verify_submodule_lock.sh
```

Refresh lock after intentional changes:
```bash
./scripts/update_submodule_lock.sh
```

Audit local III branch policy before pushing:
```bash
./scripts/iii_branch_guard.sh audit --base develop
```

Align changed III submodules to feature branch (dry-run, then apply):
```bash
./scripts/iii_branch_guard.sh align --base develop --feature version-migration
./scripts/iii_branch_guard.sh align --base develop --feature version-migration --yes
```
