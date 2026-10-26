#!/usr/bin/env bash
set -euo pipefail

readonly sysroot="${III_SYSROOT:-/opt/iii/sysroot}"
readonly target_install="${III_TARGET_INSTALL:-/opt/iii/current/install}"

target_library_paths=(
  # A contract exporter must load the just-linked package library, not the
  # previous install tree that is still present during incremental builds.
  "$(dirname -- "$1")"
  "${sysroot}/opt/ros/jazzy/lib"
  "${sysroot}/usr/lib/aarch64-linux-gnu"
  "${sysroot}/usr/lib/aarch64-linux-gnu/blas"
  "${sysroot}/usr/lib/aarch64-linux-gnu/lapack"
  "${sysroot}/lib/aarch64-linux-gnu"
)
[[ -d "${target_install}/lib" ]] && target_library_paths+=("${target_install}/lib")
shopt -s nullglob
for library_path in "${target_install}"/*/lib; do
  target_library_paths+=("${library_path}")
done
shopt -u nullglob

target_ld_library_path="$(IFS=:; printf '%s' "${target_library_paths[*]}")"
exec /usr/bin/qemu-aarch64-static \
  -L "${sysroot}" \
  -E "LD_LIBRARY_PATH=${target_ld_library_path}" \
  "$@"
