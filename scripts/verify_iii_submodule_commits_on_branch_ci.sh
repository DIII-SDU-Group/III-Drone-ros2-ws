#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/verify_iii_submodule_commits_on_branch_ci.sh --target-branch <branch>

Checks each III submodule gitlink commit pinned by the workspace checkout
and verifies that commit is reachable from origin/<target-branch>
in the corresponding submodule repository.

This is intended for CI gatekeeping before merging workspace PRs.
USAGE
}

target_branch=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-branch)
      target_branch="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$target_branch" ]]; then
  echo "error: --target-branch is required" >&2
  usage
  exit 1
fi

WORKSPACE_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$WORKSPACE_DIR" ]]; then
  echo "error: not inside a git repository" >&2
  exit 1
fi
cd "$WORKSPACE_DIR"

mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)

if (( ${#iii_submodules[@]} == 0 )); then
  echo "No III submodules found."
  exit 0
fi

status_file="$(mktemp)"
trap 'rm -f "$status_file"' EXIT

mismatches=0
for p in "${iii_submodules[@]}"; do
  [[ ! -d "$p" ]] && continue

  commit="$(git -C "$p" rev-parse HEAD)"
  git -C "$p" fetch --no-tags origin "$target_branch" >/dev/null 2>&1 || true

  if ! git -C "$p" rev-parse --verify --quiet "origin/$target_branch" >/dev/null; then
    echo "[MISMATCH] $p missing origin/$target_branch" >&2
    printf '%s\t%s\t%s\t%s\n' "$p" "$commit" "MISMATCH" "missing origin/$target_branch" >> "$status_file"
    mismatches=$((mismatches + 1))
    continue
  fi

  if git -C "$p" merge-base --is-ancestor "$commit" "origin/$target_branch"; then
    echo "[OK] $p @ $commit is on origin/$target_branch"
    printf '%s\t%s\t%s\t%s\n' "$p" "$commit" "OK" "on origin/$target_branch" >> "$status_file"
  else
    echo "[MISMATCH] $p @ $commit not on origin/$target_branch" >&2
    printf '%s\t%s\t%s\t%s\n' "$p" "$commit" "MISMATCH" "not on origin/$target_branch" >> "$status_file"
    mismatches=$((mismatches + 1))
  fi
done

mkdir -p .ci
cp "$status_file" .ci/iii-submodule-status.tsv

if (( mismatches > 0 )); then
  echo "III submodule target-branch check failed for $mismatches submodule(s)." >&2
  exit 1
fi

echo "III submodule target-branch check passed."
