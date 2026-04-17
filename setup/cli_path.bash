SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
CLI_DIR="$WORKSPACE_DIR/tools/III-Drone-CLI"
CLI_BIN_DIR="$CLI_DIR/bin"

case ":$PATH:" in
    *":$CLI_BIN_DIR:"*)
        ;;
    *)
        export PATH="$CLI_BIN_DIR:$PATH"
        ;;
esac

case ":${PYTHONPATH:-}:" in
    *":$CLI_DIR:"*)
        ;;
    *)
        export PYTHONPATH="$CLI_DIR${PYTHONPATH:+:$PYTHONPATH}"
        ;;
esac
