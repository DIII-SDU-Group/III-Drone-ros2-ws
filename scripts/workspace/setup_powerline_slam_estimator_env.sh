#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
NAME
  setup_powerline_slam_estimator_env.sh - build the powerline SLAM estimator's
  dependencies for this workspace's Python and ROS distribution

SYNOPSIS
  scripts/workspace/setup_powerline_slam_estimator_env.sh --powerline-root PATH
      [--prefix PATH] [--jobs N] [--pylon-mask-checkpoint PATH] [--skip-tests]
      [--v13-runtime PATH]

DESCRIPTION
  Builds, inside the devcontainer (Python 3.12, ROS Jazzy), what the estimator
  in a powerline_slam checkout needs and the workspace does not provide:

  1. GTSAM 4.2.1 with powerline_slam's fixed-lag modifications, its Python
     bindings and the gtsam_unstable fixed-lag smoother bindings, built in
     <prefix>/gtsam and used in place (nothing is installed system-wide). The
     modifications are the five vendored source files of the checkout's
     powerline_perception/build/gtsam-4.2/source (the fixed-lag backport plus
     the covariance-capture API the estimator requires), each verified against
     that tree's powerline-fixed-lag-artifacts.sha256 before it replaces the
     upstream file;
  2. a virtual environment <prefix>/venv layered on the ROS Python packages
     (--system-site-packages) with the estimator's pip requirements and
     CPU-only torch. The image has no ensurepip, so the venv is created
     without pip and populated by the system pip; user-site packages are
     excluded (PYTHONNOUSERSITE=1) at setup and at run time;
  3. the estimator's native modules (_slam_native, _bootstrap_native), built
     against that GTSAM into the powerline_slam source tree next to its
     existing Python 3.10 builds (*.so files are ignored by its Git). They
     subclass GTSAM's Python types, which pybind11 only allows between modules
     built with the same pybind11 internals, so they use the pybind11 bundled
     with GTSAM's wrapper (2.10), not the system one;
  4. <prefix>/env.sh to source before running the estimator, and an import
     check of the bindings, the fixed-lag capture methods and the native
     modules;
  5. unless --skip-tests, the estimator's test suite (powerline_perception/tests);
  6. if the v13-derived runtime exists (--v13-runtime, default
     <prefix>/v13_py312_runtime, materialized on the host by powerline_slam's
     corridor_simulation/tools/iii_r1_materialize_runtime.py), a verification
     of its Python bytes against its manifest, its native modules built for
     this Python into the runtime, and NATIVE_BUILD.json recording them. This
     is the runtime of the powerline_slam perception node
     (src/iii_drone_powerline_slam).

  Network is needed only on the first run (GTSAM source clone, pip). A
  rerun in the network-disconnected devcontainer reuses both.

  --pylon-mask-checkpoint copies the pylon mask network weights into
  <prefix>/models and records their SHA-256. The default prefix is the
  workspace's Git-ignored .cache/powerline_slam_estimator.

  The powerline_slam checkout must be readable (and its native module
  directories writable) inside the container, e.g. through a bind mount.
EOF
}

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
powerline_root=""
prefix="${workspace_root}/.cache/powerline_slam_estimator"
jobs="$(nproc)"
checkpoint=""
run_tests=1
v13_runtime=""
while (($# > 0)); do
  case "$1" in
    --powerline-root) powerline_root="$2"; shift ;;
    --prefix) prefix="$2"; shift ;;
    --jobs) jobs="$2"; shift ;;
    --pylon-mask-checkpoint) checkpoint="$2"; shift ;;
    --skip-tests) run_tests=0 ;;
    --v13-runtime) v13_runtime="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done
if [[ -z "${powerline_root}" || ! -d "${powerline_root}/powerline_perception/src/powerline_perception" ]]; then
  echo "--powerline-root must name a powerline_slam checkout" >&2
  exit 64
fi
perception="$(cd "${powerline_root}/powerline_perception" && pwd)"
vendored_gtsam="${perception}/build/gtsam-4.2"
vendored_manifest="${vendored_gtsam}/powerline-fixed-lag-artifacts.sha256"
modified_gtsam_files=(
  gtsam/nonlinear/ISAM2.cpp
  gtsam_unstable/nonlinear/IncrementalFixedLagSmoother.h
  gtsam_unstable/nonlinear/IncrementalFixedLagSmoother.cpp
  gtsam_unstable/gtsam_unstable.i
  python/gtsam_unstable/specializations/gtsam_unstable.h
)
gtsam_source="${prefix}/gtsam/source"
gtsam_build="${prefix}/gtsam/build"
native_build="${prefix}/native-build"
v13_runtime="${v13_runtime:-${prefix}/v13_py312_runtime}"
venv="${prefix}/venv"
mkdir -p "${prefix}"

