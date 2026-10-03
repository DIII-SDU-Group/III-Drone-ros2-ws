#!/usr/bin/env bash
set -euo pipefail

# Fast research deployment build for the Raspberry Pi. The target is built on
# the workstation in the pinned ARM64 cross-builder; only the resulting
# install tree is copied to the Pi. This remains a plain developer workflow:
# no release bundle, signing step, receiver, or Pi-side compiler is involved.

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace="$(cd -- "${script_dir}/../.." && pwd)"
image="${III_CROSS_BUILDER_IMAGE:-iii-arm64-cross-builder:p1}"
cache_root="${III_CROSS_CACHE_DIR:-${XDG_CACHE_HOME:-${TMPDIR:-/tmp}}/iii/arm64-cross}"
output_dir="${III_CROSS_OUTPUT_DIR:-${workspace}/.cache/iii/arm64-cross}"
parallel_workers="${III_CROSS_PARALLEL_WORKERS:-}"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/build/cross_compile_arm64.sh [options]

Build the Pi ARM64 runtime on the workstation and write a deployable install
tree. Options:
  --workspace PATH       workspace to build (default: repository root)
  --output-dir PATH      output containing build/, install/, and log/
  --cache-dir PATH       persistent ccache directory
  --image IMAGE          cross-builder image (default: iii-arm64-cross-builder:p1)
  --parallel-workers N   colcon worker count (default: colcon default)
EOF
  exit 64
}

