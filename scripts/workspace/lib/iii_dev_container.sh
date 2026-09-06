#!/usr/bin/env bash

# Host-to-devcontainer transport for iii-dev. This file is sourced by the
# dispatcher and deliberately owns no runtime behavior.

III_DEV_DOCKER_BIN="${III_DEV_DOCKER_BIN:-docker}"
III_DEV_CONTAINER_USER="${III_DEV_CONTAINER_USER:-iii}"
III_DEV_CONTAINER_WORKSPACE="${III_DEV_CONTAINER_WORKSPACE:-/home/iii/ws}"

iii_dev_error() {
    printf 'iii-dev: %s\n' "$*" >&2
}

iii_dev_die() {
    iii_dev_error "$@"
    return 1
}

iii_dev_require_docker() {
    command -v "${III_DEV_DOCKER_BIN}" >/dev/null 2>&1 ||
        iii_dev_die "Docker is not installed or III_DEV_DOCKER_BIN is invalid."
    "${III_DEV_DOCKER_BIN}" info >/dev/null 2>&1 ||
        iii_dev_die "Docker is unavailable. Is the daemon running and accessible?"
}

iii_dev_running_containers() {
    "${III_DEV_DOCKER_BIN}" ps \
        --filter "label=devcontainer.local_folder=${III_DEV_WORKSPACE_ROOT}" \
        --format '{{.ID}}\t{{.Names}}'
}

iii_dev_all_containers() {
    "${III_DEV_DOCKER_BIN}" ps -a \
        --filter "label=devcontainer.local_folder=${III_DEV_WORKSPACE_ROOT}" \
        --format '{{.ID}}\t{{.Names}}\t{{.Status}}'
}

iii_dev_container_id() {
    local rows
    local -a containers=()

    iii_dev_require_docker || return
    rows="$(iii_dev_running_containers)" || return
    while IFS=$'\t' read -r container_id _container_name; do
        [[ -n "${container_id}" ]] && containers+=("${container_id}")
    done <<< "${rows}"

    if ((${#containers[@]} == 0)); then
        iii_dev_error "No running devcontainer is associated with ${III_DEV_WORKSPACE_ROOT}."
        iii_dev_error "Run './iii-dev container up' or start the workspace devcontainer in VS Code."
        return 1
    fi
    if ((${#containers[@]} != 1)); then
        iii_dev_error "Expected one running workspace devcontainer, found ${#containers[@]}:"
        printf '%s\n' "${rows}" >&2
        return 1
    fi
    printf '%s\n' "${containers[0]}"
}

iii_dev_exec() {
    local tty_mode="$1"
    shift
    local container_id
    local container_shell
    local -a docker_args=(exec --user "${III_DEV_CONTAINER_USER}" --workdir "${III_DEV_CONTAINER_WORKSPACE}")

    container_id="$(iii_dev_container_id)" || return
    case "${tty_mode}" in
        never)
            ;;
        interactive)
            if [[ ! -t 0 || ! -t 1 ]]; then
                iii_dev_die "This command requires an interactive terminal."
                return
            fi
            docker_args+=(-it)
            ;;
        *)
            iii_dev_die "Invalid internal TTY mode: ${tty_mode}"
            return
            ;;
    esac

    # Expansion is intentionally deferred to the bash process in the container.
    # shellcheck disable=SC2016
    container_shell='set -eo pipefail; workspace="$1"; shift; set +u; source "${workspace}/setup/setup_dev.bash" >/dev/null; set -u; exec "$@"'
    "${III_DEV_DOCKER_BIN}" "${docker_args[@]}" "${container_id}" \
        bash -lc "${container_shell}" \
        iii-dev "${III_DEV_CONTAINER_WORKSPACE}" "$@"
}

# A devcontainer shares the workspace with occasional root-owned maintenance and
# CI commands.  Root-owned generated files make the normal ``iii`` user fail
# late in a boot-time incremental build.  Repair only generated trees and only
# entries owned by root; source and user-owned artifacts are never touched.
iii_dev_repair_generated_ownership() {
    local container_id
    local repair_shell

    container_id="$(iii_dev_container_id)" || return
    repair_shell='set -eu; workspace="$1"; target_user="$2"; target_group="$(id -gn "$target_user")"; for root in build install log; do path="$workspace/$root"; [ -d "$path" ] || continue; find "$path" -xdev -uid 0 -exec chown "$target_user:$target_group" -- {} +; done'
    "${III_DEV_DOCKER_BIN}" exec \
        --user root \
        --workdir "${III_DEV_CONTAINER_WORKSPACE}" \
        "${container_id}" \
        bash -lc "${repair_shell}" \
        iii-dev-repair-generated-ownership \
        "${III_DEV_CONTAINER_WORKSPACE}" \
        "${III_DEV_CONTAINER_USER}"
}

iii_dev_container_status() {
    local running all

    iii_dev_require_docker || return
    running="$(iii_dev_running_containers)" || return
    if [[ -n "${running}" ]]; then
        printf 'Workspace devcontainer: running\n%s\n' "${running}"
        return 0
    fi

    all="$(iii_dev_all_containers)" || return
    if [[ -n "${all}" ]]; then
        printf 'Workspace devcontainer: stopped\n%s\n' "${all}"
    else
        printf 'Workspace devcontainer: not created\n'
    fi
    return 1
}

iii_dev_container_up() {
    local command_name="${III_DEV_DEVCONTAINER_BIN:-devcontainer}"
    local -a devcontainer_command=()

    iii_dev_require_docker || return
    if [[ -n "$(iii_dev_running_containers)" ]]; then
        iii_dev_container_status
        return 0
    fi

    if command -v "${command_name}" >/dev/null 2>&1; then
        devcontainer_command=("${command_name}")
    elif command -v npx >/dev/null 2>&1; then
        printf 'Dev Container CLI is not installed; running it through npx.\n'
        devcontainer_command=(npx --yes @devcontainers/cli)
    else
        iii_dev_error "The Dev Container CLI is required to create or resume the container safely."
        iii_dev_error "Install @devcontainers/cli, or start this workspace through VS Code once."
        return 1
    fi

    "${devcontainer_command[@]}" up --workspace-folder "${III_DEV_WORKSPACE_ROOT}"
    iii_dev_container_id >/dev/null
    iii_dev_container_status
}
