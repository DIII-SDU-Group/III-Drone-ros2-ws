#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
iii_branch_guard.sh - audit and align III submodule branches to workspace branch policy

SYNOPSIS
  scripts/git/iii_branch_guard.sh <action> --base <base-branch> [--feature <feature-branch>] [--all-iii] [--yes]
  scripts/git/iii_branch_guard.sh -h | --help

ACTIONS
  audit
      Validate branch-tree consistency for target III submodules.
      No branch switches or branch creation are performed.

  align
      Dry-run by default: prints intended branch switches/creation.
      With --yes, applies the changes:
      - switch target submodule to existing feature branch, or
      - create feature branch from current allowed parent and switch to it.

ARGUMENTS / OPTIONS
  --base <base-branch>
      Required. The root parent branch for policy checks (e.g., develop).
      The allowed branch stack is inferred between base and feature.

  --feature <feature-branch>
      Optional. Target branch at top of stack (e.g., version-migration).
      Default: current top-level workspace branch.

  --all-iii
      Optional. Target all III submodules:
      - src/III-*
      - tools/III-*
      Default target set: only changed III submodules.

  --yes
      Optional. Apply changes in align mode.
      Ignored in audit mode.

BEHAVIOR
  - Operates only inside the current top-level git workspace.
  - Fails if top-level repo is detached HEAD (branch policy needs a branch name).
  - Fails if any changed non-III submodules are detected.
    Hardcoded exception: PX4-Autopilot is ignored.
  - For each target III submodule, current branch must be in allowed stack:
      base -> ... -> feature
  - Detached submodule HEAD is reported as mismatch.
  - In `align` mode it can switch or create local submodule branches so the
    workspace and III repos follow the same feature-stack naming policy.

EXAMPLES
  Audit changed III submodules against develop -> current workspace branch:
    scripts/git/iii_branch_guard.sh audit --base develop

  Audit all III submodules against develop -> version-migration:
    scripts/git/iii_branch_guard.sh audit --base develop --feature version-migration --all-iii

  Plan alignment of changed III submodules:
    scripts/git/iii_branch_guard.sh align --base develop --feature version-migration

  Apply alignment:
    scripts/git/iii_branch_guard.sh align --base develop --feature version-migration --yes
USAGE
}

mode=""
base_branch=""
feature_branch=""
all_iii=0
assume_yes=0

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

mode="$1"
shift

case "$mode" in
  audit|align) ;;
  *)
    echo "ERROR: first argument must be 'audit' or 'align'" >&2
    usage
    exit 1
    ;;
esac

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
      assume_yes=1
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

top_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [[ -z "$top_branch" ]]; then
  echo "ERROR: top-level repo is in detached HEAD; branch policy checks need a branch" >&2
  exit 1
fi

if [[ -z "$feature_branch" ]]; then
  feature_branch="$top_branch"
fi

if ! git rev-parse --verify --quiet "$base_branch" >/dev/null; then
  echo "ERROR: top-level base branch '$base_branch' does not exist locally" >&2
  exit 1
fi

if ! git merge-base --is-ancestor "$base_branch" "$top_branch"; then
  echo "WARN: top branch '$top_branch' is not based on '$base_branch' (ancestor check failed)" >&2
fi

mapfile -t submodule_paths < <(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}')

is_iii_repo() {
  local p="$1"
  [[ "$p" == src/III-* || "$p" == tools/III-* ]]
}

is_ignored_non_iii_repo() {
  local p="$1"
  [[ "$p" == "PX4-Autopilot" ]]
}

is_submodule_changed() {
  local p="$1"
  [[ -n "$(git -C "$p" status --porcelain 2>/dev/null || true)" ]]
}

changed_iii=()
changed_non_iii=()
all_iii_submodules=()
allowed_branches=()

for p in "${submodule_paths[@]}"; do
  if [[ ! -d "$p" ]]; then
    continue
  fi

  if is_iii_repo "$p"; then
    all_iii_submodules+=("$p")
  fi

  if is_submodule_changed "$p"; then
    if is_iii_repo "$p"; then
      changed_iii+=("$p")
    elif is_ignored_non_iii_repo "$p"; then
      :
    else
      changed_non_iii+=("$p")
    fi
  fi
done

