#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  post_pr_sync.sh - safe post-PR sync for workspace + III submodules

SYNOPSIS
  scripts/post_pr_sync.sh [--base <branch>] [--yes]
  scripts/post_pr_sync.sh -h | --help

DESCRIPTION
  After PRs are merged, this script helps bring your local tree back to a clean
  base branch state (default: develop), and syncs III submodules.

  It performs:
  1) Safety checks: fails if workspace or any III submodule has uncommitted changes.
  2) fetch --prune for workspace and III submodules.
  3) Switch workspace to base branch and fast-forward pull from origin.
  4) Initialize/update submodules to workspace-pinned commits.
  5) For each III submodule:
     - ensure local base branch exists (create tracking branch if needed)
     - switch to base branch
     - fast-forward pull from origin/base
  6) Delete local branches whose upstream is gone *and* are merged into base.

SAFETY
  - No destructive action is done without passing --yes.
  - Branch deletion only happens if upstream is gone and branch is merged.

OPTIONS
  --base <branch>
      Base branch to sync to. Default: develop.

  --yes
      Apply changes. Without this flag the script runs in dry-run mode.

EXAMPLES
  scripts/post_pr_sync.sh
  scripts/post_pr_sync.sh --base develop
  scripts/post_pr_sync.sh --base develop --yes
USAGE
}

base_branch="develop"
apply=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      base_branch="${2:-}"
      shift 2
      ;;
    --yes)
      apply=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

is_iii_repo() {
  local p="$1"
  [[ "$p" == src/III-* || "$p" == tools/III-* ]]
}

assert_clean_repo() {
  local repo="$1"
  local label="$2"
  if [[ -n "$(git -C "$repo" status --porcelain 2>/dev/null || true)" ]]; then
    echo "ERROR: $label has uncommitted changes. Commit/stash/clean first." >&2
    git -C "$repo" status --short >&2 || true
    exit 2
  fi
}

assert_clean_workspace_ignoring_px4() {
  local repo="$1"
  local raw filtered
  raw="$(git -C "$repo" status --porcelain 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$raw" | sed -E '/^.. PX4-Autopilot(\/|$)/d')"

  if [[ -n "${filtered//[$'\n\r\t ']}" ]]; then
    echo "ERROR: workspace has uncommitted changes (excluding PX4-Autopilot ignore rule). Commit/stash/clean first." >&2
    printf '%s\n' "$filtered" >&2
    exit 2
  fi
}

run_or_echo() {
  if (( apply == 1 )); then
    "$@"
  else
    echo "DRY-RUN: $*"
  fi
}

ensure_local_tracking_branch() {
  local repo="$1"
  local branch="$2"

  if git -C "$repo" rev-parse --verify --quiet "$branch" >/dev/null; then
    return
  fi

  if ! git -C "$repo" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
    echo "ERROR: $repo missing origin/$branch" >&2
    exit 1
  fi

  if (( apply == 1 )); then
    git -C "$repo" switch -c "$branch" --track "origin/$branch"
  else
    echo "DRY-RUN: git -C $repo switch -c $branch --track origin/$branch"
  fi
}

delete_gone_merged_branches() {
  local repo="$1"
  local label="$2"
  local current
  current="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"

  while IFS=$'\t' read -r branch upstream track; do
    [[ -z "$branch" ]] && continue

    # Never touch protected/current branches.
    if [[ "$branch" == "$base_branch" || "$branch" == "main" || "$branch" == "master" || "$branch" == "staging" || "$branch" == "$current" ]]; then
      continue
    fi

    # Only consider branches that used to track a remote and are now gone.
    if [[ -z "$upstream" || "$track" != *"[gone]"* ]]; then
      continue
    fi

    if git -C "$repo" merge-base --is-ancestor "$branch" "$base_branch"; then
      if (( apply == 1 )); then
        git -C "$repo" branch -d "$branch"
      else
        echo "DRY-RUN: git -C $repo branch -d $branch"
      fi
    else
      echo "WARN: $label branch '$branch' has gone upstream but is not merged into '$base_branch'; keeping it." >&2
    fi
  done < <(git -C "$repo" for-each-ref --format='%(refname:short)\t%(upstream:short)\t%(upstream:track)' refs/heads)
}

mapfile -t submodule_paths < <(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}')
mapfile -t iii_submodules < <(
  for p in "${submodule_paths[@]}"; do
    if is_iii_repo "$p" && [[ -d "$p" ]]; then
      echo "$p"
    fi
  done
)

# Safety checks.
assert_clean_workspace_ignoring_px4 "$root"
for p in "${iii_submodules[@]}"; do
  assert_clean_repo "$p" "III submodule $p"
done

echo "Workspace: $root"
echo "Base branch: $base_branch"
echo "III submodules (${#iii_submodules[@]}):"
for p in "${iii_submodules[@]}"; do
  echo "  - $p"
done

# Workspace sync.
run_or_echo git fetch --prune origin
if ! git rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
  echo "ERROR: workspace missing origin/$base_branch" >&2
  exit 1
fi
run_or_echo git switch "$base_branch"
run_or_echo git pull --ff-only origin "$base_branch"

# Sync submodule checkout to workspace pointers.
run_or_echo git submodule sync --recursive
run_or_echo git submodule update --init --recursive

# III submodule sync.
for p in "${iii_submodules[@]}"; do
  echo
  echo "== $p =="
  run_or_echo git -C "$p" fetch --prune origin

  if ! git -C "$p" rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: $p missing origin/$base_branch" >&2
    exit 1
  fi

  ensure_local_tracking_branch "$p" "$base_branch"
  run_or_echo git -C "$p" switch "$base_branch"
  run_or_echo git -C "$p" pull --ff-only origin "$base_branch"

  delete_gone_merged_branches "$p" "$p"
done

# Workspace branch cleanup.
delete_gone_merged_branches "$root" "workspace"

echo
if (( apply == 1 )); then
  echo "Post-PR sync complete."
else
  echo "DRY-RUN complete. Re-run with --yes to apply changes."
fi
