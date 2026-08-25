#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_main_to_release_pr.sh - create/update the workspace-only main-to-release PR

SYNOPSIS
  scripts/git/create_main_to_release_pr.sh [--yes]

DESCRIPTION
  Opens or updates the only supported release promotion: protected workspace
  main into protected workspace release. The head is always main, so this helper
  cannot introduce release-only implementation changes and never creates
  submodule release branches. Dry-run is the default.

OPTIONS
  --yes   Create or update the pull request. Without it, print the exact plan.
USAGE
}

apply=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

title="chore(release): promote main to release"
body="Workspace-only stable promotion from protected main to protected release. The source branch is fixed; release-only implementation changes and submodule release branches are not permitted."

echo "Source branch: main"
echo "Target branch: release"
echo "Submodule release branches: prohibited"
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
