#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  get_debug_pid.sh - locate a running installed III executable by package/executable name

SYNOPSIS
  .vscode/get_debug_pid.sh <package-name> <executable-name>

DESCRIPTION
  Searches running processes for an installed executable path matching:
    <workspace>/install/<package>/lib/<package>/<executable>

  This is mainly useful when attaching a debugger to a process started from the
  installed workspace layout.
USAGE
}

if [[ $# -ne 2 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit $(( $# == 2 ? 0 : 1 ))
fi

workspace_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
package_name="$1"
executable="$2"

pgrep -f "$workspace_dir/install/$package_name/lib/$package_name/$executable" || true
