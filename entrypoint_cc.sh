#!/bin/bash
# set -e

source /arm64-sysroot/opt/ros/humble/setup.bash

# Execute the command passed to the entrypoint
exec "$@"
