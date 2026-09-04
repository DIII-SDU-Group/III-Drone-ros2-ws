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


def _relay(raw: bytes, *, client_id: str) -> bytes:
    """Forward one canonical request without weakening peer authentication.

    A persistent SSH gateway reuses this process for a bounded sequence of
    read-only clock probes.  Each request still gets its own Unix connection,
    so the receiver continues to authenticate the PID, UID, and forced SSH
    ancestry at the existing transport boundary.
    """
    if len(raw) > MAXIMUM_REQUEST_BYTES + 1 or not raw.endswith(b"\n"):
        raise ContractError(
            "receiver client requires one bounded newline-terminated request"
        )
    request = Request.parse(raw[:-1], maximum_bytes=MAXIMUM_REQUEST_BYTES)
    if request.client_id != client_id:
        raise ContractError(
            "request client differs from authenticated forced-command identity"
        )
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(SOCKET_PATH))
        connection.sendall(raw)
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
    return bytes(response)


def main(*, persistent: bool = False, client_id: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iii-deploymentctl")
    parser.add_argument("--client-id", required=client_id is None)
    parser.add_argument("--persistent", action="store_true")
    arguments = parser.parse_args()
    try:
        authenticated_client = client_id or arguments.client_id
        if arguments.persistent and not persistent:
            raise ContractError("persistent receiver mode is gateway-only")
        if not IDENTITY.fullmatch(authenticated_client):
            raise ContractError("forced receiver client identity is invalid")
        if persistent:
            failed = False
            while raw := sys.stdin.buffer.readline(MAXIMUM_REQUEST_BYTES + 2):
                response = _relay(raw, client_id=authenticated_client)
                sys.stdout.buffer.write(response)
                sys.stdout.buffer.flush()
                failed = failed or json.loads(response).get("ok") is not True
            return 1 if failed else 0
        raw = sys.stdin.buffer.read(MAXIMUM_REQUEST_BYTES + 2)
        response = _relay(raw, client_id=authenticated_client)
        sys.stdout.buffer.write(response)
        return 0 if json.loads(response).get("ok") is True else 1
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
