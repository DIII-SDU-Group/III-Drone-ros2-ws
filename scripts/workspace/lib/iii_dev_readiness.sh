#!/usr/bin/env bash

iii_dev_wait_until() {
    local label="$1"
    local timeout_seconds="$2"
    local interval_seconds="$3"
    shift 3
    local started_at="${SECONDS}"
    local output=""
    local result=1

    printf 'Waiting for %s' "${label}"
    while ((SECONDS - started_at < timeout_seconds)); do
        if output="$("$@" 2>&1)"; then
            printf ' ready.\n'
            return 0
        else
            result=$?
        fi
        if ((result == 2)); then
            printf ' failed.\n' >&2
            [[ -z "${output}" ]] || printf '%s\n' "${output}" >&2
            return 1
        fi
        printf '.'
        sleep "${interval_seconds}"
    done

    printf ' timed out after %ss.\n' "${timeout_seconds}" >&2
    if [[ -n "${output}" ]]; then
        printf '%s\n' "${output}" >&2
    fi
    return 1
}
