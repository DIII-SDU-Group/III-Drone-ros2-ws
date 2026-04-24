#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  push_stack.sh - push coordinated III submodule + workspace feature branches without PR creation

SYNOPSIS
  scripts/git/push_stack.sh --base <base-branch> [--feature <feature-branch>] [--all-iii] [--yes]
  scripts/git/push_stack.sh -h | --help

DESCRIPTION
  Top-centric helper for a workspace feature branch workflow with III submodules.

  For affected III submodules (src/III-*, tools/III-*), this script:
  1) verifies branch consistency with iii_branch_guard.sh
  2) pushes the feature branch in each target submodule
  3) pushes the workspace feature branch

  It is the push-only counterpart to create_stack_prs.sh. Use it when you want
  the coordinated feature branches published to origin, but do not want any PRs
  created or updated yet.

REQUIREMENTS
  - write permission to workspace and submodule remotes
  - clean enough branches for push (no unresolved divergence)

OPTIONS
  --base <base-branch>
      Required base branch (usually develop).

  --feature <feature-branch>
      Optional feature branch. Default: current workspace branch.

  --all-iii
      Target all III submodules instead of only changed ones.

  --yes
      Apply mode. Without --yes the script runs in dry-run.

BEHAVIOR
  - By default, targets affected III submodules, detected as either:
    - locally changed submodule working tree, or
    - committed workspace gitlink change in <base>...<feature>.
  - With --all-iii, targets all III submodules.
  - Ignores changed non-III PX4-Autopilot (consistent with iii_branch_guard.sh).
  - Any other changed non-III submodule blocks execution.
  - For each target III submodule, requires actual commits on feature vs base
    before pushing a missing remote branch.
  - Dirty worktrees are not pushed; only committed branch state is published.

EXAMPLES
  scripts/git/push_stack.sh --base develop --feature version-migration
  scripts/git/push_stack.sh --base develop --feature version-migration --yes
  scripts/git/push_stack.sh --base main --feature release/develop-to-main-2026-03 --all-iii --yes
USAGE
}

base_branch=""
feature_branch=""
all_iii=0
apply=0

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      base_branch="${2:-}"
      shift 2
      ;;
    --feature)
      feature_branch="${2:-}"
      shift 2
      ;;
    --all-iii)
      all_iii=1
      shift
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

if [[ -z "$base_branch" ]]; then
  echo "ERROR: --base is required" >&2
  exit 1
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

if [[ -z "$feature_branch" ]]; then
  feature_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
fi
if [[ -z "$feature_branch" ]]; then
  echo "ERROR: workspace is detached HEAD; pass --feature explicitly and checkout a branch" >&2
  exit 1
fi

audit_out="$(mktemp)"
trap 'rm -f "$audit_out"' EXIT

if ! scripts/git/iii_branch_guard.sh audit --base "$base_branch" --feature "$feature_branch" >"$audit_out"; then
  cat "$audit_out"
  echo "ERROR: iii_branch_guard audit failed" >&2
  exit 1
fi

mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)

declare -A target_map=()

if (( all_iii == 1 )); then
  for p in "${iii_submodules[@]}"; do
    target_map["$p"]=1
  done
else
  for p in "${iii_submodules[@]}"; do
    [[ ! -d "$p" ]] && continue
    if [[ -n "$(git -C "$p" status --porcelain 2>/dev/null || true)" ]]; then
      target_map["$p"]=1
    fi
  done

  for p in "${iii_submodules[@]}"; do
    if ! git diff --quiet "${base_branch}...${feature_branch}" -- "$p"; then
      target_map["$p"]=1
    fi
  done
fi

