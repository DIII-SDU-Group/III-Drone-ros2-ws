from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.engine import ReceiverEngine
from iii_deployment.receiver.protocol import Action, Request
from iii_deployment.receiver.state import AuditLog, OperationJournalStore, ReceiverControlStore
from iii_deployment.staging import StageResult


class Clock:
    def __init__(self) -> None:
        self.value = 100.0
        self.boot = "boot-a"

    def monotonic(self) -> float:
        return self.value

    def boot_id(self) -> str:
        return self.boot


class QueuedExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        return SimpleNamespace()

    def run_next(self) -> None:
        function, args = self.calls.pop(0)
        function(*args)


class ImmediateExecutor:
    def submit(self, function, *args):
        function(*args)
        return SimpleNamespace()


@dataclass
class FakeReleaseStore:
    releases_root: Path
    stage_calls: list[tuple]

    def state(self) -> dict:
        return {"recovery": {"recovery_only": False, "flight_capable": True, "reason": None}}

    def stage(self, component: Path, *, status_index, staged_at: str) -> StageResult:
        self.stage_calls.append((component, status_index, staged_at))
        release_id = __import__("json").loads(
            (component / "release-manifest.json").read_text(encoding="utf-8")
        )["release_id"]
        return StageResult(release_id, "field-development", True, release_id, 1000, "9" * 64)


def key(character: int) -> str:
    return "ssh-ed25519 " + base64.b64encode(bytes([character]) * 32).decode("ascii")


def request(
    action: str,
    operation_id: str,
    client_id: str,
    payload: dict,
    nonce: str | None = None,
) -> Request:
    return Request.parse(
        canonical_json(
            {
                "protocol_version": "1",
                "action": action,
                "operation_id": operation_id,
                "client_id": client_id,
                "payload": payload,
                "nonce": nonce,
            }
        )
    )


@pytest.fixture
def receiver(tmp_path: Path):
    clock = Clock()
    executor = QueuedExecutor()
    access = AccessManager(
        state_path=tmp_path / "state/access.json",
        authorized_keys_path=tmp_path / "home/iii/.ssh/authorized_keys",
    )
    operator_key = key(1)
    operator_id = client_id_for_public_key(operator_key)
    access.bootstrap([operator_key])
    live = {
        "active_release_id": None,
        "configuration_hash": "a" * 64,
        "commissioning_hash": "b" * 64,
        "profile": "real",
        "target_state_hash": "c" * 64,
    }
    store = FakeReleaseStore(tmp_path / "target/opt/iii/releases", [])
    store.releases_root.mkdir(parents=True)
    control = ReceiverControlStore(
        tmp_path / "state", 1, 300, clock.monotonic, clock.boot_id
    )
    journals = OperationJournalStore(tmp_path / "state", clock.monotonic, clock.boot_id)
    audit = AuditLog(tmp_path / "log/audit.jsonl", clock.monotonic, clock.boot_id)

    def build(selected_executor=executor):
        return ReceiverEngine(
            release_store=store,
            control=control,
            journals=journals,
            audit=audit,
            access=access,
            incoming_root=tmp_path / "incoming",
            receiver_root=tmp_path / "target/opt/iii/receiver",
            logical_target="drone",
            profile="real",
            live_state=lambda: live,
            executor=selected_executor,
        )

    return SimpleNamespace(
        clock=clock,
        executor=executor,
        access=access,
        operator_id=operator_id,
        live=live,
        store=store,
        control=control,
        journals=journals,
        audit=audit,
        engine=build(),
        build=build,
        root=tmp_path,
    )


def stage_plan(receiver, operation_id: str = "operation-stage-0001"):
    release_id = "d" * 64
    upload_id = "e" * 64
    component = receiver.root / "incoming" / upload_id / "drone"
    component.mkdir(parents=True, exist_ok=True)
    archive = component / "bundle.tar.zst"
    archive.write_bytes(b"signed archive")
    (component / "release-manifest.json").write_bytes(
        canonical_json({"release_id": release_id}) + b"\n"
    )
    (component / "bundle.manifest.json").write_bytes(canonical_json({}) + b"\n")
    (component / "bundle.sha256").write_text("placeholder\n", encoding="ascii")
    (component / "bundle.sig.json").write_bytes(canonical_json({}) + b"\n")
    planning = request(
        "plan-stage",
        operation_id,
        receiver.operator_id,
        {
            "artifact": {
                "release_id": release_id,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "upload_id": upload_id,
                "status_index_id": None,
            },
            "target": {"logical_id": "drone", "profile": "real"},
        },
    )
    return receiver.engine.handle(planning)


