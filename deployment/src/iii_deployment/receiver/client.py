"""Forced unprivileged SSH client for the fixed receiver Unix socket."""

from __future__ import annotations

import argparse
import json
import socket
import sys

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.receiver.config import SOCKET_PATH
from iii_deployment.receiver.protocol import IDENTITY, Request
from iii_deployment.receiver.transport import MAXIMUM_REQUEST_BYTES


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-deploymentctl")
    parser.add_argument("--client-id", required=True)
    arguments = parser.parse_args()
    try:
        if not IDENTITY.fullmatch(arguments.client_id):
            raise ContractError("forced receiver client identity is invalid")
        raw = sys.stdin.buffer.read(MAXIMUM_REQUEST_BYTES + 2)
        if len(raw) > MAXIMUM_REQUEST_BYTES + 1 or not raw.endswith(b"\n"):
            raise ContractError(
                "receiver client requires one bounded newline-terminated request"
            )
        request = Request.parse(raw[:-1], maximum_bytes=MAXIMUM_REQUEST_BYTES)
        if request.client_id != arguments.client_id:
            raise ContractError(
                "request client differs from authenticated forced-command identity"
            )
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(SOCKET_PATH))
            connection.sendall(canonical_json(json.loads(raw)) + b"\n")
            connection.shutdown(socket.SHUT_WR)
            response = bytearray()
            while len(response) <= MAXIMUM_REQUEST_BYTES:
                block = connection.recv(65536)
                if not block:
                    break
                response.extend(block)
        finally:
            connection.close()
        if len(response) > MAXIMUM_REQUEST_BYTES or not response.endswith(b"\n"):
            raise ContractError("receiver returned an invalid bounded response")
        value = json.loads(response)
        if not isinstance(value, dict) or response != canonical_json(value) + b"\n":
            raise ContractError("receiver returned a non-canonical response")
        sys.stdout.buffer.write(response)
        return 0 if value.get("ok") is True else 1
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
