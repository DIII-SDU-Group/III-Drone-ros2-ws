#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOCK_FILE="$WORKSPACE_DIR/deps/submodule-lock.txt"

usage() {
  cat <<'USAGE'
NAME
  update_submodule_lock.sh - regenerate deps/submodule-lock.txt from current submodule pointers

SYNOPSIS
  scripts/git/update_submodule_lock.sh

DESCRIPTION
  Rewrites `deps/submodule-lock.txt` from the workspace's current recursive
  submodule state. Run this only when submodule pointer changes are intentional
  and should become part of the committed workspace state.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! git -C "$WORKSPACE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: $WORKSPACE_DIR is not a git repository" >&2
  exit 1
fi

{
  echo "# Submodule dependency lock file for III-Drone-ros2-ws."
  echo "# Format: <path> <commit-sha>"
  echo "# Managed by scripts/git/update_submodule_lock.sh"
  # `git submodule status` reports the superproject index SHA, even when an
  # initialized submodule worktree has advanced and the gitlink has not yet
  # been staged.  This command is explicitly an update-from-current-worktrees
  # operation, so inventory each checked-out HEAD instead.
  git -C "$WORKSPACE_DIR" submodule foreach --recursive --quiet \
    'printf "%s %s\n" "$displaypath" "$(git rev-parse HEAD)"' \
    | sort
} > "$LOCK_FILE"

echo "Updated $LOCK_FILE"
