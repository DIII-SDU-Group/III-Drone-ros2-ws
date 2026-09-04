#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  run_iii_test_suite.sh - run the workspace-owned III package test suite

SYNOPSIS
  scripts/workspace/run_iii_test_suite.sh

DESCRIPTION
  Runs the curated III-only test suite for this workspace:
  - ROS package tests for the selected III packages via `colcon test`
  - generated TypeScript contract freshness check
  - GUI v2 frontend lint, typecheck, unit tests, and production build
  - top-level integration pytest suite under `tests/`
  - CLI pytest suite under `tools/III-Drone-CLI/test`

  This script intentionally excludes third-party package tests.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "${workspace_root}/.." && pwd)"
cd "${workspace_root}"

run_frontend_tests_with_npm() {
  npm --prefix src/III-Drone-GC/frontend ci --no-audit --no-fund
  npm --prefix src/III-Drone-GC/frontend run contracts:check
  npm --prefix src/III-Drone-GC/frontend run lint
  npm --prefix src/III-Drone-GC/frontend run typecheck
  npm --prefix src/III-Drone-GC/frontend test
  npm --prefix src/III-Drone-GC/frontend run build
}

if [[ -n "${ROS_DISTRO:-}" ]] && [[ -f "/opt/ros/${ROS_DISTRO}/setup.sh" ]]; then
  set +u
  # The selected ROS distribution is resolved by the guarded path above.
  # shellcheck disable=SC1090
  . "/opt/ros/${ROS_DISTRO}/setup.sh"
  set -u
elif [[ -f "/opt/ros/jazzy/setup.sh" ]]; then
  set +u
  . "/opt/ros/jazzy/setup.sh"
  set -u
fi

packages=(
  iii_drone_configuration
  iii_drone_contracts
  iii_drone_core
  iii_drone_interfaces
  iii_drone_mission
  iii_drone_runtime
  iii_drone_simulation
  iii_drone_supervision
  iii_drone_gc
)

has_stale_colcon_prefix=0
if [[ -f install/setup.sh ]]; then
  install_prefix="$(sed -n 's/^_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX=//p' install/setup.sh | head -n 1 | tr -d '"')"
  if [[ -n "$install_prefix" && "$install_prefix" != "${workspace_root}/install" ]]; then
    has_stale_colcon_prefix=1
  fi
fi

while IFS= read -r cache_file; do
  cache_source_dir="$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' "$cache_file")"
  if [[ -n "$cache_source_dir" && "$cache_source_dir" != "${workspace_root}"/* ]]; then
    has_stale_colcon_prefix=1
    break
  fi
done < <(find build -name CMakeCache.txt -print 2>/dev/null)

if (( has_stale_colcon_prefix == 1 )); then
  echo "Detected stale colcon build/install metadata from a different workspace path."
  echo "Cleaning build/, install/, and log/ before rebuilding test targets."
  rm -rf build install log
fi

colcon build \
  --base-paths src \
  --packages-up-to \
  "${packages[@]}"

colcon test \
  --base-paths src \
  --packages-select \
  "${packages[@]}"

colcon test-result --verbose

set +u
. install/setup.sh
set -u

python3 src/III-Drone-Contracts/scripts/generate_typescript.py \
  --output src/III-Drone-GC/frontend/src/generated/contracts.ts \
  --check

if command -v npm >/dev/null 2>&1; then
  run_frontend_tests_with_npm
elif command -v docker >/dev/null 2>&1; then
  docker run --rm \
    -v "${workspace_root}/src/III-Drone-GC/frontend:/app:ro" \
    -v iii_gc_frontend_npm_cache:/root/.npm \
    --tmpfs /app/node_modules:rw,exec,size=1g \
    --tmpfs /app/dist:rw \
    -w /app \
    node:22-alpine \
    sh -lc 'npm ci --no-audit --no-fund && npm run lint && npm run typecheck && npm test && npm run build'
else
  node_version="${III_NODE_VERSION:-22.21.1}"
  case "$(uname -m)" in
    x86_64|amd64) node_arch="x64" ;;
    aarch64|arm64) node_arch="arm64" ;;
    *)
      echo "Unsupported architecture for automatic Node.js download: $(uname -m)" >&2
      exit 1
      ;;
  esac
  node_dist="node-v${node_version}-linux-${node_arch}"
  node_dir="${workspace_root}/.cache/${node_dist}"
  if [[ ! -x "${node_dir}/bin/npm" ]]; then
    mkdir -p "${workspace_root}/.cache"
    curl -fsSL "https://nodejs.org/dist/v${node_version}/${node_dist}.tar.xz" \
      | tar -xJ -C "${workspace_root}/.cache"
  fi
  export PATH="${node_dir}/bin:${PATH}"
  run_frontend_tests_with_npm
fi

python3 -m pytest tests
PYTHONPATH="${workspace_root}/deployment/src:${workspace_root}/tools/III-Drone-CLI${PYTHONPATH:+:${PYTHONPATH}}" \
  python3 -m pytest tools/III-Drone-CLI/test
