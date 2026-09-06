# Dependency Governance

This workspace uses a lock-file based governance model for git submodule dependencies.

## Why

Submodule refs can drift silently and make builds/deployments non-reproducible.
The lock file ensures everyone uses the same dependency commits unless a change is intentional and reviewed.

## Files

- Lock file: `deps/submodule-lock.txt`
- Verify script: `scripts/git/verify_submodule_lock.sh`
- Update script: `scripts/git/update_submodule_lock.sh`
- Local III branch policy script: `scripts/git/iii_branch_guard.sh`
- CI III branch policy script: `scripts/ci/verify_iii_submodule_branch_policy_ci.sh`
- CI III develop-gate script: `scripts/ci/verify_iii_submodule_commits_on_branch_ci.sh`
- Stacked PR helper: `scripts/git/create_stack_prs.sh`
- Develop-to-main release helper: `scripts/git/create_develop_to_main_prs.sh`
- Main-to-release helper: `scripts/git/create_main_to_release_pr.sh`
- Qualified-tag publisher: `scripts/release/publish_qualified_tag.py`
- Read-only live governance audit: `scripts/governance/audit_github_rulesets.py`
- Stacked PR post-merge pointer refresh: `scripts/git/refresh_workspace_submodule_pointers.sh`
- Post-PR local sync helper: `scripts/git/post_pr_sync.sh`
- CI workflow: `.github/workflows/dependency-governance.yml`
- Manual pointer refresh workflow: `.github/workflows/refresh-submodule-pointers.yml`

## Team Workflow

### Canonical branch matrix

This matrix is policy, not a suggestion:

| Repository class | Source | PR base | Result |
|---|---|---|---|
| Workspace and changed editable III repos | normal feature/work-sweep branch | `develop` | reviewed integration head |
| Workspace and all editable III repos | `promote/develop-to-main/<operation-id>` created from `develop` | `main` | reviewed stable heads |
| Workspace only | `main` | `release` | exact qualified-release candidate |
| Workspace only | exact clean `release` commit | immutable `vX.Y.Z` | qualified publication trigger |

Every other protected-branch source/base pair is rejected. Editable submodules
stop at `main`; they never receive `release` branches or tags coordinated by the
workspace release flow. A normal feature name has no reserved prefix, but must be
the same branch in every affected editable repository. Direct protected-branch
pushes, release-only implementation commits, moving/reusing a qualified tag, and
floating deployment branches are unsupported.

### 1. Normal feature work

Do not edit `deps/submodule-lock.txt` unless intentionally updating dependency versions.

### 2. Intentionally bumping dependencies

1. Update submodule refs as needed.
2. Regenerate lock file:
   ```bash
   ./scripts/git/update_submodule_lock.sh
   ```
3. Verify:
   ```bash
   ./scripts/git/verify_submodule_lock.sh
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

For pull requests targeting protected integration branches (`develop`, `main`, `release`), CI additionally runs `verify_iii_submodule_commits_on_branch_ci.sh`, which enforces:
- each pinned III submodule commit in the workspace PR must exactly match `origin/<base>` HEAD in that submodule repo
- merge is blocked if any pinned III commit does not match the latest target-branch head
- a PR status comment bot updates a table in the workspace PR with per-submodule pass/fail

For those same protected-branch PRs, CI also verifies that every linked III submodule PR listed in the workspace PR body is already merged into the same target branch.

## Stacked PR Automation

Use the workspace helper to create/update a coordinated PR stack:

```bash
./scripts/git/create_stack_prs.sh --base develop --feature <feature-branch> \
  --operation-id <stable-operation-id>
./scripts/git/create_stack_prs.sh --base develop --feature <feature-branch> \
  --operation-id <stable-operation-id> --yes
