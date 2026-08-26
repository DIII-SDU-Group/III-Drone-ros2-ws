from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json
from iii_deployment.log_lifecycle import LogInventory, LogTransferStore
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.receiver.access import AccessManager, client_id_for_public_key
from iii_deployment.receiver.engine import ReceiverEngine
from iii_deployment.receiver.clock import ClockController
from iii_deployment.receiver.protocol import Action, Request
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    ReceiverControlStore,
)
from iii_deployment.staging import StageResult

REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


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
        return {
            "recovery": {"recovery_only": False, "flight_capable": True, "reason": None}
        }

    def stage(self, component: Path, *, status_index, staged_at: str) -> StageResult:
        self.stage_calls.append((component, status_index, staged_at))
        release_id = __import__("json").loads(
            (component / "release-manifest.json").read_text(encoding="utf-8")
        )["release_id"]
        return StageResult(
            release_id, "field-development", True, release_id, 1000, "9" * 64
        )


def key(character: int) -> str:
    blob = (
        struct.pack(">I", 11)
        + b"ssh-ed25519"
        + struct.pack(">I", 32)
        + bytes([character]) * 32
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


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
        registry=REGISTRY,
    )
    operator_key = key(1)
    operator_id = client_id_for_public_key(operator_key)
    access.bootstrap([enrollment(operator_key, 1)])
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

    def build(
        selected_executor=executor,
        activation_coordinator=None,
        clock_controller=None,
        log_inventory=None,
        log_transfer=None,
    ):
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
            activation_coordinator=activation_coordinator,
            clock_controller=clock_controller,
            log_inventory=log_inventory,
            log_transfer=log_transfer,
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


