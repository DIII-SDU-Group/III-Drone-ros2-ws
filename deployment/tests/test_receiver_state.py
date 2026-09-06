from __future__ import annotations

import base64
from pathlib import Path
import struct
import hashlib

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.protocol import Action
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    ReceiverControlStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = 10.0
        self.boot = "boot-a"

    def monotonic(self) -> float:
        return self.value

    def boot_id(self) -> str:
        return self.boot


def key(character: int) -> str:
    blob = (
        struct.pack(">I", 11)
        + b"ssh-ed25519"
        + struct.pack(">I", 32)
        + bytes([character]) * 32
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


def enrollment(public_key: str, character: int) -> dict:
    signer = bytes([character]) * 32
    return create_machine_enrollment(
        label=f"machine-{character}",
        ssh_public_key=public_key,
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


def plan(operation_id: str = "operation-0001", client_id: str = "a" * 64) -> dict:
    from iii_deployment.contracts import content_identity

    value = {
        "schema": "iii.receiver-mutation-plan/v1",
        "plan_id": "0" * 64,
        "action": "stage",
        "operation_id": operation_id,
        "client_id": client_id,
        "receiver_generation": 1,
        "parameters": {
            "release_id": "b" * 64,
            "archive_sha256": "c" * 64,
            "upload_id": "d" * 64,
            "status_index_id": None,
        },
        "target": {"logical_id": "drone", "profile": "real"},
        "expected_state": {
            "active_release_id": None,
            "configuration_hash": "e" * 64,
            "commissioning_hash": "f" * 64,
            "profile": "real",
            "target_state_hash": "1" * 64,
            "access_state_id": "2" * 64,
        },
    }
    value["plan_id"] = content_identity(
        {k: v for k, v in value.items() if k != "plan_id"}
    )
    return value


def test_nonce_is_state_bound_expiring_single_use_and_lease_is_global(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = ReceiverControlStore(tmp_path, 1, 300, clock.monotonic, clock.boot_id)
    retained = plan()
    nonce, _ = store.issue_nonce(
        operation_id=retained["operation_id"],
        client_id=retained["client_id"],
        plan_id=retained["plan_id"],
    )
    with pytest.raises(ContractError, match="another operation"):
        store.consume_and_acquire(
            nonce=nonce,
            operation_id="operation-0002",
            client_id=retained["client_id"],
            action=Action.STAGE,
            plan_id=retained["plan_id"],
        )
    store.consume_and_acquire(
        nonce=nonce,
        operation_id=retained["operation_id"],
        client_id=retained["client_id"],
        action=Action.STAGE,
        plan_id=retained["plan_id"],
    )
    other = plan("operation-0002")
    other_nonce, _ = store.issue_nonce(
        operation_id=other["operation_id"],
        client_id=other["client_id"],
        plan_id=other["plan_id"],
    )
    with pytest.raises(ContractError, match="mutation lease"):
        store.consume_and_acquire(
            nonce=other_nonce,
            operation_id=other["operation_id"],
            client_id=other["client_id"],
            action=Action.STAGE,
            plan_id=other["plan_id"],
        )
    store.release(retained["operation_id"])
    with pytest.raises(ContractError, match="already consumed"):
        store.consume_and_acquire(
            nonce=nonce,
            operation_id=retained["operation_id"],
            client_id=retained["client_id"],
            action=Action.STAGE,
            plan_id=retained["plan_id"],
        )
    clock.value += 301
    with pytest.raises(ContractError, match="expired"):
        store.consume_and_acquire(
            nonce=other_nonce,
            operation_id=other["operation_id"],
            client_id=other["client_id"],
            action=Action.STAGE,
            plan_id=other["plan_id"],
        )


def test_nonce_expires_across_boot_and_journal_budget_pauses_while_powered_off(
    tmp_path: Path,
) -> None:
    clock = Clock()
    control = ReceiverControlStore(tmp_path, 1, 300, clock.monotonic, clock.boot_id)
    retained = plan()
    nonce, _ = control.issue_nonce(
        operation_id=retained["operation_id"],
        client_id=retained["client_id"],
        plan_id=retained["plan_id"],
    )
    clock.boot = "boot-b"
    with pytest.raises(ContractError, match="expired"):
        control.consume_and_acquire(
            nonce=nonce,
            operation_id=retained["operation_id"],
            client_id=retained["client_id"],
            action=Action.STAGE,
            plan_id=retained["plan_id"],
        )
    journals = OperationJournalStore(tmp_path, clock.monotonic, clock.boot_id)
    journals.create(plan=retained)
    clock.value += 500
    clock.boot = "boot-c"
    # A reboot must not consume the stage operation's powered-on budget. Stage
    # transfers have their own 600-second hard deadline; the shorter
    # 120-second default applies to non-staging mutations.
    assert journals.remaining_budget(retained["operation_id"]) == 600
    journals.transition(
        retained["operation_id"],
        state="running",
        checkpoint="mutation",
        cancellation_safe=False,
        event="started",
    )
    clock.value += 601
    assert journals.remaining_budget(retained["operation_id"]) == 0


def test_access_add_prove_revoke_and_final_key_denial(tmp_path: Path) -> None:
    manager = AccessManager(
        state_path=tmp_path / "access.json",
        authorized_keys_path=tmp_path / "authorized_keys",
        registry=REGISTRY,
        runtime_verifiers_path=tmp_path / "runtime-verifiers.json",
        field_signers_path=tmp_path / "field-signers.json",
    )
    old_key = key(1)
    new_key = key(2)
    old_id = client_id_for_public_key(old_key)
    new_id = client_id_for_public_key(new_key)
    old_enrollment = enrollment(old_key, 1)
    new_enrollment = enrollment(new_key, 2)
    manager.bootstrap([old_enrollment])
    manager.add_pending(requester=old_id, enrollment=new_enrollment)
    forced = (tmp_path / "authorized_keys").read_text(encoding="ascii")
    assert (
        'restrict,command="/usr/bin/iii-deployment-ssh-gateway --client-id ' in forced
    )
    assert "pty" not in forced and "ssh-ed25519" in forced
    with pytest.raises(ContractError, match="prove itself"):
        manager.prove(requester=old_id, enrollment=new_enrollment)
    manager.prove(requester=new_id, enrollment=new_enrollment)
    manager.revoke_field_signer(
        requester=old_id,
        field_signer_id=old_enrollment["field_signing"]["signer_id"],
    )
    signing_loss = {item["machine_id"]: item for item in manager.list_clients()}[
        old_enrollment["machine_id"]
    ]
    assert signing_loss["ssh_runtime_state"] == "active"
    assert signing_loss["field_signing_state"] == "revoked"
    assert old_enrollment["runtime_api"]["token_sha256"] in (
        tmp_path / "runtime-verifiers.json"
    ).read_text(encoding="utf-8")
    manager.revoke(requester=new_id, machine_id=old_enrollment["machine_id"])
    assert old_key not in (tmp_path / "authorized_keys").read_text(encoding="ascii")
    runtime = (tmp_path / "runtime-verifiers.json").read_text(encoding="utf-8")
    signers = (tmp_path / "field-signers.json").read_text(encoding="utf-8")
    assert old_enrollment["runtime_api"]["token_sha256"] not in runtime
    assert new_enrollment["runtime_api"]["token_sha256"] in runtime
    assert '"state":"revoked"' in signers and '"state":"active"' in signers
    with pytest.raises(ContractError, match="final usable"):
        manager.revoke(requester=new_id, machine_id=new_enrollment["machine_id"])
    assert all("public_key" not in item for item in manager.list_clients())


def test_initial_receiver_reconcile_is_safe_before_access_bootstrap(
    tmp_path: Path,
) -> None:
    manager = AccessManager(
        state_path=tmp_path / "missing-access.json",
        authorized_keys_path=tmp_path / "authorized_keys",
        registry=REGISTRY,
        runtime_verifiers_path=tmp_path / "runtime-verifiers.json",
        field_signers_path=tmp_path / "field-signers.json",
    )

    manager.reconcile_derived_access()

    assert not (tmp_path / "missing-access.json").exists()
    assert (tmp_path / "authorized_keys").read_bytes() == b""


def test_audit_is_hash_chained_and_never_contains_request_payload_or_key(
    tmp_path: Path,
) -> None:
    clock = Clock()
    audit = AuditLog(tmp_path / "audit.jsonl", clock.monotonic, clock.boot_id)
    first = audit.append(
        event="request",
        outcome="accepted",
        operation_id="operation-0001",
        client_id="a" * 64,
        action="access-add",
        detail_code="request-complete",
    )
    clock.value += 1
    second = audit.append(
        event="operation",
        outcome="completed",
        operation_id="operation-0001",
        client_id="a" * 64,
        action="access-add",
        detail_code="mutation-complete",
    )
    assert second["previous_event_id"] == first["event_id"]
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "ssh-ed25519" not in raw and "public_key" not in raw and "nonce" not in raw
    assert audit.entries() == [first, second]
