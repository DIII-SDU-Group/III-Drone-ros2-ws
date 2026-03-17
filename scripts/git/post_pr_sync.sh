#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  post_pr_sync.sh - safe post-PR sync for workspace + III submodules

SYNOPSIS
  scripts/git/post_pr_sync.sh [--base <branch>] [--clean-only] [--yes]
  scripts/git/post_pr_sync.sh -h | --help

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
     Also deletes local branches with no matching remote branch when they add
     no commits beyond base.

  Use this after merged PRs to reset your local workspace and III submodules
  back onto the shared base branch without manually cleaning every repository.

SAFETY
  - No destructive action is done without passing --yes.
  - Branch deletion only happens when branch has no commits beyond base and
    has no matching origin branch.
  - Hardcoded protected branches from deletion: main, develop.

OPTIONS
  --base <branch>
      Base branch to sync to. Default: develop.

  --clean-only
      Do not fail if workspace or some III submodules are dirty.
      Instead, skip dirty targets and sync only clean ones.

  --yes
      Apply changes. Without this flag the script runs in dry-run mode.

EXAMPLES
  scripts/git/post_pr_sync.sh
  scripts/git/post_pr_sync.sh --base develop
  scripts/git/post_pr_sync.sh --base develop --clean-only
  scripts/git/post_pr_sync.sh --base develop --yes
USAGE
}

base_branch="develop"
apply=0
clean_only=0

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
    --clean-only)
      clean_only=1
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

repo_is_clean() {
  local repo="$1"
  [[ -z "$(git -C "$repo" status --porcelain 2>/dev/null || true)" ]]
}

workspace_is_clean_ignoring_px4() {
  local repo="$1"
  local raw filtered
  raw="$(git -C "$repo" status --porcelain 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$raw" | sed -E '/^.. PX4-Autopilot(\/|$)/d')"

  [[ -z "${filtered//[$'\n\r\t ']}" ]]
}

print_workspace_dirty_ignoring_px4() {
  local repo="$1"
  local raw filtered
  raw="$(git -C "$repo" status --porcelain 2>/dev/null || true)"
  filtered="$(printf '%s\n' "$raw" | sed -E '/^.. PX4-Autopilot(\/|$)/d')"
  printf '%s\n' "$filtered"
}

run_or_echo() {
  if (( apply == 1 )); then
    "$@"
  else
    echo "DRY-RUN: $*"
  fi
}

