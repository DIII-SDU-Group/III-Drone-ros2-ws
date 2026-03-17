#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
LOCK_FILE="$WORKSPACE_DIR/deps/submodule-lock.txt"

usage() {
  cat <<'USAGE'
NAME
  verify_submodule_lock.sh - compare the committed lock file with actual submodule refs

SYNOPSIS
  scripts/git/verify_submodule_lock.sh

DESCRIPTION
  Verifies that the recursive git submodule state in the current workspace
  exactly matches `deps/submodule-lock.txt`.

  This is the local equivalent of the dependency-governance CI gate.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "$LOCK_FILE" ]]; then
  echo "error: lock file not found: $LOCK_FILE" >&2
  exit 1
fi

actual="$(mktemp)"
expected="$(mktemp)"
cleanup() {
  rm -f "$actual" "$expected"
}
trap cleanup EXIT

git -C "$WORKSPACE_DIR" submodule status --recursive \
  | sed -E 's/^[ +-U]?([0-9a-f]{40}) ([^ ]+).*/\2 \1/' \
  | sort > "$actual"

grep -v '^#' "$LOCK_FILE" | sed '/^$/d' | sort > "$expected"

if diff -u "$expected" "$actual" >/dev/null; then
  echo "Submodule lock check passed."
  exit 0
fi

echo "Submodule lock check FAILED." >&2
echo "Expected (from deps/submodule-lock.txt) vs actual submodule refs differ:" >&2

diff -u "$expected" "$actual" || true

echo >&2
echo "To update the lock file intentionally, run:" >&2
echo "  ./scripts/git/update_submodule_lock.sh" >&2
exit 1
