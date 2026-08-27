#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_main_to_release_pr.sh - create/update the workspace-only main-to-release PR

SYNOPSIS
  scripts/git/create_main_to_release_pr.sh [--operation-id <id>] [--yes]

DESCRIPTION
  Opens or updates the only supported release promotion: protected workspace
  main into protected workspace release. The head is always main, so this helper
  cannot introduce release-only implementation changes and never creates
  submodule release branches. Dry-run is the default.

OPTIONS
  --operation-id <id>
      Stable retained-plan identifier. Default: promote-main-to-release.

  --yes   Create or update the pull request. Without it, print the exact plan.
USAGE
}

apply=0
operation_id="promote-main-to-release"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --operation-id) operation_id="${2:-}"; shift 2 ;;
    --yes) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done
if [[ ! "$operation_id" =~ ^[a-z0-9][a-z0-9-]{7,63}$ ]]; then
  echo "ERROR: --operation-id must match [a-z0-9][a-z0-9-]{7,63}" >&2
  exit 1
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

plan_state_root="${XDG_STATE_HOME:-$HOME/.local/state}/iii/automation-plans"
plan_output="$(mktemp)"
trap 'rm -f "$plan_output"' EXIT
plan_arguments=(
  --operation-id "$operation_id"
  --state-root "$plan_state_root"
)
if (( apply == 1 )); then
  plan_arguments+=(--verify-existing)
fi
python3 scripts/git/create_release_pr_plan.py "${plan_arguments[@]}" >"$plan_output"
plan_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$plan_output")"

title="chore(release): promote main to release"
body="$(cat <<EOF
Workspace-only stable promotion from protected main to protected release. The source branch is fixed; release-only implementation changes and submodule release branches are not permitted.

<!-- iii-pr-transport-v1; untrusted display metadata -->
Operation ID: $operation_id
Retained plan ID: $plan_id
EOF
)"

echo "Source branch: main"
echo "Target branch: release"
echo "Submodule release branches: prohibited"
echo "Operation ID: $operation_id"
echo "Retained automation plan: $plan_state_root/$operation_id.plan.json"
if (( apply == 0 )); then
  echo "DRY-RUN: would create or update the workspace PR main -> release"
  echo "DRY-RUN complete. Re-run with --yes to apply."
  exit 0
fi

if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: authenticated gh CLI is required" >&2
  exit 1
fi

repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
existing="$(gh pr list --repo "$repo" --head main --base release --state open --json url -q '.[0].url' 2>/dev/null || true)"
if [[ -n "$existing" && "$existing" != "null" ]]; then
  gh pr edit "$existing" --repo "$repo" --title "$title" --body "$body"
  echo "Updated: $existing"
else
  gh pr create --repo "$repo" --head main --base release --title "$title" --body "$body"
fi
