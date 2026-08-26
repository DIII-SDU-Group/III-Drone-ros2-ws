from __future__ import annotations

from pathlib import Path
import base64
import struct

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.receiver import access_bootstrap
from iii_deployment.receiver.access import AccessManager


def _key(character: bytes) -> str:
    blob = (
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + character * 32
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


KEY_A = _key(b"a")
KEY_B = _key(b"b")


def test_access_bootstrap_is_idempotent_and_reconciles_forced_commands(
    monkeypatch, tmp_path: Path
) -> None:
    state = tmp_path / "access-state.json"
    authorized = tmp_path / "authorized_keys"
    monkeypatch.setattr(access_bootstrap, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(access_bootstrap, "AUTHORIZED_KEYS_PATH", authorized)
    keys = tmp_path / "keys"
    keys.write_text(KEY_A + "\n")
    first = access_bootstrap.reconcile(keys)
    assert first["changed"] is True
    assert first["active_clients"]
    authorized.write_text("tampered\n")
    second = access_bootstrap.reconcile(keys)
    assert second["changed"] is False
    assert (
        'restrict,command="/usr/bin/iii-deployment-ssh-gateway'
        in authorized.read_text()
    )
    assert (
        AccessManager(state_path=state, authorized_keys_path=authorized).load()[
            "access_id"
        ]
        == first["access_id"]
    )


def test_access_bootstrap_rejects_key_set_change_and_noncanonical_input(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(access_bootstrap, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        access_bootstrap, "AUTHORIZED_KEYS_PATH", tmp_path / "authorized_keys"
    )
    keys = tmp_path / "keys"
    keys.write_text(KEY_A + "\n")
    access_bootstrap.reconcile(keys)
    keys.write_text(KEY_A + "\n" + KEY_B + "\n")
    with pytest.raises(ContractError, match="differs"):
        access_bootstrap.reconcile(keys)
    keys.write_text("# comment\n" + KEY_A + "\n")
    with pytest.raises(ContractError, match="only canonical"):
        access_bootstrap.reconcile(keys)