./scripts/git/create_stack_prs.sh --base main --feature promote/develop-to-main/<id> --all-iii --yes
```

What it does:
- detects changed III submodules
- pushes each changed III submodule feature branch
- creates/updates submodule PRs (`<feature> -> <base>`)
- creates/updates workspace PR (`<feature> -> <base>`) with linked submodule PRs
- compares every local feature SHA with the authenticated remote feature SHA and
  pushes the exact local head when the remote is stale

Notes:
- `--yes` is required to actually push and create/edit PRs
- `--all-iii` targets all III submodules instead of only changed ones
- without `--yes`, it is a dry-run
- requires authenticated `gh` CLI
- dry-run output and PR markers are untrusted transport; Git/GitHub refs,
  rulesets, required checks, and schema/content/signature verification are the
  authority
- after submodule PRs are merged, refresh pointers to capture merge commits:
  ```bash
  ./scripts/git/refresh_workspace_submodule_pointers.sh --base develop --feature <feature-branch> --yes
  ```
  then commit + push workspace branch to update workspace PR gitlinks and lock file

### Push-Only Stack Automation

Use the push-only helper when you want the coordinated III feature branches
published to origin without creating any PRs yet:

```bash
./scripts/git/push_stack.sh --base develop --feature <feature-branch>
./scripts/git/push_stack.sh --base develop --feature <feature-branch> --yes
./scripts/git/push_stack.sh --base main --feature promote/develop-to-main/<id> --all-iii --yes
```

What it does:
- detects changed III submodules
- pushes each eligible III submodule feature branch
- pushes the workspace feature branch

Notes:
- `--yes` is required to actually push
- `--all-iii` targets all III submodules instead of only changed ones
- without `--yes`, it is a dry-run
- unlike `create_stack_prs.sh`, it never calls `gh` and never creates or edits PRs
- dirty worktrees are skipped because only committed branch state can be pushed

### Develop to main promotion flow

Use the verified promotion wrapper when promoting `develop` into `main`:

```bash
./scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08
./scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08 --yes
```

What it does:
- syncs the workspace and all III submodules back onto `develop`
- creates or switches `promote/develop-to-main/2026-08`
- aligns all editable III submodules onto matching promotion branches
- creates or updates linked submodule PRs and the workspace PR into `main`
- leaves third-party repositories untouched

After the submodule PRs merge into `main`, refresh the workspace promotion branch:

```bash
./scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08 --phase refresh --yes
```

The refresh phase pins exact merged `main` heads, updates and verifies the lock,
checks that the promotion branch differs from `develop` only by governed
gitlinks/lock, and idempotently commits and pushes when needed.

### Main to release flow

Only the workspace has a `release` branch. Create or update the direct protected
`main` -> `release` PR with:

```bash
./scripts/git/create_main_to_release_pr.sh
./scripts/git/create_main_to_release_pr.sh --yes
```

The helper fixes both source and target names, cannot carry release-only
implementation changes, and never creates submodule `release` branches.

### Qualified tag publication

After the workspace-only `main -> release` PR merges, create a retained
`iii.qualification-evidence/v1` document bound to the exact release commit,
version, dependency-lock hash, governance audit, and required passing checks.
Publication is read-only unless `--apply` is explicit:

```bash
python scripts/release/publish_qualified_tag.py \
  --version v1.2.3 \
  --evidence .iii/evidence/v1.2.3-preflight.json \
  --operation-id publish-v1-2-3
python scripts/release/publish_qualified_tag.py \
  --version v1.2.3 \
  --evidence .iii/evidence/v1.2.3-preflight.json \
  --operation-id publish-v1-2-3 \
  --apply
