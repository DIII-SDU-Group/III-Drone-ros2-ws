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
  git -C "$WORKSPACE_DIR" submodule status --recursive \
    | sed -E 's/^[ +-U]?([0-9a-f]{40}) ([^ ]+).*/\2 \1/' \
    | sort
} > "$LOCK_FILE"

echo "Updated $LOCK_FILE"
