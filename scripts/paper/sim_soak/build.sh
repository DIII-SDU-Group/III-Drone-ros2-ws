#!/usr/bin/env bash
# Build the paper's simulation tables and figures: bag extraction inside the
# devcontainer (needs ROS), rendering on the host (the manuscript's Helvetica
# style uses Nimbus Sans, which the devcontainer does not have).
set -euo pipefail
W=$(cd "$(dirname "$0")/../../.." && pwd)
C=$(docker ps --filter "label=devcontainer.local_folder=$W" --format '{{.ID}}' | head -n1)
docker exec --user iii "$C" bash -lc 'source /opt/ros/jazzy/setup.bash; source /home/iii/ws/install/setup.bash; cd /home/iii/ws && python3 scripts/paper/sim_soak/extract_all.py' \
  2>&1 | grep -v -E "read order|falling back|no message index"
cd "$W" && python3 scripts/paper/sim_soak/make_all.py
