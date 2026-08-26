from __future__ import annotations

import base64
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.protocol import Action
from iii_deployment.receiver.state import AuditLog, OperationJournalStore, ReceiverControlStore


class Clock:
    def __init__(self) -> None:
        self.value = 10.0
        self.boot = "boot-a"

    def monotonic(self) -> float:
        return self.value

    def boot_id(self) -> str:
        return self.boot


def key(character: int) -> str:
    return "ssh-ed25519 " + base64.b64encode(bytes([character]) * 32).decode("ascii")


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
    value["plan_id"] = content_identity({k: v for k, v in value.items() if k != "plan_id"})
    return value


def test_nonce_is_state_bound_expiring_single_use_and_lease_is_global(tmp_path: Path) -> None:
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
        operation_id=other["operation_id"], client_id=other["client_id"], plan_id=other["plan_id"]
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


def test_nonce_expires_across_boot_and_journal_budget_pauses_while_powered_off(tmp_path: Path) -> None:
    clock = Clock()
    control = ReceiverControlStore(tmp_path, 1, 300, clock.monotonic, clock.boot_id)
    retained = plan()
    nonce, _ = control.issue_nonce(
        operation_id=retained["operation_id"], client_id=retained["client_id"], plan_id=retained["plan_id"]
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
    assert journals.remaining_budget(retained["operation_id"]) == 120
    journals.transition(
        retained["operation_id"],
        state="running",
        checkpoint="mutation",
        cancellation_safe=False,
        event="started",
    )
    clock.value += 121
    assert journals.remaining_budget(retained["operation_id"]) == 0


def test_access_add_prove_revoke_and_final_key_denial(tmp_path: Path) -> None:
    manager = AccessManager(
        state_path=tmp_path / "access.json",
        authorized_keys_path=tmp_path / "authorized_keys",
    )
    old_key = key(1)
    new_key = key(2)
    old_id = client_id_for_public_key(old_key)
    new_id = client_id_for_public_key(new_key)
    manager.bootstrap([old_key])
    manager.add_pending(requester=old_id, client_id=new_id, public_key=new_key)
    forced = (tmp_path / "authorized_keys").read_text(encoding="ascii")
    assert "restrict,command=\"/usr/bin/iii-deploymentctl --client-id " in forced
    assert "pty" not in forced and "ssh-ed25519" in forced
    with pytest.raises(ContractError, match="prove itself"):
        manager.prove(requester=old_id, client_id=new_id, public_key=new_key)
    manager.prove(requester=new_id, client_id=new_id, public_key=new_key)
    manager.revoke(requester=new_id, client_id=old_id)
    assert old_key not in (tmp_path / "authorized_keys").read_text(encoding="ascii")
    with pytest.raises(ContractError, match="final usable"):
        manager.revoke(requester=new_id, client_id=new_id)
    assert all("public_key" not in item for item in manager.list_clients())


def test_audit_is_hash_chained_and_never_contains_request_payload_or_key(tmp_path: Path) -> None:
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