set +u
source /opt/ros/jazzy/setup.bash
set -u

echo "== GTSAM 4.2.1 source with the powerline_slam fixed-lag modifications"
if [[ ! -d "${gtsam_source}/.git" ]]; then
  git clone --depth 1 --branch 4.2.1 https://github.com/borglab/gtsam.git "${gtsam_source}"
fi
if [[ "$(git -C "${gtsam_source}" describe --tags --exact-match 2>/dev/null)" != "4.2.1" ]]; then
  echo "${gtsam_source} is not GTSAM tag 4.2.1" >&2
  exit 1
fi
for relative in "${modified_gtsam_files[@]}"; do
  # The manifest names each file by its absolute path in the powerline checkout.
  expected="$(awk -v suffix="/source/${relative}" \
    'length($2) >= length(suffix) && substr($2, length($2) - length(suffix) + 1) == suffix {print $1}' \
    "${vendored_manifest}")"
  actual="$(sha256sum "${vendored_gtsam}/source/${relative}" | cut -d' ' -f1)"
  if [[ -z "${expected}" || "${expected}" != "${actual}" ]]; then
    echo "vendored ${relative} does not match ${vendored_manifest}" >&2
    exit 1
  fi
  install -m 0644 "${vendored_gtsam}/source/${relative}" "${gtsam_source}/${relative}"
done

echo "== Python environment"
export PYTHONNOUSERSITE=1
if [[ ! -x "${venv}/bin/python" ]]; then
  python3 -m venv --system-site-packages --without-pip "${venv}"
fi
python_bin="${venv}/bin/python"
# ROS provides rosbag2_py, rclpy and the message packages; numpy, scipy,
# yaml and opencv come from the ROS/Ubuntu Python packages.
if "${python_bin}" -c "import pyparsing, rosbags, pytest, torch" 2>/dev/null; then
  echo "pip packages present; skipping pip"
else
  "${python_bin}" -m pip install --quiet "pyparsing>=3" "rosbags>=0.9" "pytest>=7"
  "${python_bin}" -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
fi

echo "== GTSAM build (core, unstable, Python bindings)"
cmake -S "${gtsam_source}" -B "${gtsam_build}" \
  -DGTSAM_BUILD_PYTHON=ON \
  -DGTSAM_BUILD_UNSTABLE=ON \
  -DGTSAM_BUILD_TESTS=OFF \
  -DGTSAM_BUILD_EXAMPLES_ALWAYS=OFF \
  -DGTSAM_BUILD_DOCS=OFF \
  -DGTSAM_BUILD_WITH_MARCH_NATIVE=OFF \
  -DGTSAM_USE_SYSTEM_EIGEN=ON \
  -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${prefix}/gtsam/install" \
  -DPYTHON_EXECUTABLE="${python_bin}" \
  -DPython3_EXECUTABLE="${python_bin}" >/dev/null
# CppUnitLite is part of the install set the native build finds GTSAM through.
cmake --build "${gtsam_build}" --target gtsam gtsam_unstable gtsam_py gtsam_unstable_py CppUnitLite --parallel "${jobs}"
cmake --install "${gtsam_build}" >/dev/null
cmake -S "${gtsam_source}/wrap/pybind11" -B "${prefix}/pybind11/build" \
  -DPYBIND11_TEST=OFF -DCMAKE_INSTALL_PREFIX="${prefix}/pybind11/install" >/dev/null
cmake --install "${prefix}/pybind11/build" >/dev/null

cat > "${prefix}/env.sh" <<ENV
# Source after /opt/ros/jazzy/setup.bash to run the powerline SLAM estimator.
export VIRTUAL_ENV="${venv}"
export PYTHONNOUSERSITE=1
export PATH="${venv}/bin:\${PATH}"
export PYTHONPATH="${gtsam_build}/python:${perception}/src:${perception}\${PYTHONPATH:+:\${PYTHONPATH}}"
export LD_LIBRARY_PATH="${gtsam_build}/gtsam:${gtsam_build}/gtsam_unstable:${gtsam_build}/gtsam/3rdparty/metis/libmetis\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export POWERLINE_SLAM_CHECKOUT="${powerline_root}"
export POWERLINE_SLAM_V13_RUNTIME="${v13_runtime}"
ENV

echo "== Estimator native modules"
cmake -E remove_directory "${native_build}"
cmake -S "${perception}/native" -B "${native_build}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="${prefix}/gtsam/install" \
  -Dpybind11_DIR="${prefix}/pybind11/install/share/cmake/pybind11" \
  -DPython3_EXECUTABLE="${python_bin}" \
  -DPOWERLINE_NATIVE_OUTPUT_DIR="${perception}/src/powerline_perception/slam" \
  -DPOWERLINE_BOOTSTRAP_NATIVE_OUTPUT_DIR="${perception}/src/powerline_reference_manager" >/dev/null