if (( ${#changed_non_iii[@]} > 0 )); then
  echo "ERROR: changed non-III submodules detected (explicitly review first):" >&2
  for p in "${changed_non_iii[@]}"; do
    echo "  - $p" >&2
  done
  exit 2
fi

targets=()
if (( all_iii == 1 )); then
  targets=("${all_iii_submodules[@]}")
else
  targets=("${changed_iii[@]}")
fi

target_mode="changed only"
if (( all_iii == 1 )); then
  target_mode="all III"
fi

if (( ${#targets[@]} == 0 )); then
  echo "No target III submodules found (mode: $target_mode)."
  exit 0
fi

echo "Top-level branch: $top_branch"
echo "Expected base branch: $base_branch"
echo "Target feature branch: $feature_branch"

# Allowed branch stack for III submodules:
# any local branch that is an ancestor of feature and descendant of/equal to base.
mapfile -t allowed_branches < <(
  git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
    if git merge-base --is-ancestor "$b" "$feature_branch" && git merge-base --is-ancestor "$base_branch" "$b"; then
      echo "$b"
    fi
  done
)

if (( ${#allowed_branches[@]} == 0 )); then
  echo "ERROR: no allowed branch stack inferred between base '$base_branch' and feature '$feature_branch'" >&2
  exit 1
fi

echo "Allowed III submodule branches:"
for b in "${allowed_branches[@]}"; do
  echo "  - $b"
done

echo "Target III submodules (${#targets[@]}):"
for p in "${targets[@]}"; do
  echo "  - $p"
done

mismatches=0

check_one() {
  local p="$1"
  local current_branch
  current_branch="$(git -C "$p" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$current_branch" ]]; then
    current_branch="DETACHED"
  fi

  local has_base=1
  if ! git -C "$p" rev-parse --verify --quiet "$base_branch" >/dev/null; then
    has_base=0
  fi

  local has_feature=1
  if ! git -C "$p" rev-parse --verify --quiet "$feature_branch" >/dev/null; then
    has_feature=0
  fi

  local feature_based=1
  if (( has_base == 1 && has_feature == 1 )); then
    if ! git -C "$p" merge-base --is-ancestor "$base_branch" "$feature_branch"; then
      feature_based=0
    fi
  fi

  echo
  echo "[$p]"
  echo "  current branch: $current_branch"
  echo "  has base '$base_branch': $has_base"
  echo "  has feature '$feature_branch': $has_feature"
  if (( has_base == 1 && has_feature == 1 )); then
    echo "  feature based on base: $feature_based"
  fi

  local in_allowed=0
  for b in "${allowed_branches[@]}"; do
    if [[ "$current_branch" == "$b" ]]; then
      in_allowed=1
      break
    fi
  done

  # Tree-consistency policy:
  # - Allowed current branches: any branch in allowed_branches stack.
  # - If feature exists, it must be based on base.
  # - Any branch outside stack is a mismatch.
  local consistent=1
  local reason=""
  if [[ "$current_branch" == "DETACHED" ]]; then
    consistent=0
    reason="detached HEAD"
  elif (( in_allowed == 1 )); then
    if (( has_feature == 1 && has_base == 1 && feature_based == 0 )); then
      consistent=0
      reason="feature branch '$feature_branch' is not based on '$base_branch'"
    fi
  else
    consistent=0
    reason="current branch '$current_branch' is outside allowed stack"
  fi

  if (( consistent == 1 )); then
    echo "  consistency: OK"
  else
    echo "  consistency: MISMATCH ($reason)"
    mismatches=$((mismatches + 1))
  fi

  if [[ "$mode" == "align" ]]; then
    if [[ "$current_branch" == "DETACHED" ]]; then
      echo "  ACTION: skip (detached HEAD)"
      return
    fi

    if (( in_allowed == 0 )); then
      echo "  ACTION: skip (current branch not in allowed stack)"
      return
    fi

    if [[ "$current_branch" == "$feature_branch" ]]; then
      echo "  ACTION: no-op (already on feature branch)"
      return
    fi

    if (( has_feature == 1 )); then
      echo "  ACTION: switch to existing feature branch '$feature_branch'"
      if (( assume_yes == 1 )); then
        git -C "$p" switch "$feature_branch"
      fi
    else
      echo "  ACTION: create and switch to feature branch '$feature_branch' from current parent '$current_branch'"
      if (( assume_yes == 1 )); then
        git -C "$p" switch -c "$feature_branch"
      fi
    fi
  fi
}

for p in "${targets[@]}"; do
  check_one "$p"
done

if (( mismatches > 0 )); then
  echo
  echo "Branch-tree mismatches detected in $mismatches submodule(s)." >&2
  echo "Fix mismatches (or switch to expected branches) before pushing." >&2
  exit 3
fi

if [[ "$mode" == "align" && $assume_yes -eq 0 ]]; then
  echo
  echo "Dry run complete. Re-run with --yes to apply switches/branch creation."
fi
