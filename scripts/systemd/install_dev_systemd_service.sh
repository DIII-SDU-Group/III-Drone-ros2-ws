#!/bin/bash
set -euo pipefail

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_SOURCE="$WORKSPACE_DIR/tools/systemd/iii-system-daemon.service"
SERVICE_TARGET="/etc/systemd/system/iii-system-daemon.service"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is not available; the devcontainer must run with systemd." >&2
    exit 1
fi

system_state="$(systemctl is-system-running 2>&1 || true)"
case "$system_state" in
    *offline*|*"not been booted with systemd"*|*"Failed to connect to bus"*|*"Host is down"*)
        echo "systemd is not running in this container: $system_state" >&2
        echo "Rebuild/restart the devcontainer after applying the systemd devcontainer settings." >&2
        exit 1
        ;;
esac

sudo install -D -m 0644 "$SERVICE_SOURCE" "$SERVICE_TARGET"
sudo systemctl daemon-reload
sudo systemctl enable iii-system-daemon.service
sudo systemctl restart iii-system-daemon.service
