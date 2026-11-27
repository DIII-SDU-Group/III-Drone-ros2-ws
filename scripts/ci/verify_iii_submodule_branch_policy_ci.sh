#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/ci/verify_iii_submodule_branch_policy_ci.sh --base <base-branch> --feature <feature-branch>

CI-safe III submodule branch policy verifier.
It validates that each changed III submodule pinned commit (HEAD in the
checked-out submodule) is reachable from at least one branch in the allowed
stack:
  base -> ... -> feature
Governed forks (PX4-Autopilot, px4-ros2-interface-lib, BehaviorTree.*,
iwr6843aop-ROS2-pkg) follow the same rule, and may also pin a commit on the
fork's default branch (an unmodified upstream revision). A fork pin that lives
only on a temporary branch would disappear when that branch is deleted.

This avoids relying on local branch names (submodules are often detached in CI)
and is intended for pull-request validation, not day-to-day local branch
management.
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

mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)
governed_forks=(PX4-Autopilot src/px4-ros2-interface-lib src/BehaviorTree.CPP src/BehaviorTree.ROS2 src/iwr6843aop-ROS2-pkg)
is_fork() {
  local candidate="$1" fork
  for fork in "${governed_forks[@]}"; do
    [[ "$fork" == "$candidate" ]] && return 0
  done
  return 1
}

changed_iii_submodules=()
for p in "${iii_submodules[@]}" "${governed_forks[@]}"; do
  if ! git diff --quiet "origin/$base_branch...origin/$feature_branch" -- "$p"; then
    changed_iii_submodules+=("$p")
  fi
done

if (( ${#changed_iii_submodules[@]} == 0 )); then
  echo "No III submodule gitlink changes detected between origin/$base_branch and origin/$feature_branch."
  echo "Skipping III branch policy check for workspace-only PR."
  exit 0
fi

if (( ${#allowed_branches[@]} == 0 )); then
  echo "error: no allowed branch stack inferred for origin/$base_branch..origin/$feature_branch" >&2
  exit 1
fi

echo "Allowed III branch stack (CI):"
for b in "${allowed_branches[@]}"; do
  echo "  - $b"
done

echo "Changed III submodules in PR (${#changed_iii_submodules[@]}):"
for p in "${changed_iii_submodules[@]}"; do
  echo "  - $p"
done

mismatches=0
for p in "${changed_iii_submodules[@]}"; do
  [[ ! -d "$p" ]] && continue
  commit="$(git -C "$p" rev-parse HEAD)"
  git -C "$p" fetch --no-tags origin >/dev/null 2>&1 || true

  ok=0
  candidates=("${allowed_branches[@]}")
  if is_fork "$p"; then
    default_branch="$(git -C "$p" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    [[ -n "$default_branch" ]] && candidates+=("${default_branch#origin/}")
  fi
  for b in "${candidates[@]}"; do
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
