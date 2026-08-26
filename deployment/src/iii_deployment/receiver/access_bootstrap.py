"""Idempotent root-only bootstrap of the first forced-command operator keys."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from iii_deployment.contracts import ContractError
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.config import AUTHORIZED_KEYS_PATH, STATE_ROOT


def _keys(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        raise ContractError("operator public-key input is missing or linked")
    values = [line.strip() for line in path.read_text(encoding="ascii").splitlines()]
    if not values or any(not value or value.startswith("#") for value in values):
        raise ContractError(
            "operator public-key input must contain only canonical keys"
        )
    identities = [client_id_for_public_key(value) for value in values]
    if len(set(identities)) != len(identities):
        raise ContractError("operator public-key input repeats a key")
    return values


def reconcile(path: Path) -> dict:
    manager = AccessManager(
        state_path=STATE_ROOT / "access-state.json",
        authorized_keys_path=AUTHORIZED_KEYS_PATH,
    )
    keys = _keys(path)
    state = manager.load()
    if not state["clients"]:
        state = manager.bootstrap(keys)
        changed = True
    else:
        expected = {client_id_for_public_key(value): value for value in keys}
        active = {
            client_id: record["public_key"]
            for client_id, record in state["clients"].items()
            if record["state"] == "active"
        }
        if active != expected or any(
            record["state"] == "pending" for record in state["clients"].values()
        ):
            raise ContractError(
                "existing receiver access state differs from the Ansible bootstrap keys"
            )
        manager.reconcile_authorized_keys()
        changed = False
    return {
        "schema": "iii.receiver-access-bootstrap/v1",
        "changed": changed,
        "access_id": state["access_id"],
        "active_clients": sorted(
            client_id
            for client_id, record in state["clients"].items()
            if record["state"] == "active"
        ),
        "forced_command": "/usr/bin/iii-deployment-ssh-gateway",
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-receiver-access-bootstrap")
    parser.add_argument("--keys", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ContractError("receiver access bootstrap requires root")
        result = reconcile(arguments.keys)
    except (ContractError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
