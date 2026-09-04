SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
CLI_DIR="$WORKSPACE_DIR/tools/III-Drone-CLI"
CLI_BIN_DIR="$CLI_DIR/bin"
DEPLOYMENT_SRC_DIR="$WORKSPACE_DIR/deployment/src"
CONTRACTS_SRC_DIR="$WORKSPACE_DIR/src/III-Drone-Contracts"

prepend_path() {
    local path_entry="$1"
    case ":$PATH:" in
        *":$path_entry:"*)
            ;;
        *)
            export PATH="$path_entry:$PATH"
            ;;
    esac
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

if [ -d "$HOME/.local/bin" ]; then
    prepend_path "$HOME/.local/bin"
fi

# A checked-out workspace is authoritative over a possibly stale per-user
# installation. This keeps development and field commands on the exact code
# that the operator is validating and deploying.
if [ -d "$CLI_BIN_DIR" ]; then
    prepend_path "$CLI_BIN_DIR"
fi

prepend_pythonpath "$CLI_DIR"
if [ -d "$CONTRACTS_SRC_DIR/iii_drone_contracts" ]; then
    prepend_pythonpath "$CONTRACTS_SRC_DIR"
fi
if [ -d "$DEPLOYMENT_SRC_DIR" ]; then
    prepend_pythonpath "$DEPLOYMENT_SRC_DIR"
fi

unset -f prepend_path
unset -f prepend_pythonpath
unset DEPLOYMENT_SRC_DIR
unset CONTRACTS_SRC_DIR