if (( ${#target_map[@]} > 0 )); then
  mapfile -t targets < <(printf '%s\n' "${!target_map[@]}" | sed '/^$/d' | sort)
else
  targets=()
fi

workspace_only_mode=0
if (( ${#targets[@]} == 0 )); then
  workspace_only_mode=1
fi

filtered_targets=()
early_skipped_base_clean=()
for p in "${targets[@]}"; do
  [[ -z "$p" ]] && continue
  sub_branch="$(git -C "$p" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  sub_dirty="$(git -C "$p" status --porcelain 2>/dev/null || true)"
  if [[ "$sub_branch" == "$base_branch" && -z "$sub_dirty" ]]; then
    early_skipped_base_clean+=("$p")
    continue
  fi
  filtered_targets+=("$p")
done
targets=("${filtered_targets[@]}")

if (( ${#targets[@]} == 0 )); then
  workspace_only_mode=1
  echo "No actionable III submodules detected."
  if (( ${#early_skipped_base_clean[@]} > 0 )); then
    echo "Skipped from start (already on base branch and clean):"
    for p in "${early_skipped_base_clean[@]}"; do
      echo "  - $p"
    done
  fi
fi

echo "Workspace branch: $feature_branch"
echo "Base branch: $base_branch"
target_mode="changed only"
if (( all_iii == 1 )); then
  target_mode="all III"
fi
echo "Target mode: $target_mode"
if (( workspace_only_mode == 1 )); then
  echo "Candidate III submodules (0)"
  echo "Workspace-only push mode: enabled"
else
  echo "Candidate III submodules (${#targets[@]}):"
  for p in "${targets[@]}"; do
    echo "  - $p"
  done
fi
if (( ${#early_skipped_base_clean[@]} > 0 )); then
  echo "Skipped from start (already on base branch and clean):"
  for p in "${early_skipped_base_clean[@]}"; do
    echo "  - $p"
  done
fi

mapfile -t allowed_branches < <(
  git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
    if git merge-base --is-ancestor "$base_branch" "$b" && git merge-base --is-ancestor "$b" "$feature_branch"; then
      echo "$b"
    fi
  done
)

echo "Allowed submodule branches from workspace stack:"
for b in "${allowed_branches[@]}"; do
  echo "  - $b"
done

skipped_no_delta=()
skipped_branch_mismatch=()
skipped_dirty=()
pushed_targets=()

for p in "${targets[@]}"; do
  [[ -z "$p" ]] && continue
  echo
  echo "== $p =="
  sub_branch="$(git -C "$p" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$sub_branch" ]]; then
    echo "WARN: $p is detached HEAD; skipping push for this repo." >&2
    skipped_branch_mismatch+=("$p")
    continue
  fi

  in_allowed=0
  for b in "${allowed_branches[@]}"; do
    if [[ "$sub_branch" == "$b" ]]; then
      in_allowed=1
      break
    fi
  done

  if (( in_allowed == 0 )); then
    echo "WARN: $p is on '$sub_branch' (outside allowed stack base->feature); skipping push for this repo." >&2
    skipped_branch_mismatch+=("$p")
    continue
  fi

  sub_dirty="$(git -C "$p" status --porcelain 2>/dev/null || true)"
  if [[ -n "$sub_dirty" ]]; then
    echo "WARN: $p has uncommitted changes; only committed branch state can be pushed. Skipping." >&2
    skipped_dirty+=("$p")
    continue
  fi

  git -C "$p" fetch --no-tags origin "$base_branch" >/dev/null 2>&1
  if ! git -C "$p" rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: $p missing origin/$base_branch; cannot validate branch delta." >&2
    exit 1
  fi

  has_remote_feature=0
  if git -C "$p" ls-remote --exit-code --heads origin "$feature_branch" >/dev/null 2>&1; then
    has_remote_feature=1
  fi

  if (( has_remote_feature == 0 )); then
    delta_count="$(git -C "$p" rev-list --count "origin/$base_branch..$feature_branch" 2>/dev/null || echo 0)"
    if [[ "$delta_count" == "0" ]]; then
      echo "WARN: $p local '$feature_branch' has no commits beyond origin/$base_branch; skipping push for this submodule." >&2
      echo "      Hint: ./scripts/git/post_pr_sync.sh --base $base_branch --clean-only --yes" >&2
      skipped_no_delta+=("$p")
      continue
    fi
  fi

  local_head="$(git -C "$p" rev-parse --short HEAD)"
  if (( apply == 1 )); then
    if (( has_remote_feature == 1 )); then
      git -C "$p" push origin "$feature_branch"
    else
      git -C "$p" push -u origin "$feature_branch"
    fi
  else
    if (( has_remote_feature == 1 )); then
      echo "DRY-RUN: would push $p:$feature_branch"
    else
      echo "DRY-RUN: would push -u $p:$feature_branch (remote branch missing)"
    fi
  fi
  echo "Submodule HEAD: $local_head"
  pushed_targets+=("$p")
done

echo
if (( ${#pushed_targets[@]} > 0 )); then
  echo "Pushed or prepared III submodules (${#pushed_targets[@]}):"
  for p in "${pushed_targets[@]}"; do
    echo "  - $p"
  done
else
  echo "No III submodule branches were pushed."
fi

if (( ${#skipped_no_delta[@]} > 0 )); then
  echo
  echo "Skipped III submodules with no feature-vs-base delta (${#skipped_no_delta[@]}):"
  for p in "${skipped_no_delta[@]}"; do
    echo "  - $p"
  done
fi

if (( ${#skipped_branch_mismatch[@]} > 0 )); then
  echo
  echo "Skipped III submodules not on workspace feature branch (${#skipped_branch_mismatch[@]}):"
  for p in "${skipped_branch_mismatch[@]}"; do
    echo "  - $p"
  done
fi

if (( ${#skipped_dirty[@]} > 0 )); then
  echo
  echo "Skipped dirty III submodules (${#skipped_dirty[@]}):"
  for p in "${skipped_dirty[@]}"; do
    echo "  - $p"
  done
fi

workspace_dirty="$(git status --porcelain --untracked-files=no -- . ':(exclude)PX4-Autopilot' ':(exclude)src/III-Drone-Configuration' ':(exclude)src/III-Drone-Core' ':(exclude)src/III-Drone-GC' ':(exclude)src/III-Drone-Interfaces' ':(exclude)src/III-Drone-Mission' ':(exclude)src/III-Drone-Simulation' ':(exclude)src/III-Drone-Simulation/Gazebo-simulation-assets' ':(exclude)src/III-Drone-Supervision' ':(exclude)tools/III-Drone-CLI')"
if [[ -n "$workspace_dirty" ]]; then
  echo
  echo "WARN: workspace has uncommitted changes outside submodule worktrees; only committed branch state will be pushed." >&2
fi

echo
if (( apply == 1 )); then
  git push -u origin "$feature_branch"
  echo "Workspace branch pushed: $feature_branch"
  echo "Done."
else
  echo "DRY-RUN: would push workspace branch '$feature_branch'"
  echo "DRY-RUN complete. Re-run with --yes to push branches without creating PRs."
fi
