#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  get_package_executable_names.sh - list executable names exported by a ROS package

SYNOPSIS
  .vscode/get_package_executable_names.sh <package-name>

DESCRIPTION
  Wrapper around `ros2 pkg executables` that strips the repeated package name
  column and prints only executable names.
USAGE
}

if [[ $# -ne 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit $(( $# == 1 ? 0 : 1 ))
fi

package_name=$1

executables=$(ros2 pkg executables $package_name)

# Remove all occurences of "$package_name " from the list of executables
executables=$(echo "$executables" | sed "s/$package_name //g")

echo "$executables"
