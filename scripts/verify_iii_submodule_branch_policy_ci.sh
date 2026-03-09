#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/verify_iii_submodule_branch_policy_ci.sh --base <base-branch> --feature <feature-branch>

CI-safe III submodule branch policy verifier.
It validates that each III submodule pinned commit (HEAD in the checked-out submodule)
is reachable from at least one branch in the allowed stack:
  base -> ... -> feature

This avoids relying on local branch names (submodules are often detached in CI).
USAGE
}

base_branch=""
feature_branch=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) base_branch="${2:-}"; shift 2 ;;
    --feature) feature_branch="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$base_branch" || -z "$feature_branch" ]]; then
  echo "error: --base and --feature are required" >&2
  usage
  exit 1
fi

WORKSPACE_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$WORKSPACE_DIR" ]]; then
  echo "error: not inside a git repository" >&2
  exit 1
fi
cd "$WORKSPACE_DIR"

git fetch --no-tags origin "$base_branch" "$feature_branch" >/dev/null 2>&1 || true

if ! git rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
  echo "error: missing origin/$base_branch in checkout" >&2
  exit 1
fi
if ! git rev-parse --verify --quiet "origin/$feature_branch" >/dev/null; then
  echo "error: missing origin/$feature_branch in checkout" >&2
  exit 1
fi

mapfile -t allowed_branches < <(
  git for-each-ref --format='%(refname:short)' refs/remotes/origin | while read -r rb; do
    b="${rb#origin/}"
    if git merge-base --is-ancestor "origin/$base_branch" "origin/$b" \
      && git merge-base --is-ancestor "origin/$b" "origin/$feature_branch"; then
      echo "$b"
    fi
  done
)

if (( ${#allowed_branches[@]} == 0 )); then
  echo "error: no allowed branch stack inferred for origin/$base_branch..origin/$feature_branch" >&2
  exit 1
fi

echo "Allowed III branch stack (CI):"
for b in "${allowed_branches[@]}"; do
  echo "  - $b"
done

mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)

mismatches=0
for p in "${iii_submodules[@]}"; do
  [[ ! -d "$p" ]] && continue
  commit="$(git -C "$p" rev-parse HEAD)"
  git -C "$p" fetch --no-tags origin >/dev/null 2>&1 || true

  ok=0
  for b in "${allowed_branches[@]}"; do
    if git -C "$p" rev-parse --verify --quiet "origin/$b" >/dev/null; then
      if git -C "$p" merge-base --is-ancestor "$commit" "origin/$b"; then
        ok=1
        break
      fi
    fi
  done

  if (( ok == 1 )); then
    echo "[OK] $p @ $commit is reachable from allowed stack"
  else
    echo "[MISMATCH] $p @ $commit is not reachable from allowed stack" >&2
    mismatches=$((mismatches + 1))
  fi
done

if (( mismatches > 0 )); then
  echo "III branch policy check failed for $mismatches submodule(s)." >&2
  exit 1
fi

echo "III branch policy check passed."
