SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
CLI_DIR="$WORKSPACE_DIR/tools/III-Drone-CLI"
CLI_BIN_DIR="$CLI_DIR/bin"
CONTRACTS_SRC_DIR="$WORKSPACE_DIR/src/III-Drone-Contracts"

prepend_path() {
    local path_entry="$1"
    local existing cleaned=""
    local saved_ifs="$IFS"
    local -a path_entries=()
    IFS=:
    read -r -a path_entries <<< "${PATH:-}"
    IFS="$saved_ifs"
    for existing in "${path_entries[@]}"; do
        if [[ -n "$existing" && "$existing" != "$path_entry" ]]; then
            cleaned="${cleaned:+${cleaned}:}${existing}"
        fi
    done
    export PATH="${path_entry}${cleaned:+:${cleaned}}"
}

prepend_pythonpath() {
    local path_entry="$1"
    case ":${PYTHONPATH:-}:" in
        *":$path_entry:"*)
            ;;
        *)
            export PYTHONPATH="$path_entry${PYTHONPATH:+:$PYTHONPATH}"
            ;;
    esac
}

# A checked-out workspace is authoritative over a possibly stale per-user
# installation. This keeps development and field commands on the exact code
# that the operator is validating and deploying. The standalone GC installer
# is the one exception: its marked wrapper must remain ahead of this checkout
# when a native host shell sources a runtime profile.
if [ -d "$CLI_BIN_DIR" ]; then
    prepend_path "$CLI_BIN_DIR"
fi

if [ -d "$HOME/.local/bin" ]; then
    if [ -f "$HOME/.local/bin/iii" ] &&
        grep -Fq '# managed by III GC installer' "$HOME/.local/bin/iii"; then
        prepend_path "$HOME/.local/bin"
    elif [ ! -d "$CLI_BIN_DIR" ]; then
        prepend_path "$HOME/.local/bin"
    fi
fi

prepend_pythonpath "$CLI_DIR"
if [ -d "$CONTRACTS_SRC_DIR/iii_drone_contracts" ]; then
    prepend_pythonpath "$CONTRACTS_SRC_DIR"
fi
unset -f prepend_path
unset -f prepend_pythonpath
unset CONTRACTS_SRC_DIR
