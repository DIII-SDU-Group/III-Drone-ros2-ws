#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  install_remote.bash - retired remote bootstrap entry point

SYNOPSIS
  scripts/remote/install_remote.bash

DESCRIPTION
  This script is retired and never mutates the workstation. Provision a native
  ground-control host with `iii gc provision`; for checkout-local development,
  source `setup/setup_dev.bash` and use the devcontainer workflow.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

echo "III_REMOTE_BOOTSTRAP_RETIRED: this entry point performs no changes." >&2
echo "Next: iii gc provision --help" >&2
echo "Development: source setup/setup_dev.bash" >&2
exit 64
