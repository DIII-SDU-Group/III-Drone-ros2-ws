from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

sys.dont_write_bytecode = True

if os.environ.get("III_DRONE_MCP_KEEP_RMW") != "1":
    os.environ["RMW_IMPLEMENTATION"] = os.environ.get("III_DRONE_MCP_RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
    os.environ["FASTDDS_BUILTIN_TRANSPORTS"] = os.environ.get("III_DRONE_MCP_FASTDDS_BUILTIN_TRANSPORTS", "UDPv4")


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"arguments must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("arguments must be a JSON object")
    return parsed


def main() -> None:
    from iii_drone_mcp.agent_tools import DroneAgentTools, result_to_text
    from iii_drone_mcp.mcp_server import _reexec_as_runtime_user_if_needed

    _reexec_as_runtime_user_if_needed()

    parser = argparse.ArgumentParser(description="Call one III-Drone MCP tool from the command line")
    parser.add_argument("tool", help="MCP tool name, for example simulation or px4")
    parser.add_argument("arguments", nargs="?", help="JSON object with tool arguments")
    parser.add_argument("--artifact-dir", default=os.environ.get("III_DRONE_MCP_ARTIFACT_DIR", "/tmp/iii_drone/iii_drone_agent"))
    parser.add_argument("--px4-system-address", default="udpin://0.0.0.0:14540")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of text")
    parser.add_argument("--log-stderr", action="store_true", help="Keep middleware logs on stderr instead of writing them to an artifact log")
    args = parser.parse_args()

    if args.json and not args.log_stderr:
        _redirect_stderr(args.artifact_dir, "mcp_call_stderr.log")

    tools = DroneAgentTools(
        artifact_dir=args.artifact_dir,
        px4_system_address=args.px4_system_address,
    )
    try:
        arguments = _parse_arguments(args.arguments)
        specs = tools_as_specs(tools)
        spec = specs.get(args.tool)
        if spec is None:
            raise SystemExit(f"unknown tool: {args.tool}")
        result = spec(arguments)
        if args.json:
            print(
                json.dumps(
                    {
                        "success": bool(result.success),
                        "message": result.message,
                        "data": result.data,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(result_to_text(result))
        raise SystemExit(0 if result.success else 1)
    except SystemExit:
        raise
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "success": False,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    indent=2,
                )
            )
        else:
            print(f"{exc}\n{traceback.format_exc()}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        tools.close()


def tools_as_specs(tools: Any):
    from iii_drone_mcp.mcp_server import DroneMcpServer

    server = DroneMcpServer(tools)
    return {
        name: spec.handler
        for name, spec in server._tool_specs.items()
    }


def _redirect_stderr(artifact_dir: str, filename: str) -> None:
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, filename)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    os.dup2(fd, 2)
    os.close(fd)


if __name__ == "__main__":
    main()
