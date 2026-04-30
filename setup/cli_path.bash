SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
CLI_DIR="$WORKSPACE_DIR/tools/III-Drone-CLI"
CLI_BIN_DIR="$CLI_DIR/bin"

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

if [ -d "$CLI_BIN_DIR" ]; then
    prepend_path "$CLI_BIN_DIR"
fi

if [ -d "$HOME/.local/bin" ]; then
    prepend_path "$HOME/.local/bin"
fi

prepend_pythonpath "$CLI_DIR"

unset -f prepend_path
unset -f prepend_pythonpath
