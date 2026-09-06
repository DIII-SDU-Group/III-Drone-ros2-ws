#!/usr/bin/env bash
# Canonical operator-to-aircraft Runtime API binding. Keep credentials in an
# owner-only file and never rely on whatever service happens to listen locally.

III_RUNTIME_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export III_RUNTIME_API_URL="${III_RUNTIME_API_URL:-http://iii.local:8765}"
export III_RUNTIME_API_TOKEN_FILE="${III_RUNTIME_API_TOKEN_FILE:-$III_RUNTIME_CONFIG_HOME/iii/credentials/runtime-api.token}"
unset III_RUNTIME_CONFIG_HOME
