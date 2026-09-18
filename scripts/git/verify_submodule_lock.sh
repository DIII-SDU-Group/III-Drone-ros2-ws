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
  scripts/git/verify_submodule_lock.sh [--fresh]

DESCRIPTION
  Verifies that the recursive git submodule state in the current workspace
  exactly matches `deps/submodule-lock.txt`.

  This is the local equivalent of the dependency-governance CI gate.

  With --fresh, also creates a detached worktree at the current commit,
  initializes every recursive submodule from its configured remotes, and
  verifies the lock there. This catches gitlinks that resolve only from local
  objects and cannot be reproduced by another developer or a clean CI worker.
USAGE
}

fresh=0
case "${1:-}" in
  "") ;;
  --fresh) fresh=1 ;;
  -h|--help) usage; exit 0 ;;
  *) echo "error: unknown argument: $1" >&2; usage >&2; exit 1 ;;
esac

if [[ ! -f "$LOCK_FILE" ]]; then
  echo "error: lock file not found: $LOCK_FILE" >&2
  exit 1
fi

if (( fresh == 1 )); then
  fresh_worktree="$(mktemp -d "${TMPDIR:-/tmp}/iii-submodule-lock-fresh.XXXXXX")"
  rmdir "$fresh_worktree"
  cleanup_fresh() {
    git -C "$WORKSPACE_DIR" worktree remove --force "$fresh_worktree" >/dev/null 2>&1 || true
  }
  trap cleanup_fresh EXIT

  git -C "$WORKSPACE_DIR" worktree add --detach "$fresh_worktree" HEAD >/dev/null
  git -C "$fresh_worktree" submodule update --init --recursive
  "$fresh_worktree/scripts/git/verify_submodule_lock.sh"

  if [[ -n "$(git -C "$fresh_worktree" status --porcelain)" ]]; then
    echo "Fresh submodule checkout is dirty:" >&2
    git -C "$fresh_worktree" status --short >&2
    exit 1
  fi

  echo "Fresh recursive submodule checkout is reproducible."
  exit 0
fi

actual="$(mktemp)"
expected="$(mktemp)"
cleanup() {
  rm -f "$actual" "$expected"
}
trap cleanup EXIT

# Verify the checked-out submodule commits rather than the superproject index.
# This prevents a locally advanced but unstaged gitlink from making a stale lock
# appear valid.
git -C "$WORKSPACE_DIR" submodule foreach --recursive --quiet \
  'printf "%s %s\n" "$displaypath" "$(git rev-parse HEAD)"' \
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
