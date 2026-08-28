from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import struct

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.receiver import access_bootstrap
from iii_deployment.receiver.access import AccessManager

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"
REGISTRY = ContractRegistry(SCHEMAS)


def _key(character: int) -> str:
    blob = (
        struct.pack(">I", 11)
        + b"ssh-ed25519"
        + struct.pack(">I", 32)
        + bytes([character]) * 32
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def _enrollment(character: int) -> dict:
    signer = bytes([character]) * 32
    return create_machine_enrollment(
        label=f"machine-{character}",
        ssh_public_key=_key(character),
        runtime_token=base64.urlsafe_b64encode(bytes([character + 10]) * 32)
        .decode("ascii")
        .rstrip("="),
        field_signer_descriptor={
            "schema_version": "1",
            "descriptor_type": "iii.signer-public",
            "signer_id": hashlib.sha256(signer).hexdigest(),
            "algorithm": "Ed25519",
            "authority": "workstation-field",
            "public_key": base64.b64encode(signer).decode("ascii"),
        },
        registry=REGISTRY,
    )


def _configure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(access_bootstrap, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(
        access_bootstrap, "AUTHORIZED_KEYS_PATH", tmp_path / "authorized_keys"
    )
    monkeypatch.setattr(
        access_bootstrap, "RUNTIME_VERIFIERS_PATH", tmp_path / "runtime-verifiers.json"
    )
    monkeypatch.setattr(
        access_bootstrap, "FIELD_SIGNERS_PATH", tmp_path / "field-signers.json"
    )


def _write(path: Path, value: dict) -> None:
    path.write_bytes(canonical_json(value) + b"\n")


def test_access_bootstrap_is_idempotent_and_reconciles_all_derived_access(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    source = tmp_path / "enrollment.json"
    _write(source, _enrollment(1))
    first = access_bootstrap.reconcile([source], schema_root=SCHEMAS)
    assert first["changed"] is True
    assert first["active_machines"]
    (tmp_path / "authorized_keys").write_text("tampered\n")
    (tmp_path / "runtime-verifiers.json").write_text("tampered\n")
    (tmp_path / "access-state.json").chmod(0o600)
    second = access_bootstrap.reconcile([source], schema_root=SCHEMAS)
    assert second["changed"] is True
    assert (tmp_path / "access-state.json").stat().st_mode & 0o777 == 0o640
    assert (
        'restrict,command="/usr/bin/iii-deployment-ssh-gateway'
        in (tmp_path / "authorized_keys").read_text()
    )
    assert (
        AccessManager(
            state_path=tmp_path / "access-state.json",
            authorized_keys_path=tmp_path / "authorized_keys",
            registry=REGISTRY,
        ).load()["access_id"]
        == first["access_id"]
    )


def test_access_bootstrap_projects_authorized_keys_to_runtime_owner(
    monkeypatch, tmp_path: Path
) -> None:
    import iii_deployment.receiver.access as access

    _configure(monkeypatch, tmp_path)
    source = tmp_path / "enrollment.json"
    _write(source, _enrollment(1))
    ownership: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(
        access.os,
        "chown",
        lambda path, uid, gid, **_kwargs: ownership.append((Path(path), uid, gid)),
    )

    access_bootstrap.reconcile(
        [source], schema_root=SCHEMAS, runtime_uid=1100, runtime_gid=1100
    )

    assert (tmp_path / "authorized_keys", 1100, 1100) in ownership


def test_access_bootstrap_allows_later_enrollment_but_rejects_bootstrap_change(
    monkeypatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    source = tmp_path / "enrollment.json"
    first = _enrollment(1)
    second = _enrollment(2)
    _write(source, first)
    access_bootstrap.reconcile([source], schema_root=SCHEMAS)
    manager = AccessManager(
        state_path=tmp_path / "access-state.json",
        authorized_keys_path=tmp_path / "authorized_keys",
        runtime_verifiers_path=tmp_path / "runtime-verifiers.json",
        field_signers_path=tmp_path / "field-signers.json",
        registry=REGISTRY,
    )
    manager.add_pending(requester=first["ssh"]["client_id"], enrollment=second)
    manager.prove(requester=second["ssh"]["client_id"], enrollment=second)
    assert access_bootstrap.reconcile([source], schema_root=SCHEMAS)["changed"] is False
    manager.revoke(requester=second["ssh"]["client_id"], machine_id=first["machine_id"])
    assert access_bootstrap.reconcile([source], schema_root=SCHEMAS)["changed"] is False
    _write(source, _enrollment(3))
    with pytest.raises(ContractError, match="bootstrap enrollment"):
        access_bootstrap.reconcile([source], schema_root=SCHEMAS)