cmake --build "${native_build}" --parallel "${jobs}"

if [[ -n "${checkpoint}" ]]; then
  echo "== Pylon mask checkpoint"
  mkdir -p "${prefix}/models"
  cp "${checkpoint}" "${prefix}/models/"
  (cd "${prefix}/models" && sha256sum "$(basename "${checkpoint}")" > "$(basename "${checkpoint}").sha256")
fi

echo "== Import check"
set +u
source "${prefix}/env.sh"
set -u
python - <<'PY'
import importlib
import sys

import gtsam
import gtsam_unstable
from powerline_perception.slam import _slam_native
from powerline_reference_manager import _bootstrap_native  # noqa: F401
import torch

smoother = gtsam_unstable.IncrementalFixedLagSmoother
required = (
    "setPreMarginalizationCovarianceCaptureKeys",
    "hasPreMarginalizationCovarianceCapture",
    "getPreMarginalizationCovarianceCaptureKeys",
    "getPreMarginalizationCovarianceCapture",
    "getPostMarginalizationActiveKeys",
)
missing = [name for name in required if not hasattr(smoother, name)]
if missing:
    raise SystemExit("gtsam_unstable lacks the fixed-lag capture methods: " + ", ".join(missing))
print(f"python {sys.version.split()[0]}; gtsam {gtsam.__file__}")
print(f"_slam_native {_slam_native.__file__} ({_slam_native.implementation})")
print(f"torch {torch.__version__}")
PY

if [[ -f "${v13_runtime}/RUNTIME_MANIFEST.json" ]]; then
  echo "== v13-derived runtime ${v13_runtime}"
  "${python_bin}" "${powerline_root}/corridor_simulation/tools/iii_r1_materialize_runtime.py" verify --out "${v13_runtime}"
  v13_native_build="${prefix}/v13-native-build"
  cmake -E remove_directory "${v13_native_build}"
  cmake -S "${v13_runtime}/powerline_perception/native" -B "${v13_native_build}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="${prefix}/gtsam/install" \
    -Dpybind11_DIR="${prefix}/pybind11/install/share/cmake/pybind11" \
    -DPython3_EXECUTABLE="${python_bin}" \
    -DPOWERLINE_NATIVE_OUTPUT_DIR="${v13_runtime}/powerline_perception/src/powerline_perception/slam" \
    -DPOWERLINE_BOOTSTRAP_NATIVE_OUTPUT_DIR="${v13_runtime}/powerline_perception/src/powerline_reference_manager" >/dev/null
  cmake --build "${v13_native_build}" --parallel "${jobs}"
  "${python_bin}" - "${v13_runtime}" "${gtsam_build}" <<'PY'
import hashlib
import json
import platform
import sys
from pathlib import Path

runtime, gtsam_build = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(runtime / "powerline_perception/src"))
import gtsam  # noqa: E402,F401  (registers the GTSAM base types the native factors subclass)
import gtsam_unstable  # noqa: E402,F401
from powerline_perception.slam import _slam_native  # noqa: E402
from powerline_reference_manager import _bootstrap_native  # noqa: E402


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


for module in (_slam_native, _bootstrap_native):
    if not Path(module.__file__).resolve().is_relative_to(runtime.resolve()):
        raise SystemExit(f"{module.__name__} did not load from the v13-derived runtime: {module.__file__}")
gtsam_modules = sorted(gtsam_build.glob("python/gtsam*/gtsam*.cpython-*.so"))
record = {
    "schema_version": 1,
    "artifact_type": "III_R1_V13_DERIVED_NATIVE_BUILD",
    "python": platform.python_version(),
    "runtime_manifest_sha256": digest(runtime / "RUNTIME_MANIFEST.json"),
    "native_modules": {module.__name__: {"path": str(Path(module.__file__).relative_to(runtime)),
                                         "sha256": digest(module.__file__)}
                       for module in (_slam_native, _bootstrap_native)},
    "slam_native_implementation": getattr(_slam_native, "implementation", None),
    "gtsam_python_modules": {str(path.relative_to(gtsam_build)): digest(path) for path in gtsam_modules},
}
(runtime / "NATIVE_BUILD.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
print(json.dumps(record["native_modules"]))
PY
fi

if ((run_tests)); then
  echo "== Estimator tests"
  # ROS's launch_testing pytest plugins import every test module during
  # collection and abort the session on the first module-level error.
  (cd "${powerline_root}" && python -m pytest -q -p no:launch_testing -p no:launch_ros powerline_perception/tests)
fi
echo "Powerline SLAM estimator environment ready: source ${prefix}/env.sh"
