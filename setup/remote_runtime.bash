#!/usr/bin/env bash
# Canonical operator-to-aircraft Runtime API binding for the development
# platform. The runtime API is intentionally unauthenticated.
export III_RUNTIME_API_HOST="${III_HIL_PI_ENDPOINT:-${III_HIL_PI_ADDRESS:-${III_RUNTIME_API_HOST:-${III_RUNTIME_HOST:-${III_SSH_HOST:-iii.local}}}}}"
export III_RUNTIME_API_URL="${III_RUNTIME_API_URL:-http://${III_RUNTIME_API_HOST}:8765}"
