#!/usr/bin/env bash
# Stdio bridge that launches the III-Drone MCP server inside the workspace
# devcontainer. Used by the project .mcp.json registration.
#
# The devcontainer is discovered by its devcontainer.local_folder label. When
# run from a git worktree without its own devcontainer, the main checkout's
# devcontainer is used instead (it then serves the main checkout's /home/iii/ws).
#
# Usage: iii_drone_mcp_bridge.sh [--artifact-dir DIR]
set -euo pipefail

artifact_dir="/tmp/iii_drone/claude_mcp"
if [[ "${1:-}" == "--artifact-dir" && -n "${2:-}" ]]; then
  artifact_dir="$2"
fi

workspace_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

find_container() {
  docker ps --filter "label=devcontainer.local_folder=$1" --format '{{.ID}}' | head -n1
}

container_id="$(find_container "$workspace_dir")"
if [[ -z "$container_id" ]]; then
  common_git_dir="$(git -C "$workspace_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$common_git_dir" ]]; then
    main_checkout="$(dirname "$common_git_dir")"
    if [[ "$main_checkout" != "$workspace_dir" ]]; then
      container_id="$(find_container "$main_checkout")"
      if [[ -n "$container_id" ]]; then
        echo "iii_drone_mcp_bridge: using main checkout devcontainer ($main_checkout)" >&2
      fi
    fi
  fi
fi

if [[ -z "$container_id" ]]; then
  echo "iii_drone_mcp_bridge: III-Drone devcontainer not found for $workspace_dir" >&2
  exit 1
fi

exec docker exec -i "$container_id" bash -lc \
  "source /home/iii/ws/setup/setup_dev.bash; cd /home/iii/ws; exec iii-drone-mcp-server --artifact-dir '$artifact_dir'"
