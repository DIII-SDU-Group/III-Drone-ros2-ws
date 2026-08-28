"""Idempotent root-only bootstrap of public per-machine credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.identity import load_machine_enrollment
from iii_deployment.receiver.access import AccessManager
from iii_deployment.receiver.config import (
    AUTHORIZED_KEYS_PATH,
    FIELD_SIGNERS_PATH,
    RUNTIME_VERIFIERS_PATH,
    SCHEMA_ROOT,
    STATE_ROOT,
)


def _projection() -> tuple[tuple[str, int, int, int, str] | None, ...]:
    paths = (
        STATE_ROOT / "access-state.json",
        AUTHORIZED_KEYS_PATH,
        RUNTIME_VERIFIERS_PATH,
        FIELD_SIGNERS_PATH,
    )
    rows = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            rows.append(None)
            continue
        metadata = path.lstat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if stat.S_ISREG(metadata.st_mode)
            else "unsafe"
        )
        rows.append(
            (
                path.name,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                digest,
            )
        )
    return tuple(rows)


def reconcile(
    paths: list[Path],
    *,
    schema_root: Path = SCHEMA_ROOT,
    runtime_uid: int | None = None,
    runtime_gid: int | None = None,
) -> dict:
    registry = ContractRegistry(schema_root)
    enrollments = [load_machine_enrollment(path, registry) for path in paths]
    manager = AccessManager(
        state_path=STATE_ROOT / "access-state.json",
        authorized_keys_path=AUTHORIZED_KEYS_PATH,
        registry=registry,
        runtime_verifiers_path=RUNTIME_VERIFIERS_PATH,
        field_signers_path=FIELD_SIGNERS_PATH,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
    )
    before_projection = _projection()
    before = manager.load()
    state = manager.bootstrap(enrollments)
    return {
        "schema": "iii.receiver-access-bootstrap/v2",
        "changed": (
            before["access_id"] != state["access_id"]
            or before_projection != _projection()
        ),
        "access_id": state["access_id"],
        "active_machines": sorted(
            record["machine_id"]
            for record in state["clients"].values()
            if record["state"] == "active"
        ),
        "forced_command": "/usr/bin/iii-deployment-ssh-gateway",
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-receiver-access-bootstrap")
    parser.add_argument("--enrollment", type=Path, action="append", required=True)
    parser.add_argument("--runtime-uid", type=int, required=True)
    parser.add_argument("--runtime-gid", type=int, required=True)
    parser.add_argument("--schema-root", type=Path, default=SCHEMA_ROOT)
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ContractError("receiver access bootstrap requires root")
        result = reconcile(
            arguments.enrollment,
            schema_root=arguments.schema_root,
            runtime_uid=arguments.runtime_uid,
            runtime_gid=arguments.runtime_gid,
        )
    except (ContractError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
