#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
LOCK_FILE="$WORKSPACE_DIR/deps/submodule-lock.txt"

if ! git -C "$WORKSPACE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: $WORKSPACE_DIR is not a git repository" >&2
  exit 1
fi

{
  echo "# Submodule dependency lock file for III-Drone-ros2-ws."
  echo "# Format: <path> <commit-sha>"
  echo "# Managed by scripts/update_submodule_lock.sh"
  git -C "$WORKSPACE_DIR" submodule status --recursive \
    | sed -E 's/^[ +-U]?([0-9a-f]{40}) ([^ ]+).*/\2 \1/' \
    | sort
} > "$LOCK_FILE"

echo "Updated $LOCK_FILE"
