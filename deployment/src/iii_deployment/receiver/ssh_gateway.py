"""Forced-command SSH gateway for receiver IPC and fixed-root SFTP only."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import sys
from typing import Callable

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.receiver.client import main as receiver_client_main
from iii_deployment.receiver.config import INCOMING_ROOT
from iii_deployment.receiver.protocol import IDENTITY
from iii_deployment.receiver.transport import MAXIMUM_REQUEST_BYTES
from iii_deployment.receiver.upload import BackupUploadStore, LOCK_PATH, UploadStore


SFTP_SERVER = Path("/usr/lib/openssh/sftp-server")
SFTP_ORIGINAL_COMMANDS = frozenset(
    {
        "internal-sftp",
        "sftp-server",
        "/usr/lib/openssh/sftp-server",
    }
)

# Linux UAPI values from <linux/landlock.h>.  The deployment target is Linux
# ARM64 and these syscalls use the generic syscall numbers on both ARM64 and
# x86_64 (the latter is also useful for workstation verification).
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_WRITE_ACCESS = (
    (1 << 1)  # WRITE_FILE
    | (1 << 4)  # REMOVE_DIR
    | (1 << 5)  # REMOVE_FILE
    | (1 << 6)  # MAKE_CHAR
    | (1 << 7)  # MAKE_DIR
    | (1 << 8)  # MAKE_REG
    | (1 << 9)  # MAKE_SOCK
    | (1 << 10)  # MAKE_FIFO
    | (1 << 11)  # MAKE_BLOCK
    | (1 << 12)  # MAKE_SYM
    | (1 << 13)  # REFER
    | (1 << 14)  # TRUNCATE
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def restrict_writes_to(root: Path) -> None:
    """Confine this process and descendants to writes beneath ``root``.

    ``sftp-server -d`` changes only the initial directory.  Landlock supplies
    the actual write boundary without granting the unprivileged SSH account any
    namespace or chroot capability.  Unsupported kernels fail closed.
    """

    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ContractError("fixed SFTP write root is unavailable or unsafe")
    libc = ctypes.CDLL(None, use_errno=True)
    ruleset = _RulesetAttr(_WRITE_ACCESS)
    ruleset_fd = libc.syscall(
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset),
        ctypes.sizeof(ruleset),
        0,
    )
    if ruleset_fd < 0:
        observed = ctypes.get_errno()
        if observed in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL}:
            raise ContractError("kernel Landlock write confinement is unavailable")
        raise OSError(observed, os.strerror(observed))
    root_fd = -1
    try:
        root_fd = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        path_rule = _PathBeneathAttr(_WRITE_ACCESS, root_fd)
        if (
            libc.syscall(
                _LANDLOCK_ADD_RULE,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(path_rule),
                0,
            )
            < 0
        ):
            observed = ctypes.get_errno()
            raise OSError(observed, os.strerror(observed))
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            observed = ctypes.get_errno()
            raise OSError(observed, os.strerror(observed))
        if libc.syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            observed = ctypes.get_errno()
            raise OSError(observed, os.strerror(observed))
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(ruleset_fd)


def _request_document() -> dict:
    raw = sys.stdin.buffer.read(MAXIMUM_REQUEST_BYTES + 2)
    if len(raw) > MAXIMUM_REQUEST_BYTES + 1 or not raw.endswith(b"\n"):
        raise ContractError("SSH upload control requires one bounded JSON document")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"SSH upload control JSON is invalid: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError("SSH upload control document is not canonical")
    return value


def _emit(value: dict) -> int:
    sys.stdout.buffer.write(canonical_json(value) + b"\n")
    return 0


def dispatch(
    *,
    client_id: str,
    original_command: str,
    incoming_root: Path = INCOMING_ROOT,
    lock_path: Path = LOCK_PATH,
    sftp_server: Path = SFTP_SERVER,
    execve=os.execve,
    write_restrictor: Callable[[Path], None] = restrict_writes_to,
) -> int:
    """Dispatch an exact, non-shell command under the authenticated SSH key."""

    if not IDENTITY.fullmatch(client_id):
        raise ContractError("forced SSH gateway client identity is invalid")
    if original_command == "":
        return receiver_client_main()
    if original_command in SFTP_ORIGINAL_COMMANDS:
        if not sftp_server.is_absolute() or not sftp_server.is_file():
            raise ContractError("fixed OpenSSH SFTP server is unavailable")
        store = UploadStore(incoming_root, lock_path=lock_path)
        descriptor_context = store.locked(nonblocking=True)
        descriptor = descriptor_context.__enter__()
        os.set_inheritable(descriptor, True)
        write_restrictor(incoming_root)
        arguments = [
            str(sftp_server),
            "-d",
            str(incoming_root),
            "-u",
            "027",
            "-P",
            "symlink,hardlink",
        ]
        try:
            execve(str(sftp_server), arguments, {"PATH": "/usr/bin:/bin"})
        finally:
            descriptor_context.__exit__(None, None, None)
        raise ContractError("fixed SFTP server unexpectedly returned")

    fields = original_command.split(" ")
    if fields and fields[0] == "iii-backup-upload":
        if len(fields) != 3 or fields[1] not in {"begin", "inspect", "finalize"}:
            raise ContractError("SSH portable backup upload command is unsupported")
        action, backup_id = fields[1:]
        backups = BackupUploadStore(incoming_root, lock_path=lock_path)
        if action == "begin":
            result = backups.begin(
                _request_document(), backup_id=backup_id, client_id=client_id
            )
        elif action == "inspect":
            result = backups.inspect(backup_id=backup_id, client_id=client_id)
        else:
            result = backups.finalize(backup_id=backup_id, client_id=client_id)
        return _emit(result)
    if not fields or fields[0] != "iii-upload":
        raise ContractError("SSH command is outside the fixed deployment gateway")
    store = UploadStore(incoming_root, lock_path=lock_path)
    if fields[1:] == ["cleanup"]:
        return _emit(store.cleanup())
    if len(fields) != 3 or fields[1] not in {
        "begin",
        "inspect",
        "touch",
        "finalize",
    }:
        raise ContractError("SSH upload control command is unsupported")
    action, release_id = fields[1:]
    if action == "begin":
        result = store.begin(
            _request_document(), release_id=release_id, client_id=client_id
        )
    elif action == "inspect":
        result = store.inspect(release_id=release_id, client_id=client_id)
    elif action == "touch":
        result = store.touch(release_id=release_id, client_id=client_id)
    else:
        result = store.finalize(release_id=release_id, client_id=client_id)
    return _emit(result)


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-deployment-ssh-gateway")
    parser.add_argument("--client-id", required=True)
    arguments = parser.parse_args()
    try:
        return dispatch(
            client_id=arguments.client_id,
            original_command=os.environ.get("SSH_ORIGINAL_COMMAND", ""),
        )
    except (ContractError, OSError) as exc:
        parser.error(str(exc))
    return 64
