#!/usr/bin/env bash
set -euo pipefail

readonly workspace="${III_WORKSPACE_ROOT:-/home/iii/ws}"
if [[ ! -r "${workspace}/setup/setup_real.bash" ]]; then
  echo "III real setup is unavailable: ${workspace}/setup/setup_real.bash" >&2
  exit 30
fi
export III_WORKSPACE_INSTALL="${workspace}/install"
source "${workspace}/setup/setup_real.bash"
cd "${workspace}"
exec "$@"
