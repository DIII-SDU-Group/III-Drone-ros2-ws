"""Linux Unix-socket transport and SSH forced-command peer authentication."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
from typing import Callable, Mapping

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.receiver.protocol import Request

MAXIMUM_REQUEST_BYTES = 1024 * 1024
CONNECTION_TIMEOUT_SECONDS = 5.0


def _process_parent(pid: int) -> int:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ContractError("cannot authenticate receiver peer process") from exc
    for line in lines:
        if line.startswith("PPid:"):
            return int(line.split()[1])
    raise ContractError("receiver peer process has no parent identity")


def authenticate_forced_ssh_peer(pid: int, uid: int, client_id: str) -> None:
    """Require the fixed client argv beneath a root-owned sshd session."""

    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError as exc:
        raise ContractError("cannot authenticate receiver peer command") from exc
    expected = [b"--client-id", client_id.encode("ascii")]
    if len(arguments) < 3 or arguments[-3:-1] != expected or arguments[-1] != b"":
        raise ContractError(
            "receiver peer was not invoked for the authenticated client"
        )
    current = pid
    for _ in range(12):
        current = _process_parent(current)
        if current <= 1:
            break
        try:
            executable = Path(f"/proc/{current}/exe").resolve()
            owner = Path(f"/proc/{current}").stat().st_uid
        except OSError:
            continue
        if executable.name == "sshd" and owner == 0:
            return
    raise ContractError(
        "receiver peer is not descended from an authenticated sshd session"
    )


class UnixReceiverServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        transport_uid: int,
        transport_gid: int,
        handler: Callable[[Request], dict],
        peer_authenticator: Callable[
            [int, int, str], None
        ] = authenticate_forced_ssh_peer,
        rejection_logger: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.transport_uid = transport_uid
        self.transport_gid = transport_gid
        self.handler = handler
        self.peer_authenticator = peer_authenticator
        self.rejection_logger = rejection_logger or (lambda _code, _pid, _uid: None)
        self.socket: socket.socket | None = None

    def open(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if self.socket_path.is_symlink() or not self.socket_path.is_socket():
                raise ContractError(
                    "receiver socket path is occupied by an unsafe entry"
                )
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
            if os.geteuid() == 0:
                os.chown(self.socket_path, 0, self.transport_gid)
            listener.listen(16)
        except Exception:
            listener.close()
            if self.socket_path.exists() and not self.socket_path.is_symlink():
                self.socket_path.unlink()
            raise
        self.socket = listener

    def serve_once(self) -> None:
        if self.socket is None:
            raise ContractError("receiver Unix socket is not open")
        connection, _ = self.socket.accept()
        with connection:
            connection.settimeout(CONNECTION_TIMEOUT_SECONDS)
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, uid, _gid = struct.unpack("3i", credentials)
            if uid not in {0, self.transport_uid}:
                self.rejection_logger("peer-uid-rejected", pid, uid)
                self._send_error(
                    connection, "peer-uid-rejected", "receiver peer UID is unauthorized"
                )
                return
            try:
                raw = self._receive(connection)
                request = Request.parse(raw, maximum_bytes=MAXIMUM_REQUEST_BYTES)
                self.peer_authenticator(pid, uid, request.client_id)
            except (ContractError, OSError) as exc:
                self.rejection_logger("transport-contract-rejected", pid, uid)
                response = {
                    "schema": "iii.receiver-response/v1",
                    "ok": False,
                    "error": {"code": "contract-rejected", "message": str(exc)},
                }
            else:
                try:
                    result = self.handler(request)
                    response = {
                        "schema": "iii.receiver-response/v1",
                        "ok": True,
                        "result": result,
                    }
                except ContractError as exc:
                    response = {
                        "schema": "iii.receiver-response/v1",
                        "ok": False,
                        "error": {"code": "contract-rejected", "message": str(exc)},
                    }
            self._send_response(connection, response)

    @staticmethod
    def _send_response(
        connection: socket.socket, response: Mapping[str, object]
    ) -> None:
        """Best-effort response delivery after a peer has disconnected.

        The stable bootstrap probes readiness by connecting to the receiver's
        Unix socket and closing immediately.  A vanished local peer must not
        terminate the long-running receiver process while it attempts to send
        the resulting contract error.
        """

        try:
            connection.sendall(canonical_json(response) + b"\n")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return

    @staticmethod
    def _receive(connection: socket.socket) -> bytes:
        blocks: list[bytes] = []
        total = 0
        while True:
            block = connection.recv(min(65536, MAXIMUM_REQUEST_BYTES + 1 - total))
            if not block:
                break
            total += len(block)
            if total > MAXIMUM_REQUEST_BYTES:
                raise ContractError("receiver request exceeds maximum size")
            blocks.append(block)
            if b"\n" in block:
                break
        raw = b"".join(blocks)
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ContractError(
                "receiver transport requires one newline-terminated request"
            )
        return raw[:-1]

    @staticmethod
    def _send_error(connection: socket.socket, code: str, message: str) -> None:
        connection.sendall(
            canonical_json(
                {
                    "schema": "iii.receiver-response/v1",
                    "ok": False,
                    "error": {"code": code, "message": message},
                }
            )
            + b"\n"
        )

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.socket_path.exists() and not self.socket_path.is_symlink():
            if not self.socket_path.is_socket():
                raise ContractError("receiver socket path changed type during shutdown")
            self.socket_path.unlink()