```

Apply is refused unless `HEAD` is the exact clean `origin/release` head,
recursive submodule worktrees and the dependency lock verify, all evidence is
complete and identity-bound, and the version is unused locally and remotely.
Release preparation also runs the live governance audit and embeds its audit
identity and full compact result in the publication plan; drift fails closed.
The pushed tag triggers qualified CI; local tooling never produces a qualified
artifact. Active GitHub tag protection blocks ordinary deletion or movement of
all `v*` refs. A failed version is investigated and never silently reused.

### Live governance audit and drift remediation

The audit is strictly read-only and covers the workspace plus all ten editable
III repositories:

```bash
python scripts/governance/audit_github_rulesets.py
python scripts/governance/audit_github_rulesets.py --json > .iii/evidence/governance-audit.json
```

It verifies required branches, exact active rulesets, target-specific promotion
source checks, all other required checks, qualified-tag immutability, and zero
unexpected bypass actors. Exit `0` means exact policy match, `20` means policy
drift, and `30` means the audit could not obtain trustworthy live state. The
JSON form is `iii.github-governance-audit/v1`, includes a content identity, and
is suitable for qualified-release evidence.

For declared-policy drift, review the plan and reconcile explicitly:

```bash
python scripts/governance/manage_github_rulesets.py --json
python scripts/governance/manage_github_rulesets.py --apply --json
python scripts/governance/audit_github_rulesets.py --json
```

Do not auto-delete an unexpected live ruleset or recreate a missing protected
branch. Review those findings, restore or retire the object through an approved
change, then rerun reconciliation and the audit.

GitHub-native alternative (no local update needed):
1. Open workspace repo Actions tab.
2. Run workflow `Refresh Submodule Pointers`.
3. Set:
- `pr_branch`: your workspace PR branch (for example `version-migration`)
- `base_branch`: `develop` for feature stacks or `main` for a verified promotion
- a `main` refresh requires `pr_branch=promote/develop-to-main/<id>`
- `all_iii`: optional, `true` to refresh all III submodules
4. Workflow commits/pushes updated gitlinks + lock file back to the PR branch and comments status on the PR.

## Post-PR Local Sync

After PRs are merged, you can safely sync workspace + III submodules back to `develop`:

```bash
./scripts/git/post_pr_sync.sh --base develop
./scripts/git/post_pr_sync.sh --base develop --yes
./scripts/git/post_pr_sync.sh --base develop --clean-only --yes
```

Behavior:
- fails if workspace or any III submodule has uncommitted changes
- fetches/prunes workspace + III submodules
- switches workspace to `develop` and fast-forwards
- syncs submodules, then switches each III submodule to `develop` and fast-forwards
- deletes local branches when no matching `origin/<branch>` exists and the branch has no commits beyond `develop`
- with `--clean-only`, dirty workspace/submodules are skipped instead of failing

## Suggested Policy

The solo-maintainer policy still requires pull requests, all required checks,
resolved conversations, immutable history, and evidence-bearing promotion. It
sets required approvals to zero because a second person is not assumed; it does
not authorize self-fabricated evidence, check bypass, force push, direct protected
push, or mutation from a read-only plan.

1. Change gitlinks only for reviewed submodule commits and update the lock in the
   same workspace change.
2. Require the exact target's dependency, source-pair, linked-PR, and package
   checks before merge.
3. Use Q118 signed local evidence only for its exact source/policy identity.
4. Select Q121 evidence categories from the changed paths; Q122 may waive only
   declared physical categories with explicit scope/reason/expiry. Never waive
   governance, build, static, integrity, signing, or deployment safety.
5. Reuse Q120 evidence only while source, toolchain, policy, environment, and
   declared validity window are identical.
6. Apply SemVer: MAJOR for intentional contract breaks, MINOR for compatible
   capabilities/profiles/missions, PATCH for compatible fixes/defaults/docs.
7. Publish only protected, signed, immutable tags and append-only signed status
   statements. Withdrawal/unsafe status never rewrites a release.

## Useful Commands

Current submodule refs:
```bash
git submodule status --recursive
```

Check lock integrity locally:
```bash
./scripts/git/verify_submodule_lock.sh
```

Refresh lock after intentional changes:
```bash
./scripts/git/update_submodule_lock.sh
```

Audit local III branch policy before pushing:
```bash
./scripts/git/iii_branch_guard.sh audit --base develop
```

Align changed III submodules to feature branch (dry-run, then apply):
```bash
./scripts/git/iii_branch_guard.sh align --base develop --feature version-migration
./scripts/git/iii_branch_guard.sh align --base develop --feature version-migration --yes
```
