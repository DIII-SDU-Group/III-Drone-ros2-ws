#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  install_remote.bash - retired remote bootstrap entry point

SYNOPSIS
  scripts/remote/install_remote.bash

DESCRIPTION
  This script is retired and never mutates the workstation. Install the native
  ground-computer stack from the checkout with
  `python3 scripts/install_gc.py --profile {dev,deploy}`.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

echo "III_REMOTE_BOOTSTRAP_RETIRED: this entry point performs no changes." >&2
echo "Next: python3 scripts/install_gc.py --help" >&2
echo "Development: source setup/setup_dev.bash" >&2
exit 64