while (($#)); do
  case "$1" in
    --workspace)
      (($# >= 2)) || usage
      workspace="$(cd -- "$2" && pwd)"
      shift 2
      ;;
    --output-dir)
      (($# >= 2)) || usage
      output_dir="$(mkdir -p -- "$2" && cd -- "$2" && pwd)"
      shift 2
      ;;
    --cache-dir)
      (($# >= 2)) || usage
      cache_root="$(mkdir -p -- "$2" && cd -- "$2" && pwd)"
      shift 2
      ;;
    --image)
      (($# >= 2)) || usage
      image="$2"
      shift 2
      ;;
    --parallel-workers)
      (($# >= 2)) || usage
      parallel_workers="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "unknown option: $1" >&2
      usage
      ;;
  esac
done

[[ -d "${workspace}/src" && -d "${workspace}/cc_ws" ]] || {
  echo "workspace is missing src/ or cc_ws/: ${workspace}" >&2
  exit 2
}
command -v docker >/dev/null || {
  echo "docker is required for the ARM64 cross-build" >&2
  exit 30
}
docker image inspect "${image}" >/dev/null 2>&1 || {
  echo "cross-builder image is unavailable: ${image}" >&2
  echo "build Dockerfile.cc with the repository's normal Docker workflow first" >&2
  exit 30
}

mkdir -p -- "${output_dir}/build" "${output_dir}/install" "${output_dir}/log" "${cache_root}/ccache"

colcon_args=(
  --log-base /out/log
  build
  --base-paths src
  --packages-skip iii_drone_simulation micro_ros_agent microxrcedds_agent micro_ros_msgs btcpp_ros2_samples
  --packages-skip-regex '^example_.*$'
  --build-base /out/build
  --install-base /home/iii/ws/install
  --merge-install
  --cmake-args
  -DCMAKE_TOOLCHAIN_FILE=/opt/iii/arm64-toolchain.cmake
  '-DCMAKE_PREFIX_PATH=/opt/iii/sysroot/opt/ros/jazzy;/opt/iii/sysroot/usr'
  -DBUILD_TESTING=OFF
  -DCMAKE_BUILD_TYPE=Debug
  -DBTCPP_GROOT_INTERFACE=OFF
)
if [[ -n "${parallel_workers}" ]]; then
  colcon_args+=(--parallel-workers "${parallel_workers}")
fi

echo "Cross-building ARM64 runtime with ${image}" >&2
docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e CCACHE_DIR=/cache/ccache \
  -e III_TARGET_INSTALL=/home/iii/ws/install \
  -e III_CROSS_PARALLEL_WORKERS="${parallel_workers:-8}" \
  --entrypoint /entrypoint.sh \
  -v "${workspace}:/home/iii/ws" \
  -v "${workspace}/cc_ws/run-target-emulated.sh:/usr/local/bin/iii-run-target-emulated:ro" \
  -v "${output_dir}/install:/home/iii/ws/install" \
  -v "${output_dir}:/out" \
  -v "${cache_root}:/cache" \
  "${image}" \
  bash -lc '
    set -e
    source /opt/iii/sysroot/opt/ros/jazzy/setup.bash
    cd /home/iii/ws
    colcon "$@"
    # Micro XRCE-DDS Agent is not a ROS package (the upstream repository has no
    # package.xml), so build it beside the colcon graph with the target ARM64
    # pinned Fast CDR/Fast DDS libraries. Logging, discovery, CAN, and P2P are
    # not needed by the Pi runtime; disabling them also avoids pulling a
    # network-fetched spdlog superbuild into an offline developer build.
    agent_build_dir=/out/build/uxrce-agent
    cmake \
      -S /home/iii/ws/src/Micro-XRCE-DDS-Agent \
      -B "$agent_build_dir" \
      -DCMAKE_TOOLCHAIN_FILE=/opt/iii/arm64-toolchain.cmake \
      -DCMAKE_PREFIX_PATH="/opt/iii/sysroot/opt/ros/jazzy;/opt/iii/sysroot/usr" \
      -DCMAKE_INSTALL_PREFIX=/home/iii/ws/install \
      -DUAGENT_SUPERBUILD=OFF \
      -DUAGENT_USE_SYSTEM_FASTCDR=ON \
      -DUAGENT_USE_SYSTEM_FASTDDS=ON \
      -DUAGENT_LOGGER_PROFILE=OFF \
      -DUAGENT_FAST_PROFILE=ON \
      -DUAGENT_CED_PROFILE=OFF \
      -DUAGENT_DISCOVERY_PROFILE=OFF \
      -DUAGENT_P2P_PROFILE=OFF \
      -DUAGENT_SOCKETCAN_PROFILE=OFF \
      -DUAGENT_BUILD_TESTS=OFF \
      -DUAGENT_BUILD_EXECUTABLE=ON \
      -DBUILD_SHARED_LIBS=ON
    cmake --build "$agent_build_dir" --parallel "${III_CROSS_PARALLEL_WORKERS:-8}"
    cmake --install "$agent_build_dir"
  ' \
  bash "${colcon_args[@]}"

# Colcon records the builder's underlay in shell and ament-index metadata.
# The target has the same ROS distro at /opt/ros/jazzy, so make the staged
# install relocatable before it is copied to the Pi. Binary objects are not
# touched; only generated text metadata is rewritten.
while IFS= read -r -d '' metadata; do
  sed -i \
    -e 's#/opt/iii/sysroot/opt/ros/jazzy#/opt/ros/jazzy#g' \
    -e 's#/out/install#/home/iii/ws/install#g' \
    "${metadata}"
done < <(
  find "${output_dir}/install" -type f \( \
    -name '*.bash' -o -name '*.sh' -o -name '*.zsh' -o -name '*.dsv' \
    -o -name '*.ps1' -o -path '*/ament_index/resource_index/parent_prefix_path/*' \
  \) -print0
)

[[ -r "${output_dir}/install/setup.bash" ]] || {
  echo "cross-build completed without install/setup.bash: ${output_dir}" >&2
  exit 31
}
[[ -x "${output_dir}/install/bin/MicroXRCEAgent" ]] || {
  echo "cross-build completed without install/bin/MicroXRCEAgent: ${output_dir}" >&2
  exit 32
}

echo "ARM64 install ready: ${output_dir}/install" >&2
