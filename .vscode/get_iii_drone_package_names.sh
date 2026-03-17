#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  get_iii_drone_package_names.sh - list ROS package names for workspace-owned III packages

SYNOPSIS
  .vscode/get_iii_drone_package_names.sh

DESCRIPTION
  Scans `src/**/package.xml` and prints package names whose ROS package name
  starts with `iii`.

  Output format:
  - one package name per line
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

workspace_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
packages_names=""

for file in $(find "$workspace_dir/src" -name package.xml); do
    package_name=$(grep -oPm1 "(?<=<name>)[^<]+" "$file")
    if [[ $package_name == iii* ]]; then
        packages_names="$packages_names $package_name"
    fi
done

echo "$packages_names" | tr " " "\n" | sed '/^$/d'
