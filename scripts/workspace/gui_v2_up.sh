#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

FRONTEND_URL="${III_GC_FRONTEND_URL:-http://127.0.0.1:5174}"
PROXY_URL="${III_GC_PROXY_URL:-http://127.0.0.1:8780}"
RUNTIME_URL="${III_RUNTIME_API_URL:-http://127.0.0.1:8765}"

exec scripts/workspace/gui_v2_sim_e2e_smoke.py \
  --runtime-url "$RUNTIME_URL" \
  --proxy-url "$PROXY_URL" \
  --frontend-url "$FRONTEND_URL" \
  --start-compose \
  --keep-compose \
  "$@"
