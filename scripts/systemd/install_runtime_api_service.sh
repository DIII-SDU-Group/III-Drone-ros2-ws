#!/bin/bash
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_SOURCE="$WORKSPACE_DIR/tools/systemd/iii-runtime-api.service"
SERVICE_TARGET="/etc/systemd/system/iii-runtime-api.service"

if [[ ! -f "$SERVICE_SOURCE" ]]; then
    echo "runtime API service source not found: $SERVICE_SOURCE" >&2
    exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
    echo "Would install $SERVICE_SOURCE to $SERVICE_TARGET"
    echo "Would run: systemctl daemon-reload"
    echo "Would run: systemctl enable iii-runtime-api.service"
    echo "Would run: systemctl restart iii-runtime-api.service"
    exit 0
fi

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
sudo systemctl enable iii-runtime-api.service
sudo systemctl restart iii-runtime-api.service