def test_clock_sync_uses_receiver_plan_nonce_and_detached_journal(receiver) -> None:
    wall = [1_000_000_000]
    monotonic = [10_000_000_000]
    starts = []
    controller = ClockController(
        receiver.root / "state/clock-state.json",
        boot_id=lambda: "boot-a",
        monotonic_ns=lambda: monotonic[0],
        wall_ns=lambda: wall[0],
        set_wall_ns=lambda value: wall.__setitem__(0, value),
        gate_opened=lambda: starts.append(True) or {"started": True},
    )
    engine = receiver.build(clock_controller=controller)
    samples = [
        {
            "target_boot_id": "boot-a",
            "target_monotonic_ns": monotonic[0] + index,
            "target_wall_ns": wall[0] + index,
            "operator_midpoint_utc_ns": 2_000_000_000 + index,
            "rtt_ns": 10_000_000 + index,
            "offset_ns": -1_000_000_000,
        }
        for index in range(5)
    ]
    operation_id = "operation-clock-0001"
    planned = engine.handle(
        request(
            "plan-clock-sync",
            operation_id,
            receiver.operator_id,
            {
                "samples": samples,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    accepted = engine.handle(
        request(
            "clock-sync",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    assert accepted["operation"]["state"] == "accepted"
    receiver.executor.run_next()
    status = engine.handle(request("status", operation_id, receiver.operator_id, {}))
    assert status["operation"]["state"] == "completed"
    assert status["operation"]["result"]["gate"] == "OPERATIONAL"
    assert status["clock"]["gate"] == "OPERATIONAL"
    assert status["live_state"]["profile"] == "real"
    assert starts == [True]


def test_clock_fault_blocks_new_receiver_mutations_but_keeps_status(
    receiver,
) -> None:
    controller = SimpleNamespace(
        status=lambda: {
            "schema": "iii.receiver-clock-status/v1",
            "gate": "CLOCK_FAULT_ACTIVE",
        }
    )
    receiver.engine = receiver.build(clock_controller=controller)
    status = receiver.engine.handle(
        request("status", "operation-status-0001", receiver.operator_id, {})
    )
    assert status["clock"]["gate"] == "CLOCK_FAULT_ACTIVE"
    with pytest.raises(ContractError, match="blocks every new receiver mutation"):
        stage_plan(receiver, "operation-stage-fault")


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
    (original / "bundle.tar.zst").write_bytes(
        b"attacker changed upload after acceptance"
    )
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


class FakeActivationDiagnostics:
    def __init__(self) -> None:
        self.states = {}

    def load_state(self, operation_id):
        return self.states.get(operation_id)


class FakeActivationCoordinator:
    def __init__(self) -> None:
        self.calls = []
        self.diagnostics = FakeActivationDiagnostics()

    def preflight(
        self,
        *,
        release_id,
        configuration_checkpoint_id,
        operator_rollback=False,
    ):
        self.calls.append(
            (
                "preflight",
                release_id,
                configuration_checkpoint_id,
                operator_rollback,
            )
        )
        return {
            "schema": "iii.activation-preflight/v1",
            "ready": True,
            "rejection_reasons": [],
        }

    def activate(
        self,
        *,
        operation_id,
        release_id,
        configuration_checkpoint_id,
        explicit_qualified_action,
    ):
        self.calls.append(
            (
                "activate",
                operation_id,
                release_id,
                configuration_checkpoint_id,
                explicit_qualified_action,
            )
        )
        return {
            "kind": "activation",
            "release_id": release_id,
            "automatic_rollback_permitted": False,
            "autonomy_started": False,
        }

    def operator_rollback(
        self,
        *,
        operation_id,
        release_id,
        configuration_checkpoint_id,
    ):
        self.calls.append(
            (
                "rollback",
                operation_id,
                release_id,
                configuration_checkpoint_id,
            )
        )
        return {
            "kind": "rollback",
            "release_id": release_id,
            "automatic_rollback_permitted": False,
            "autonomy_started": False,
        }

    @staticmethod
    def reconcile():
        return {
            "schema": "iii.activation-reconciliation/v1",
            "restored_operations": [],
            "accepted_operations": [],
            "faulted_operations": [],
            "autonomy_started": False,
        }


def test_activation_is_planned_rechecked_durably_detached_and_runs_without_client(
    receiver,
) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-activate-0001"
    release_id = "4" * 64
    checkpoint_id = "5" * 64
    planned = receiver.engine.handle(
        request(
            "plan-activate",
            operation_id,
            receiver.operator_id,
            {
                "activation": {
                    "release_id": release_id,
                    "configuration_checkpoint_id": checkpoint_id,
                    "explicit_qualified_action": False,
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    assert planned["preflight"]["ready"] is True
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    accepted = receiver.engine.handle(
        request(
            "activate",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    assert accepted["detached"] is True
    assert receiver.journals.load(operation_id)["state"] == "accepted"
    del accepted
    receiver.executor.run_next()
    completed = receiver.journals.load(operation_id)
    assert completed["state"] == "completed"
    assert completed["result"]["autonomy_started"] is False
    assert coordinator.calls == [
        ("preflight", release_id, checkpoint_id, False),
        ("preflight", release_id, checkpoint_id, False),
        ("activate", operation_id, release_id, checkpoint_id, False),
    ]


def test_operator_rollback_is_planned_safety_rechecked_and_detached(receiver) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-rollback-0001"
    release_id = "6" * 64
    checkpoint_id = "7" * 64
    planned = receiver.engine.handle(
        request(
            "plan-rollback",
            operation_id,
            receiver.operator_id,
            {
                "rollback": {
                    "release_id": release_id,
                    "configuration_checkpoint_id": checkpoint_id,
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    assert planned["plan"]["action"] == "rollback"
    assert planned["preflight"]["ready"] is True
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    accepted = receiver.engine.handle(
        request(
            "rollback",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    assert accepted["detached"] is True
    assert receiver.journals.load(operation_id)["state"] == "accepted"
    receiver.executor.run_next()
    completed = receiver.journals.load(operation_id)
    assert completed["state"] == "completed"
    assert completed["result"]["kind"] == "rollback"
    assert completed["result"]["automatic_rollback_permitted"] is False
    assert coordinator.calls == [
        ("preflight", release_id, checkpoint_id, True),
        ("preflight", release_id, checkpoint_id, True),
        ("rollback", operation_id, release_id, checkpoint_id),
    ]


def test_reboot_reconciles_durably_accepted_operator_rollback_journal(receiver) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-rollback-0002"
    release_id = "8" * 64
    checkpoint_id = "9" * 64
    planned = receiver.engine.handle(
        request(
            "plan-rollback",
            operation_id,
            receiver.operator_id,
            {
                "rollback": {
                    "release_id": release_id,
                    "configuration_checkpoint_id": checkpoint_id,
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    receiver.engine.handle(
        request(
            "rollback",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    coordinator.diagnostics.states[operation_id] = {
        "stage": "accepted",
        "candidate": {"release_id": release_id},
        "previous": {"release_id": "a" * 64},
        "accepted_state_id": "b" * 64,
        "evidence_id": "c" * 64,
        "state_id": "d" * 64,
    }
    reconciled = receiver.engine.reconcile()
    assert reconciled["recovered_operations"] == [operation_id]
    journal = receiver.journals.load(operation_id)
    assert journal["state"] == "completed"
    assert journal["result"]["kind"] == "rollback"
    assert journal["result"]["automatic_rollback_permitted"] is False
    assert receiver.control.load()["lease"] is None
    receiver.executor.run_next()
    assert receiver.journals.load(operation_id)["state"] == "completed"


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


def test_unplanned_or_oversized_upload_is_rejected_before_durable_acceptance(
    receiver,
) -> None:
    planned = stage_plan(receiver)
    component = receiver.root / "incoming" / ("e" * 64) / "drone"
    (component / "arbitrary-unit.service").write_text(
        "host injection", encoding="utf-8"
    )
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


def test_key_rotation_through_planned_receiver_actions_and_final_key_loss_denial(
    receiver,
) -> None:
    receiver.engine.executor = ImmediateExecutor()
    new_key = key(2)
    new_id = client_id_for_public_key(new_key)
    new_enrollment = enrollment(new_key, 2)

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
        {"phase": "add", "enrollment": new_enrollment},
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
        {"phase": "prove", "enrollment": new_enrollment},
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
        {
            "authority": "machine",
            "machine_id": enrollment(key(1), 1)["machine_id"],
        },
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
    assert {
        item["client_id"]: item["state"] for item in receiver.access.list_clients()
    } == {
        receiver.operator_id: "revoked",
        new_id: "active",
    }
    final = plan_access(
        "access-revoke-0002",
        new_id,
        "access-revoke",
        {"authority": "machine", "machine_id": new_enrollment["machine_id"]},
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
    listed = {
        item["client_id"]: item["state"] for item in receiver.access.list_clients()
    }
    assert listed[new_id] == "active"
    raw_audit = (receiver.root / "log/audit.jsonl").read_text(encoding="utf-8")
    assert new_key not in raw_audit and "public_key" not in raw_audit


def test_pending_machine_can_poll_only_its_own_proof_operation(receiver) -> None:
    receiver.engine.executor = ImmediateExecutor()
    new_key = key(2)
    new_id = client_id_for_public_key(new_key)
    new_enrollment = enrollment(new_key, 2)
    added = receiver.engine.handle(
        request(
            "plan-access",
            "access-add-poll-0001",
            receiver.operator_id,
            {
                "action": "access-add",
                "parameters": {"phase": "add", "enrollment": new_enrollment},
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    receiver.engine.handle(
        request(
            "access-add",
            "access-add-poll-0001",
            receiver.operator_id,
            {"plan": added["plan"]},
            added["nonce"],
        )
    )

    receiver.engine.executor = receiver.executor
    proof = receiver.engine.handle(
        request(
            "plan-access",
            "access-proof-poll-0001",
            new_id,
            {
                "action": "access-add",
                "parameters": {"phase": "prove", "enrollment": new_enrollment},
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    receiver.engine.handle(
        request(
            "access-add",
            "access-proof-poll-0001",
            new_id,
            {"plan": proof["plan"]},
            proof["nonce"],
        )
    )
    polled = receiver.engine.handle(
        request("status", "access-proof-poll-0001", new_id, {})
    )
    assert polled["pending_enrollment_proof"] is True
    assert polled["operation"]["state"] == "accepted"
    with pytest.raises(ContractError, match="only its own"):
        receiver.engine.handle(request("status", "unknown-proof-0001", new_id, {}))

    receiver.executor.run_next()
    completed = receiver.engine.handle(
        request("status", "access-proof-poll-0001", new_id, {})
    )
    assert completed["operation"]["state"] == "completed"


def test_receiver_log_pull_receipt_and_exact_prune_flow(receiver) -> None:
    log_root = receiver.root / "var/log/iii"
    log_root.mkdir(parents=True)
    content = log_root / "host.jsonl"
    content.write_bytes(b'{"event":"boot"}\n')
    transfer = LogTransferStore(
        source_root=receiver.root,
        state_root=receiver.root / "state/log-transfer",
    )
    inventory = LogInventory(
        source_root=receiver.root,
        logs_root=log_root,
        deployment_state_root=receiver.root / "state",
        activation_root=receiver.root / "state/activation",
        audit_path=receiver.root / "log/audit.jsonl",
        transfer=transfer,
        active_operation_ids=lambda: (),
        retained_release_ids=lambda: (),
        audit_operation_ids=lambda: (),
    )
    engine = receiver.build(
        selected_executor=ImmediateExecutor(),
        log_inventory=inventory,
        log_transfer=transfer,
    )
    exported = engine.handle(
        request(
            "log-export", "logs-export-0001", receiver.operator_id, {"domain": "logs"}
        )
    )
    manifest = exported["manifest"]
    assert [item["locator"] for item in manifest["files"]] == ["var/log/iii/host.jsonl"]
    item = manifest["files"][0]
    chunk = engine.handle(
        request(
            "log-chunk",
            "logs-chunk-0001",
            receiver.operator_id,
            {
                "manifest_id": manifest["manifest_id"],
                "content_id": item["content_id"],
                "offset": 0,
                "length": 512,
            },
        )
    )["chunk"]
    assert base64.b64decode(chunk["data"]) == content.read_bytes()
    verified = [
        {
            "locator": item["locator"],
            "content_id": item["content_id"],
            "size": item["size"],
        }
    ]
    receipt_plan = engine.handle(
        request(
            "plan-log-receipt",
            "logs-receipt-0001",
            receiver.operator_id,
            {
                "manifest_id": manifest["manifest_id"],
                "verified_files": verified,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("receiver-mutation-plan", receipt_plan["plan"])
    engine.handle(
        request(
            "log-receipt",
            "logs-receipt-0001",
            receiver.operator_id,
            {"plan": receipt_plan["plan"]},
            receipt_plan["nonce"],
        )
    )
    receipt = receiver.journals.load("logs-receipt-0001")["result"]["receipt"]
    prune_plan = engine.handle(
        request(
            "plan-log-prune",
            "logs-prune-0001",
            receiver.operator_id,
            {
                "receipt_id": receipt["receipt_id"],
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("receiver-mutation-plan", prune_plan["plan"])
    assert prune_plan["plan"]["parameters"]["prune_plan"]["remove"] == [item]
    engine.handle(
        request(
            "log-prune",
            "logs-prune-0001",
            receiver.operator_id,
            {"plan": prune_plan["plan"]},
            prune_plan["nonce"],
        )
    )
    assert receiver.journals.load("logs-prune-0001")["result"]["removed"] == [
        "var/log/iii/host.jsonl"
    ]
    assert not content.exists()
