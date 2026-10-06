#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
NAME
  setup_powerline_slam_cuda_torch.sh - add a CUDA build of torch beside the
  powerline SLAM estimator environment

SYNOPSIS
  scripts/workspace/setup_powerline_slam_cuda_torch.sh download [--wheels DIR]
  scripts/workspace/setup_powerline_slam_cuda_torch.sh install [--wheels DIR]
      [--prefix PATH]

DESCRIPTION
  The estimator environment (setup_powerline_slam_estimator_env.sh) holds the
  CPU build of torch. It stays the reference and the fallback. This script adds
  the CUDA 12.6 build of the same torch release as a separate directory,
  <prefix>/torch-cuda, that only a process which puts it first on its Python
  path imports (the powerline_slam node's GPU mask worker). Nothing in the
  estimator environment is replaced.

  The wheel set is deps/powerline-slam-cuda-torch.txt: torch and the NVIDIA
  runtime libraries it loads, each pinned by version and SHA-256, installed
  without dependency resolution.

  download   run where there is network (the host): fetches exactly the pinned
             wheels for CPython 3.12 / x86-64 into --wheels and verifies their
             hashes.
  install    run inside the devcontainer: verifies the hashes again, unpacks the
             wheels into <prefix>/torch-cuda, imports the result and writes
             <prefix>/torch-cuda/MANIFEST.json (versions, wheel hashes, whether
             CUDA is usable and on which GPU). It does not need network.

  The default prefix is the workspace's Git-ignored
  .cache/powerline_slam_estimator; the default wheel directory is
  <prefix>/torch-cuda-wheels.
EOF
}

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
lock="${workspace_root}/deps/powerline-slam-cuda-torch.txt"
prefix="${workspace_root}/.cache/powerline_slam_estimator"
wheels=""
action="${1:-}"
[[ $# -gt 0 ]] && shift
while (($# > 0)); do
  case "$1" in
    --wheels) wheels="$2"; shift ;;
    --prefix) prefix="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done
wheels="${wheels:-${prefix}/torch-cuda-wheels}"

case "${action}" in
  download)
    mkdir -p "${wheels}"
    # Every manylinux tag an x86-64 glibc >= 2.28 CPython accepts: the NVIDIA wheels do not share one tag.
    platforms=(--platform manylinux1_x86_64 --platform manylinux2010_x86_64 --platform manylinux2014_x86_64)
    for minor in $(seq 5 28); do platforms+=(--platform "manylinux_2_${minor}_x86_64"); done
    python3 -m pip download --no-deps --require-hashes -r "${lock}" \
      --index-url https://download.pytorch.org/whl/cu126 --extra-index-url https://pypi.org/simple \
      --only-binary=:all: --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
      "${platforms[@]}" --progress-bar off -d "${wheels}"
    ;;
  install)
    python_bin="${prefix}/venv/bin/python"
    if [[ ! -x "${python_bin}" ]]; then
      echo "estimator environment ${prefix}/venv is missing; run setup_powerline_slam_estimator_env.sh first" >&2
      exit 78
    fi
    target="${prefix}/torch-cuda"
    export PYTHONNOUSERSITE=1
    rm -rf "${target}.partial"
    "${python_bin}" -m pip install --quiet --no-index --find-links "${wheels}" --no-deps --require-hashes -r "${lock}" \
      --target "${target}.partial" --no-compile
    rm -rf "${target}"
    mv "${target}.partial" "${target}"
    set +u
    source "${prefix}/env.sh"
    set -u
    PYTHONPATH="${target}:${PYTHONPATH}" "${python_bin}" - "${target}" "${lock}" "${wheels}" <<'PY'
import hashlib
import json
import platform
import re
import sys
from pathlib import Path

target, lock, wheels = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
import torch  # noqa: E402

if not Path(torch.__file__).resolve().is_relative_to(target.resolve()):
    raise SystemExit(f"torch did not load from {target}: {torch.__file__}")
pinned = dict(re.match(r"^(\S+)==(\S+) --hash=sha256:(\w+)$", line).group(1, 3)
              for line in lock.read_text().splitlines() if line and not line.startswith("#"))
by_digest = {hashlib.sha256(path.read_bytes()).hexdigest(): path.name for path in sorted(wheels.glob("*.whl"))}
record = {
    "schema": "iii.powerline-slam-cuda-torch/v1",
    "lock": {"path": "deps/powerline-slam-cuda-torch.txt", "sha256": hashlib.sha256(lock.read_bytes()).hexdigest()},
    "wheels": {name: {"sha256": digest, "file": by_digest.get(digest)} for name, digest in pinned.items()},
    "python": platform.python_version(),
    "torch": torch.__version__,
    "torch_cuda_build": torch.version.cuda,
    "cudnn": torch.backends.cudnn.version(),
    "cuda_available": torch.cuda.is_available(),
    "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    "gpu_memory_mib": torch.cuda.get_device_properties(0).total_memory // 2**20 if torch.cuda.is_available() else None,
}
(target / "MANIFEST.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps({key: record[key] for key in record if key != "wheels"}, indent=2))
if not record["cuda_available"]:
    print("warning: CUDA is not usable here; the node falls back to the CPU mask path", file=sys.stderr)
PY
    ;;
  -h|--help|"")
    usage
    [[ -n "${action}" ]] || exit 64
    ;;
  *)
    echo "Unknown action: ${action}" >&2
    usage >&2
    exit 64
    ;;
esac