def apply_stage(receiver, planned: dict, operation_id: str = "operation-stage-0001"):
    return receiver.engine.handle(
        request(
            "stage",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )


def test_accepted_stage_detaches_survives_client_loss_and_reattaches(receiver) -> None:
    planned = stage_plan(receiver)
    accepted = apply_stage(receiver, planned)
    assert accepted["detached"] is True
    assert accepted["operation"]["state"] == "accepted"
    assert receiver.control.load()["lease"]["operation_id"] == "operation-stage-0001"
    claimed = receiver.root / "state/accepted-inputs/operation-stage-0001/drone"
    assert claimed.is_dir()
    assert (claimed / "bundle.tar.zst").stat().st_mode & 0o222 == 0
    original = receiver.root / "incoming" / ("e" * 64) / "drone"
    (original / "bundle.tar.zst").write_bytes(b"attacker changed upload after acceptance")
    (original / "release-manifest.json").write_bytes(
        canonical_json({"release_id": "f" * 64}) + b"\n"
    )
    while_running = receiver.engine.handle(
        request("status", "operation-stage-0001", receiver.operator_id, {})
    )
    assert while_running["operation"]["state"] == "accepted"
    assert while_running["lease"]["operation_id"] == "operation-stage-0001"
    receiver.executor.run_next()
    status = receiver.engine.handle(
        request("status", "operation-stage-0001", receiver.operator_id, {})
    )
    assert status["operation"]["state"] == "completed"
    assert status["operation"]["result"]["deadlines"] == {
        "target_acceptance_s": 60,
        "hard_deadline_s": 120,
        "rollback_target_s": 60,
    }
    assert status["lease"] is None
    assert len(receiver.store.stage_calls) == 1
    assert receiver.store.stage_calls[0][0] == claimed


def test_stale_cross_plan_replay_and_concurrent_mutation_are_rejected(receiver) -> None:
    first = stage_plan(receiver, "operation-stage-0001")
    second = stage_plan(receiver, "operation-stage-0002")
    with pytest.raises(ContractError, match="another operation"):
        receiver.engine.handle(
            request(
                "stage",
                "operation-stage-0002",
                receiver.operator_id,
                {"plan": second["plan"]},
                first["nonce"],
            )
        )
    apply_stage(receiver, first, "operation-stage-0001")
    with pytest.raises(ContractError, match="mutation lease"):
        apply_stage(receiver, second, "operation-stage-0002")
    receiver.executor.run_next()
    with pytest.raises(ContractError, match="already consumed"):
        apply_stage(receiver, first, "operation-stage-0001")
    third = stage_plan(receiver, "operation-stage-0003")
    receiver.live["configuration_hash"] = "f" * 64
    with pytest.raises(ContractError, match="stale"):
        apply_stage(receiver, third, "operation-stage-0003")


def test_safe_cancel_and_unsafe_cancel_contract(receiver) -> None:
    planned = stage_plan(receiver)
    apply_stage(receiver, planned)
    cancelled = receiver.engine.handle(
        request(
            "cancel",
            "cancel-operation-0001",
            receiver.operator_id,
            {"target_operation_id": "operation-stage-0001"},
        )
    )
    assert cancelled["operation"]["state"] == "cancel-requested"
    receiver.executor.run_next()
    assert receiver.journals.load("operation-stage-0001")["state"] == "cancelled"

    planned = stage_plan(receiver, "operation-stage-0002")
    apply_stage(receiver, planned, "operation-stage-0002")
    receiver.journals.transition(
        "operation-stage-0002",
        state="running",
        checkpoint="privileged-mutation",
        cancellation_safe=False,
        event="test-running",
    )
    with pytest.raises(ContractError, match="cancellation-safe"):
        receiver.engine.handle(
            request(
                "cancel",
                "cancel-operation-0002",
                receiver.operator_id,
                {"target_operation_id": "operation-stage-0002"},
            )
        )


def test_restart_and_reboot_reconcile_without_starting_autonomy(receiver) -> None:
    planned = stage_plan(receiver)
    apply_stage(receiver, planned)
    resumed_executor = QueuedExecutor()
    restarted = receiver.build(resumed_executor)
    result = restarted.reconcile()
    assert result == {
        "schema": "iii.receiver-result/v1",
        "recovered_operations": ["operation-stage-0001"],
        "failed_operations": [],
        "autonomy_started": False,
    }
    receiver.clock.boot = "boot-b"
    receiver.clock.value += 500
    resumed_executor.run_next()
    assert receiver.journals.load("operation-stage-0001")["state"] == "completed"


def test_stale_lease_without_journal_is_recovered_and_audited(receiver) -> None:
    planned = stage_plan(receiver)
    receiver.control.consume_and_acquire(
        nonce=planned["nonce"],
        operation_id="operation-stage-0001",
        client_id=receiver.operator_id,
        action=Action.STAGE,
        plan_id=planned["plan"]["plan_id"],
    )
    result = receiver.engine.reconcile()
    assert result["recovered_operations"] == []
    assert receiver.control.load()["lease"] is None
    assert receiver.audit.entries()[-1]["detail_code"] == "stale-lease-released"


def test_deadline_expiry_fails_closed_before_privileged_mutation(receiver) -> None:
    planned = stage_plan(receiver)
    apply_stage(receiver, planned)
    receiver.clock.value += 121
    receiver.executor.run_next()
    journal = receiver.journals.load("operation-stage-0001")
    assert journal["state"] == "failed"
    assert receiver.store.stage_calls == []
    assert receiver.control.load()["lease"] is None


def test_unplanned_or_oversized_upload_is_rejected_before_durable_acceptance(receiver) -> None:
    planned = stage_plan(receiver)
    component = receiver.root / "incoming" / ("e" * 64) / "drone"
    (component / "arbitrary-unit.service").write_text("host injection", encoding="utf-8")
    with pytest.raises(ContractError, match="exact fixed file set"):
        apply_stage(receiver, planned)
    assert receiver.control.load()["lease"] is None
    assert receiver.journals.load("operation-stage-0001") is None

    (component / "arbitrary-unit.service").unlink()
    planned = stage_plan(receiver, "operation-stage-0002")
    receiver.engine.maximum_claim_bytes = 1
    with pytest.raises(ContractError, match="input limit"):
        apply_stage(receiver, planned, "operation-stage-0002")
    assert receiver.control.load()["lease"] is None


def test_key_rotation_through_planned_receiver_actions_and_final_key_loss_denial(receiver) -> None:
    receiver.engine.executor = ImmediateExecutor()
    new_key = key(2)
    new_id = client_id_for_public_key(new_key)

    def plan_access(operation_id: str, requester: str, action: str, parameters: dict):
        return receiver.engine.handle(
            request(
                "plan-access",
                operation_id,
                requester,
                {
                    "action": action,
                    "parameters": parameters,
                    "target": {"logical_id": "drone", "profile": "real"},
                },
            )
        )

    added = plan_access(
        "access-add-0001",
        receiver.operator_id,
        "access-add",
        {"phase": "add", "client_id": new_id, "public_key": new_key},
    )
    receiver.engine.handle(
        request(
            "access-add",
            "access-add-0001",
            receiver.operator_id,
            {"plan": added["plan"]},
            added["nonce"],
        )
    )
    proved = plan_access(
        "access-prove-0001",
        new_id,
        "access-add",
        {"phase": "prove", "client_id": new_id, "public_key": new_key},
    )
    receiver.engine.handle(
        request(
            "access-add",
            "access-prove-0001",
            new_id,
            {"plan": proved["plan"]},
            proved["nonce"],
        )
    )
    revoked = plan_access(
        "access-revoke-0001",
        new_id,
        "access-revoke",
        {"client_id": receiver.operator_id},
    )
    receiver.engine.handle(
        request(
            "access-revoke",
            "access-revoke-0001",
            new_id,
            {"plan": revoked["plan"]},
            revoked["nonce"],
        )
    )
    assert {item["client_id"]: item["state"] for item in receiver.access.list_clients()} == {
        receiver.operator_id: "revoked",
        new_id: "active",
    }
    final = plan_access(
        "access-revoke-0002", new_id, "access-revoke", {"client_id": new_id}
    )
    receiver.engine.handle(
        request(
            "access-revoke",
            "access-revoke-0002",
            new_id,
            {"plan": final["plan"]},
            final["nonce"],
        )
    )
    assert receiver.journals.load("access-revoke-0002")["state"] == "failed"
    listed = {item["client_id"]: item["state"] for item in receiver.access.list_clients()}
    assert listed[new_id] == "active"
    raw_audit = (receiver.root / "log/audit.jsonl").read_text(encoding="utf-8")
    assert new_key not in raw_audit and "public_key" not in raw_audit
