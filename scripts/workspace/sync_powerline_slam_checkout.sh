#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  sync_powerline_slam_checkout.sh - check out the pinned powerline_slam commit and materialize its
  v13-derived runtime for this workspace

SYNOPSIS
  scripts/workspace/sync_powerline_slam_checkout.sh --powerline-source PATH [--remote URL]

DESCRIPTION
  Runs on the host (network and the powerline_slam source checkout available), before
  scripts/workspace/setup_powerline_slam_estimator_env.sh runs in the devcontainer:

  1. clones or fetches deps/powerline-slam.json's repository into its checkout directory and
     checks out the pinned commit (detached, clean);
  2. stages the git-ignored GTSAM fixed-lag sources the estimator build needs
     (powerline_perception/build/gtsam-4.2: the five vendored files and their manifest)
     from --powerline-source;
  3. materializes the v13-derived Python runtime with the checkout's
     corridor_simulation/tools/iii_r1_materialize_runtime.py, which copies the bound protected
     runtime of --powerline-source byte for byte (verified against its binding) and the native
     sources; the setup script then builds its Python 3.12 native modules.

  --powerline-source is a powerline_slam checkout that holds the bound candidate-v13 runtime
  directory and the GTSAM build sources (git-ignored, so absent from a fresh clone).
USAGE
}

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
lock="${workspace_root}/deps/powerline-slam.json"
source_root=""
remote=""
while (($# > 0)); do
  case "$1" in
    --powerline-source) source_root="$2"; shift ;;
    --remote) remote="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done
if [[ -z "${source_root}" || ! -d "${source_root}/corridor_simulation/tools" ]]; then
  echo "--powerline-source must name a powerline_slam checkout" >&2
  exit 64
fi
read -r repository commit checkout environment runtime < <(python3 - "${lock}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["repository"], d["commit"], d["checkout"], d["estimator_environment"], d["v13_derived_runtime"]["directory"])
PY
)
repository="${remote:-${repository}}"
checkout="${workspace_root}/${checkout}"
runtime="${workspace_root}/${runtime}"

echo "== powerline_slam ${commit}"
if [[ ! -d "${checkout}/.git" ]]; then
  git clone --quiet --no-checkout "${repository}" "${checkout}"
fi
git -C "${checkout}" fetch --quiet --tags "${repository}" "${commit}" 2>/dev/null || git -C "${checkout}" fetch --quiet --tags "${repository}"
git -C "${checkout}" checkout --quiet --detach "${commit}"
if [[ -n "$(git -C "${checkout}" status --porcelain)" ]]; then
  echo "${checkout} is not clean at ${commit}" >&2
  exit 1
fi

echo "== GTSAM fixed-lag sources"
vendored="powerline_perception/build/gtsam-4.2"
for relative in powerline-fixed-lag-artifacts.sha256 source/gtsam/nonlinear/ISAM2.cpp \
    source/gtsam_unstable/nonlinear/IncrementalFixedLagSmoother.h \
    source/gtsam_unstable/nonlinear/IncrementalFixedLagSmoother.cpp \
    source/gtsam_unstable/gtsam_unstable.i source/python/gtsam_unstable/specializations/gtsam_unstable.h; do
  install -D -m 0644 "${source_root}/${vendored}/${relative}" "${checkout}/${vendored}/${relative}"
done

echo "== v13-derived runtime"
if [[ -f "${runtime}/RUNTIME_MANIFEST.json" ]]; then
  python3 "${source_root}/corridor_simulation/tools/iii_r1_materialize_runtime.py" verify --out "${runtime}"
else
  python3 "${source_root}/corridor_simulation/tools/iii_r1_materialize_runtime.py" materialize --out "${runtime}"
fi
echo "Next, in the devcontainer: scripts/workspace/setup_powerline_slam_estimator_env.sh --powerline-root ${checkout#${workspace_root}/}"
