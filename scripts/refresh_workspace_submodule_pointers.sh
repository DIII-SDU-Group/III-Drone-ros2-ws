#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  refresh_workspace_submodule_pointers.sh - update workspace III submodule pointers to remote base heads

SYNOPSIS
  scripts/refresh_workspace_submodule_pointers.sh [--base <branch>] [--feature <branch>] [--all-iii] [--yes]
  scripts/refresh_workspace_submodule_pointers.sh -h | --help

DESCRIPTION
  Use this after submodule PRs are merged (which creates merge commits), so the
  workspace branch points to the merged commits on origin/<base>.

  By default, targets III submodules with gitlink delta in <base>...<feature>.
  For each target submodule, it:
  - fetches origin/<base>
  - moves submodule working tree HEAD to origin/<base> (detached)
  - stages updated gitlink in workspace
  - updates deps/submodule-lock.txt

OPTIONS
  --base <branch>
      Base branch whose remote head should be pinned. Default: develop.

  --feature <branch>
      Workspace feature branch to compare against base for target detection.
      Default: current workspace branch.

  --all-iii
      Refresh all III submodules instead of only affected ones.

  --yes
      Apply changes. Without --yes, runs in dry-run mode.

EXAMPLES
  scripts/refresh_workspace_submodule_pointers.sh --base develop
  scripts/refresh_workspace_submodule_pointers.sh --base develop --yes
  scripts/refresh_workspace_submodule_pointers.sh --base develop --feature version-migration --yes
USAGE
}

base_branch="develop"
feature_branch=""
all_iii=0
apply=0

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
  echo "ERROR: workspace is detached HEAD; pass --feature explicitly" >&2
  exit 1
fi

mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)

targets=()
if (( all_iii == 1 )); then
  targets=("${iii_submodules[@]}")
else
  for p in "${iii_submodules[@]}"; do
    if ! git diff --quiet "${base_branch}...${feature_branch}" -- "$p"; then
      targets+=("$p")
    fi
  done
fi

if (( ${#targets[@]} == 0 )); then
  echo "No target III submodules found for refresh."
  exit 0
fi

# Safety: avoid stomping local submodule edits.
for p in "${targets[@]}"; do
  if [[ ! -d "$p" ]]; then
    continue
  fi
  if [[ -n "$(git -C "$p" status --porcelain 2>/dev/null || true)" ]]; then
    echo "ERROR: submodule '$p' has uncommitted changes. Commit/stash/clean first." >&2
    git -C "$p" status --short >&2 || true
    exit 2
  fi
done

echo "Workspace branch: $feature_branch"
echo "Target remote base: origin/$base_branch"
echo "III submodules to refresh (${#targets[@]}):"
for p in "${targets[@]}"; do
  echo "  - $p"
done

changed=0
for p in "${targets[@]}"; do
  [[ ! -d "$p" ]] && continue
  echo
  echo "== $p =="

  git -C "$p" fetch --prune origin "$base_branch" >/dev/null 2>&1
  if ! git -C "$p" rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: missing origin/$base_branch in $p" >&2
    exit 1
  fi

  old_sha="$(git ls-tree HEAD "$p" | awk '{print $3}')"
  new_sha="$(git -C "$p" rev-parse "origin/$base_branch")"

  echo "  old pointer: $old_sha"
  echo "  new pointer: $new_sha"

  if [[ "$old_sha" == "$new_sha" ]]; then
    echo "  no change"
    continue
  fi

  changed=1
  if (( apply == 1 )); then
    git -C "$p" checkout --detach "$new_sha" >/dev/null
    git add "$p"
  else
    echo "  DRY-RUN: would checkout --detach $new_sha and stage gitlink"
  fi
done

if (( apply == 1 )); then
  ./scripts/update_submodule_lock.sh
  git add deps/submodule-lock.txt
  echo
  if (( changed == 1 )); then
    echo "Refreshed submodule pointers and lock file."
    echo "Next: commit + push workspace branch, then update workspace PR."
  else
    echo "No pointer changes detected. Lock file refreshed."
  fi
else
  echo
  if (( changed == 1 )); then
    echo "DRY-RUN complete. Re-run with --yes to apply pointer refresh and update lock file."
  else
    echo "DRY-RUN complete. No pointer changes needed."
  fi
fi
