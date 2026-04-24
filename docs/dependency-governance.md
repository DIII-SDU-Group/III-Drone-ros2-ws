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
- Stacked PR post-merge pointer refresh: `scripts/git/refresh_workspace_submodule_pointers.sh`
- Post-PR local sync helper: `scripts/git/post_pr_sync.sh`
- CI workflow: `.github/workflows/dependency-governance.yml`
- Manual pointer refresh workflow: `.github/workflows/refresh-submodule-pointers.yml`

## Team Workflow

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

For pull requests targeting protected integration branches (`develop`, `main`, `staging`), CI additionally runs `verify_iii_submodule_commits_on_branch_ci.sh`, which enforces:
- each pinned III submodule commit in the workspace PR must exactly match `origin/<base>` HEAD in that submodule repo
- merge is blocked if any pinned III commit does not match the latest target-branch head
- a PR status comment bot updates a table in the workspace PR with per-submodule pass/fail

For those same protected-branch PRs, CI also verifies that every linked III submodule PR listed in the workspace PR body is already merged into the same target branch.

## Stacked PR Automation

Use the workspace helper to create/update a coordinated PR stack:

```bash
./scripts/git/create_stack_prs.sh --base develop --feature <feature-branch>
./scripts/git/create_stack_prs.sh --base develop --feature <feature-branch> --yes
./scripts/git/create_stack_prs.sh --base main --feature <release-branch> --all-iii --yes
```

What it does:
- detects changed III submodules
- pushes each changed III submodule feature branch
- creates/updates submodule PRs (`<feature> -> <base>`)
- creates/updates workspace PR (`<feature> -> <base>`) with linked submodule PRs

Notes:
- `--yes` is required to actually push and create/edit PRs
- `--all-iii` targets all III submodules instead of only changed ones
- without `--yes`, it is a dry-run
- requires authenticated `gh` CLI
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
./scripts/git/push_stack.sh --base main --feature <release-branch> --all-iii --yes
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

### Develop to main release flow

Use the dedicated release wrapper when promoting `develop` into `main`:

```bash
./scripts/git/create_develop_to_main_prs.sh --release release/develop-to-main-2026-03
./scripts/git/create_develop_to_main_prs.sh --release release/develop-to-main-2026-03 --yes
```

What it does:
- syncs the workspace and all III submodules back onto `develop`
- creates or switches a dedicated release branch in the workspace
- aligns all III submodules onto matching release branches
- creates or updates coordinated III submodule PRs and the workspace PR into `main`

After the submodule PRs merge into `main`, refresh the workspace release branch:

```bash
./scripts/git/refresh_workspace_submodule_pointers.sh --base main --feature release/develop-to-main-2026-03 --all-iii --yes
```

Then commit and push the refreshed gitlinks so the workspace PR can satisfy the target-branch gate.

GitHub-native alternative (no local update needed):
1. Open workspace repo Actions tab.
2. Run workflow `Refresh Submodule Pointers`.
3. Set:
- `pr_branch`: your workspace PR branch (for example `version-migration`)
- `base_branch`: usually `develop`
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