run_always() {
  "$@"
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

delete_stale_local_branches() {
  local repo="$1"
  local label="$2"
  local current_override="${3:-}"
  local current
  if [[ -n "$current_override" ]]; then
    current="$current_override"
  else
    current="$(git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  fi

  while IFS='|' read -r branch upstream track; do
    [[ -z "$branch" ]] && continue

    # Never touch current branch or hardcoded protected branches.
    if [[ "$branch" == "develop" || "$branch" == "main" || "$branch" == "$current" ]]; then
      continue
    fi

    # Determine whether a same-name remote branch exists.
    has_remote_same_name=0
    if git -C "$repo" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
      has_remote_same_name=1
    fi

    # If branch still exists on remote, keep it.
    if (( has_remote_same_name == 1 )); then
      if (( apply == 0 )); then
        echo "DRY-RUN: keep $repo branch '$branch' (origin/$branch exists)"
      fi
      continue
    fi

    # Only delete if local branch adds no commits beyond remote base.
    # Equivalent to "no local changes" in branch history relative to origin/base.
    ahead_count="$(git -C "$repo" rev-list --count "origin/$base_branch..$branch" 2>/dev/null || echo 1)"
    if [[ "$ahead_count" == "0" ]]; then
      if (( apply == 1 )); then
        git -C "$repo" branch -d "$branch"
      else
        echo "DRY-RUN: git -C $repo branch -d $branch"
      fi
    else
      if [[ -n "$upstream" && "$track" == *"[gone]"* ]]; then
        echo "WARN: $label branch '$branch' has gone upstream but has local commits beyond '$base_branch'; keeping it." >&2
      else
        echo "WARN: $label branch '$branch' has no matching remote branch and has local commits beyond '$base_branch'; keeping it." >&2
      fi
    fi
  done < <(git -C "$repo" for-each-ref --format='%(refname:short)|%(upstream:short)|%(upstream:track)' refs/heads)
}

mapfile -t submodule_paths < <(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}')
mapfile -t iii_submodules < <(
  for p in "${submodule_paths[@]}"; do
    if is_iii_repo "$p" && [[ -d "$p" ]]; then
      echo "$p"
    fi
  done
)

workspace_clean=1
if ! workspace_is_clean_ignoring_px4 "$root"; then
  workspace_clean=0
fi

clean_submodules=()
dirty_submodules=()
for p in "${iii_submodules[@]}"; do
  if repo_is_clean "$p"; then
    clean_submodules+=("$p")
  else
    dirty_submodules+=("$p")
  fi
done

# Safety checks / skip policy.
if (( clean_only == 0 )); then
  if (( workspace_clean == 0 )); then
    echo "ERROR: workspace has uncommitted changes (excluding PX4-Autopilot ignore rule). Commit/stash/clean first." >&2
    print_workspace_dirty_ignoring_px4 "$root" >&2
    exit 2
  fi
  if (( ${#dirty_submodules[@]} > 0 )); then
    echo "ERROR: dirty III submodules detected. Commit/stash/clean first." >&2
    for p in "${dirty_submodules[@]}"; do
      echo "-- $p" >&2
      git -C "$p" status --short >&2 || true
    done
    exit 2
  fi
else
  if (( workspace_clean == 0 )); then
    echo "WARN: workspace is dirty (ignoring PX4-Autopilot rule). Workspace sync will be skipped." >&2
    print_workspace_dirty_ignoring_px4 "$root" >&2
  fi
  if (( ${#dirty_submodules[@]} > 0 )); then
    echo "WARN: dirty III submodules will be skipped:" >&2
    for p in "${dirty_submodules[@]}"; do
      echo "  - $p" >&2
    done
  fi
fi

echo "Workspace: $root"
echo "Base branch: $base_branch"
echo "III submodules total: ${#iii_submodules[@]}"
echo "III submodules to sync: ${#clean_submodules[@]}"
echo "III submodules skipped (dirty): ${#dirty_submodules[@]}"
for p in "${clean_submodules[@]}"; do
  echo "  - $p"
done

# Workspace sync (unless skipped in clean-only mode).
if (( workspace_clean == 1 )); then
  run_always git fetch --prune origin
  if ! git rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: workspace missing origin/$base_branch" >&2
    exit 1
  fi
  run_or_echo git switch "$base_branch"
  run_or_echo git pull --ff-only origin "$base_branch"

  # Sync submodule checkout to workspace pointers.
  run_or_echo git submodule sync --recursive
  run_or_echo git submodule update --init --recursive
else
  echo "Skipping workspace sync because workspace is dirty and --clean-only was set."
fi

# III submodule sync.
for p in "${clean_submodules[@]}"; do
  echo
  echo "== $p =="
  run_always git -C "$p" fetch --prune origin

  if ! git -C "$p" rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: $p missing origin/$base_branch" >&2
    exit 1
  fi

  ensure_local_tracking_branch "$p" "$base_branch"
  run_or_echo git -C "$p" switch "$base_branch"
  run_or_echo git -C "$p" pull --ff-only origin "$base_branch"

  # In dry-run we don't actually switch branches, so pass intended current branch
  # to get accurate planned deletion output.
  delete_stale_local_branches "$p" "$p" "$base_branch"
done

# Workspace branch cleanup.
if (( workspace_clean == 1 )); then
  delete_stale_local_branches "$root" "workspace" "$base_branch"
fi

echo
not_synced_count=0
if (( workspace_clean == 0 )); then
  not_synced_count=$((not_synced_count + 1))
fi
not_synced_count=$((not_synced_count + ${#dirty_submodules[@]}))

if (( not_synced_count > 0 )); then
  echo "Not synced due to unclean working tree:"
  if (( workspace_clean == 0 )); then
    echo "  - workspace (top-level repo)"
  fi
  for p in "${dirty_submodules[@]}"; do
    echo "  - $p"
  done
  echo
fi

if (( apply == 1 )); then
  echo "Post-PR sync complete."
else
  echo "DRY-RUN complete. Re-run with --yes to apply changes."
fi
