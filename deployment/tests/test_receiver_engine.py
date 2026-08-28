from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
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
from iii_deployment.receiver.server import assert_reconciliation_boot_safe
from iii_deployment.receiver.upload import RECEIVER_UPDATE_FILES
from iii_deployment.staging import StageResult

REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


def px4_evidence(release_id: str, manifest_id: str = "d" * 64) -> dict:
    target = {
        "system_id": 1,
        "component_id": 1,
        "armed": False,
        "firmware_version": "1.16.0",
        "firmware_commit": "0123456789",
    }
    parameters = [
        {"name": "SYS_AUTOSTART", "mav_type": "UINT32", "value": 4001, "index": 0}
    ]
    snapshot = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": content_identity(
            {
                "profile": "real",
                "target": target,
                "parameter_count": 1,
                "parameters": parameters,
            }
        ),
        "captured_at": "2026-08-27T12:00:00Z",
        "profile": "real",
        "provenance": "qgc-forwarded-mavlink-observation",
        "target": target,
        "complete": True,
        "parameter_count": 1,
        "parameters": parameters,
    }
    comparison = {
        "schema": "iii.px4-parameter-comparison/v1",
        "profile": "real",
        "manifest_id": manifest_id,
        "snapshot_id": snapshot["snapshot_id"],
        "inventory_complete": True,
        "missing": [],
        "unexpected": [],
        "drift": {"release-required": [], "operator-tunable": []},
        "preserved_calibration_identity": [],
        "required_match": True,
    }
    evidence = {
        "schema": "iii.px4-activation-evidence/v1",
        "evidence_id": "0" * 64,
        "captured_at": "2026-08-27T12:00:00Z",
        "release_id": release_id,
        "profile": "real",
        "manifest_id": manifest_id,
        "snapshot": snapshot,
        "comparison": comparison,
        "healthy": True,
        "writes_performed": 0,
    }
    evidence["evidence_id"] = content_identity(
        {key: value for key, value in evidence.items() if key != "evidence_id"}
    )
    return evidence


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


class FakeReceiverSlots:
    def __init__(self, receiver_id: str = "f" * 64, generation: int = 2) -> None:
        self.receiver_id = receiver_id
        self.generation = generation
        self.state = None

    def verify_update(self, bundle: Path):
        assert {path.name for path in bundle.iterdir()} == {
            "receiver-update.manifest.json",
            "receiver-update.sig.json",
            "receiver-update.tar",
        }
        return SimpleNamespace(
            manifest={"receiver_id": self.receiver_id, "generation": self.generation}
        )

    def stage(self, _bundle: Path, *, operation_id: str, client_id: str):
        self.state = {
            "schema": "iii.receiver-update-state/v1",
            "state_id": "7" * 64,
            "operation_id": operation_id,
            "client_id": client_id,
            "candidate_receiver_id": self.receiver_id,
            "candidate_generation": self.generation,
            "stage": "staged",
            "failure": None,
            "readiness": None,
        }
        return self.state

    def update_state(self):
        return self.state

    def abort_staged(self, *, operation_id: str, client_id: str, reason: str):
        assert self.state["operation_id"] == operation_id
        assert self.state["client_id"] == client_id
        assert self.state["stage"] == "staged"
        self.state.update(stage="reverted", failure=reason)
        return self.state


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
        host_maintenance=None,
        hardware_inspector=None,
        host_inspector=None,
        network_controller=None,
        backup_controller=None,
        receiver_slots=None,
        receiver_generation=None,
        prepare_receiver_handoff=None,
        schedule_receiver_handoff=None,
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
            host_maintenance=host_maintenance,
            hardware_inspector=hardware_inspector,
            host_inspector=host_inspector,
            network_controller=network_controller,
            backup_controller=backup_controller,
            receiver_slots=receiver_slots,
            receiver_generation=receiver_generation,
            prepare_receiver_handoff=prepare_receiver_handoff,
            schedule_receiver_handoff=schedule_receiver_handoff,
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


class FakeHostMaintenance:
    def __init__(self) -> None:
        self.phase = "reboot-required"
        self.maintenance_id = "9" * 64
        self.transaction_id = "8" * 64

    def plan(self, *, operation_id, client_id, request, live_state):
        value = {
            "schema": "iii.host-maintenance-plan/v1",
            "maintenance_id": "0" * 64,
            "operation_id": operation_id,
            "client_id": client_id,
            "request": dict(request),
            "before": {"snapshot_id": "7" * 64},
            "installed_policy_id": "6" * 64,
            "playbook_sha256": "5" * 64,
            "executor_sha256": "4" * 64,
            "expected_package_changes": [],
            "trust_change": None,
            "boot_change": None,
            "mutations": [],
            "required_checks": ["fixed receiver lease"],
            "declared_permissions": [],
            "reboot_expected": False,
            "no_change": True,
        }
        value["maintenance_id"] = hashlib.sha256(
            canonical_json(
                {key: item for key, item in value.items() if key != "maintenance_id"}
            )
        ).hexdigest()
        self.maintenance_id = value["maintenance_id"]
        return value

    def plan_reboot(self, *, operation_id, client_id, maintenance_id):
        if maintenance_id != self.maintenance_id:
            raise ContractError("wrong maintenance")
        return {"maintenance_id": maintenance_id}

    def assert_mutation_allowed(self, _action):
        return None

    def apply(self, parameters):
        self.maintenance_id = parameters["maintenance_id"]
        return self.status()["transaction"]

    def schedule_reboot(self, maintenance_id):
        self.maintenance_id = maintenance_id
        self.phase = "reboot-scheduled"
        return self.status()["transaction"]

    def reconcile(self):
        return {
            "schema": "iii.host-maintenance-reconcile/v1",
            "maintenance_id": self.maintenance_id,
            "state": self.phase,
            "transaction_id": self.transaction_id,
        }

    def status(self):
        return {
            "schema": "iii.host-maintenance-status/v1",
            "transaction": {
                "maintenance_id": self.maintenance_id,
                "transaction_id": self.transaction_id,
                "phase": self.phase,
                "reboot": {"required": True},
                "commissioning": {"state": "unchanged", "reasons": []},
                "failure": (
                    {
                        "code": "postboot-failed",
                        "message": "failed",
                        "recommendation": "reimage",
                    }
                    if self.phase == "failed"
                    else None
                ),
            },
            "mutation_blocked": self.phase not in {"completed", "failed"},
            "recovery_recommendation": "reimage" if self.phase == "failed" else None,
        }


class FakeNetworkController:
    def __init__(self) -> None:
        self.transactions: dict[str, dict] = {}

    def plan(self, *, operation_id, client_id, profile):
        redacted = {
            "ethernet_dhcp4": True,
            "wifi_profile_ids": [
                hashlib.sha256(row["ssid"].encode()).hexdigest()
                for row in profile["wifi"]
            ],
            "wifi_profile_count": len(profile["wifi"]),
            "onboard_access_point": False,
        }
        value = {
            "schema": "iii.network-plan/v1",
            "network_id": "0" * 64,
            "operation_id": operation_id,
            "client_id": client_id,
            "candidate_sha256": hashlib.sha256(canonical_json(profile)).hexdigest(),
            "desired_netplan_sha256": "1" * 64,
            "previous_netplan_sha256": "2" * 64,
            "profile": redacted,
            "connectivity_impacting": True,
            "confirmation_deadline_s": 90,
            "no_change": False,
            "declared_permissions": ["/etc/netplan/90-iii-operator.yaml"],
            "required_checks": ["Ethernet DHCP remains enabled"],
        }
        value["network_id"] = hashlib.sha256(
            canonical_json(
                {key: item for key, item in value.items() if key != "network_id"}
            )
        ).hexdigest()
        return value

    def resume_after_transaction(self):
        return None

    def claim(self, plan, profile):
        assert plan == self.plan(
            operation_id=plan["operation_id"],
            client_id=plan["client_id"],
            profile=profile,
        )
        self.transactions[plan["operation_id"]] = {
            "operation_id": plan["operation_id"],
            "network_id": plan["network_id"],
            "client_id": plan["client_id"],
            "state": "claimed",
            "deadline_monotonic_ns": None,
        }

    def apply(self, plan):
        transaction = self.transactions[plan["operation_id"]]
        transaction["state"] = "pending-confirmation"
        transaction["deadline_monotonic_ns"] = 100_000_000_000
        return {
            "kind": "network",
            "network_id": plan["network_id"],
            "state": "pending-confirmation",
            "confirmation_required": True,
            "confirmation_deadline_s": 90,
            "deadline_monotonic_ns": transaction["deadline_monotonic_ns"],
        }

    def status(self, operation_id):
        return {"schema": "iii.network-status/v1", **self.transactions[operation_id]}

    def confirm(self, operation_id, *, client_id, network_id):
        transaction = self.transactions[operation_id]
        assert transaction["client_id"] == client_id
        assert transaction["network_id"] == network_id
        transaction["state"] = "confirmed"
        return {
            "kind": "network-confirm",
            "network_id": network_id,
            "state": "confirmed",
        }

    def reconcile(self):
        return {"schema": "iii.network-status/v1", "recovered": []}


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


def test_receiver_update_is_claimed_handed_off_and_completed_by_new_generation(
    receiver, monkeypatch,
) -> None:
    receiver_id = "f" * 64
    slots = FakeReceiverSlots(receiver_id=receiver_id, generation=2)
    prepared = []
    scheduled = []
    engine = receiver.build(
        receiver_slots=slots,
        receiver_generation=1,
        prepare_receiver_handoff=lambda: prepared.append(True),
        schedule_receiver_handoff=lambda: scheduled.append(True),
    )
    bundle = receiver.root / "incoming/receiver-updates" / receiver_id / "bundle"
    bundle.mkdir(parents=True)
    for name in (
        "receiver-update.manifest.json",
        "receiver-update.sig.json",
        "receiver-update.tar",
    ):
        (bundle / name).write_text(name + "\n", encoding="utf-8")
    archive_sha256 = hashlib.sha256(
        (bundle / "receiver-update.tar").read_bytes()
    ).hexdigest()
    files = [
        {
            "path": f"bundle/{path.name}",
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(bundle.iterdir())
    ]
    upload = {
        "schema": "iii.receiver-update-upload/v1",
        "upload_id": "0" * 64,
        "receiver_id": receiver_id,
        "client_id": receiver.operator_id,
        "files": files,
    }
    upload["upload_id"] = content_identity(
        {key: item for key, item in upload.items() if key != "upload_id"}
    )
    (bundle.parent / ".upload-manifest.json").write_bytes(
        canonical_json(upload) + b"\n"
    )
    operation_id = "receiver-update-0001"
    planned = engine.handle(
        request(
            "plan-receiver-update",
            operation_id,
            receiver.operator_id,
            {
                "artifact": {
                    "receiver_id": receiver_id,
                    "generation": 2,
                    "archive_sha256": archive_sha256,
                    "upload_id": upload["upload_id"],
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "iii_deployment.receiver.engine.shutil.disk_usage",
            lambda _path: SimpleNamespace(total=100, free=0),
        )
        with pytest.raises(ContractError, match="preserve reserve"):
            engine._claim_receiver_update(planned["plan"])
    accepted = engine.handle(
        request(
            "receiver-update",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    assert accepted["operation"]["state"] == "accepted"
    receiver.executor.run_next()
    assert prepared == [True]
    assert scheduled == [True]
    assert receiver.journals.load(operation_id)["state"] == "running"

    slots.state.update(
        stage="committed",
        state_id="8" * 64,
        readiness={"schema": "iii.receiver-readiness/v1"},
    )
    next_engine = receiver.build(
        receiver_slots=slots,
        receiver_generation=2,
        prepare_receiver_handoff=lambda: None,
        schedule_receiver_handoff=lambda: None,
    )
    recovery = next_engine.reconcile()
    assert recovery["recovered_operations"] == [operation_id]
    terminal = receiver.journals.load(operation_id)
    assert terminal["state"] == "completed"
    assert terminal["result"]["generation"] == 2
    assert receiver.control.receiver_generation == 2


def test_receiver_update_schedule_failure_aborts_before_selector_and_releases_lease(
    receiver,
) -> None:
    receiver_id = "e" * 64
    slots = FakeReceiverSlots(receiver_id=receiver_id, generation=2)
    engine = receiver.build(
        receiver_slots=slots,
        receiver_generation=1,
        prepare_receiver_handoff=lambda: None,
        schedule_receiver_handoff=lambda: (_ for _ in ()).throw(
            RuntimeError("systemd unavailable")
        ),
    )
    bundle = receiver.root / "incoming/receiver-updates" / receiver_id / "bundle"
    bundle.mkdir(parents=True)
    for name in RECEIVER_UPDATE_FILES:
        (bundle / name).write_text(name + "\n", encoding="utf-8")
    files = [
        {
            "path": f"bundle/{path.name}",
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(bundle.iterdir())
    ]
    upload = {
        "schema": "iii.receiver-update-upload/v1",
        "upload_id": "0" * 64,
        "receiver_id": receiver_id,
        "client_id": receiver.operator_id,
        "files": files,
    }
    upload["upload_id"] = content_identity(
        {key: value for key, value in upload.items() if key != "upload_id"}
    )
    (bundle.parent / ".upload-manifest.json").write_bytes(
        canonical_json(upload) + b"\n"
    )
    operation_id = "receiver-update-schedule-failure"
    planned = engine.handle(
        request(
            "plan-receiver-update",
            operation_id,
            receiver.operator_id,
            {
                "artifact": {
                    "receiver_id": receiver_id,
                    "generation": 2,
                    "archive_sha256": hashlib.sha256(
                        (bundle / "receiver-update.tar").read_bytes()
                    ).hexdigest(),
                    "upload_id": upload["upload_id"],
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    engine.handle(
        request(
            "receiver-update",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )

    receiver.executor.run_next()

    journal = receiver.journals.load(operation_id)
    assert journal["state"] == "failed"
    assert "could not be scheduled" in journal["failure"]["message"]
    assert slots.state["stage"] == "reverted"
    assert receiver.control.load()["lease"] is None


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


def test_status_exposes_authenticated_active_manifest_and_activation_safety(receiver):
    manifest = {
        "schema_version": "1",
        "release_id": "a" * 64,
        "compatibility": {
            "api_ranges": {"runtime_api": ">=2.0.0,<3.0.0"},
            "schema_ranges": {"configuration": ">=1.0.0,<2.0.0"},
        },
    }
    safety = {
        "schema": "iii.activation-safety/v1",
        "profile": "real",
        "armed": False,
        "in_air": False,
        "continuously_safe_for_s": 3.5,
    }
    receiver.store.active_release_manifest = lambda: manifest

    class Observation:
        def validate(self):
            return None

        def as_document(self):
            return safety

    engine = receiver.build(
        activation_coordinator=SimpleNamespace(safety_provider=lambda: Observation())
    )

    result = engine.handle(
        request("status", "status-paired-update", receiver.operator_id, {})
    )

    assert result["active_release_manifest"] == manifest
    assert result["activation_safety"] == safety


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
        px4_activation_evidence,
        operator_rollback=False,
        configuration_reconciliation_decisions=None,
    ):
        call = (
            "preflight",
            release_id,
            configuration_checkpoint_id,
            operator_rollback,
            px4_activation_evidence["evidence_id"],
        )
        self.calls.append(
            call
            if configuration_reconciliation_decisions is None
            else (*call, dict(configuration_reconciliation_decisions))
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
        px4_activation_evidence,
        configuration_reconciliation_decisions=None,
    ):
        call = (
            "activate",
            operation_id,
            release_id,
            configuration_checkpoint_id,
            explicit_qualified_action,
            px4_activation_evidence["evidence_id"],
        )
        self.calls.append(
            call
            if configuration_reconciliation_decisions is None
            else (*call, dict(configuration_reconciliation_decisions))
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
        px4_activation_evidence,
    ):
        self.calls.append(
            (
                "rollback",
                operation_id,
                release_id,
                configuration_checkpoint_id,
                px4_activation_evidence["evidence_id"],
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
    evidence = px4_evidence(release_id)
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
                    "px4_activation_evidence": evidence,
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
        ("preflight", release_id, checkpoint_id, False, evidence["evidence_id"]),
        ("preflight", release_id, checkpoint_id, False, evidence["evidence_id"]),
        (
            "activate",
            operation_id,
            release_id,
            checkpoint_id,
            False,
            evidence["evidence_id"],
        ),
    ]


def test_activation_plan_binds_and_forwards_configuration_review_decisions(
    receiver,
) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-activate-review-0001"
    release_id = "a" * 64
    checkpoint_id = "b" * 64
    evidence = px4_evidence(release_id)
    decisions = {"tracked/default.yaml:/review_fixture/gain": "use_old"}
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
                    "px4_activation_evidence": evidence,
                    "configuration_reconciliation_decisions": decisions,
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    assert (
        planned["plan"]["parameters"]["configuration_reconciliation_decisions"]
        == decisions
    )
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    receiver.engine.handle(
        request(
            "activate",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    receiver.executor.run_next()
    assert coordinator.calls == [
        (
            "preflight",
            release_id,
            checkpoint_id,
            False,
            evidence["evidence_id"],
            decisions,
        ),
        (
            "preflight",
            release_id,
            checkpoint_id,
            False,
            evidence["evidence_id"],
            decisions,
        ),
        (
            "activate",
            operation_id,
            release_id,
            checkpoint_id,
            False,
            evidence["evidence_id"],
            decisions,
        ),
    ]


def test_activation_plan_rejects_hostile_configuration_decision_keys(receiver) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    release_id = "c" * 64
    with pytest.raises(ContractError, match="decision key is invalid"):
        receiver.engine.handle(
            request(
                "plan-activate",
                "operation-hostile-review-0001",
                receiver.operator_id,
                {
                    "activation": {
                        "release_id": release_id,
                        "configuration_checkpoint_id": "d" * 64,
                        "explicit_qualified_action": False,
                        "px4_activation_evidence": px4_evidence(release_id),
                        "configuration_reconciliation_decisions": {
                            "bad key:/bad": "use_old"
                        },
                    },
                    "target": {"logical_id": "drone", "profile": "real"},
                },
            )
        )
    assert coordinator.calls == []


def test_operator_rollback_is_planned_safety_rechecked_and_detached(receiver) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-rollback-0001"
    release_id = "6" * 64
    checkpoint_id = "7" * 64
    evidence = px4_evidence(release_id)
    planned = receiver.engine.handle(
        request(
            "plan-rollback",
            operation_id,
            receiver.operator_id,
            {
                "rollback": {
                    "release_id": release_id,
                    "configuration_checkpoint_id": checkpoint_id,
                    "px4_activation_evidence": evidence,
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
        ("preflight", release_id, checkpoint_id, True, evidence["evidence_id"]),
        ("preflight", release_id, checkpoint_id, True, evidence["evidence_id"]),
        (
            "rollback",
            operation_id,
            release_id,
            checkpoint_id,
            evidence["evidence_id"],
        ),
    ]


def test_reboot_reconciles_durably_accepted_operator_rollback_journal(receiver) -> None:
    coordinator = FakeActivationCoordinator()
    receiver.engine = receiver.build(receiver.executor, coordinator)
    operation_id = "operation-rollback-0002"
    release_id = "8" * 64
    checkpoint_id = "9" * 64
    evidence = px4_evidence(release_id)
    planned = receiver.engine.handle(
        request(
            "plan-rollback",
            operation_id,
            receiver.operator_id,
            {
                "rollback": {
                    "release_id": release_id,
                    "configuration_checkpoint_id": checkpoint_id,
                    "px4_activation_evidence": evidence,
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


def test_host_maintenance_uses_retained_plan_lease_and_extended_deadline(
    receiver,
) -> None:
    host = FakeHostMaintenance()
    engine = receiver.build(host_maintenance=host)
    operation_id = "host-maintenance-0001"
    planned = engine.handle(
        request(
            "plan-host-maintenance",
            operation_id,
            receiver.operator_id,
            {
                "request": {"schema": "fixture-host-request"},
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])

    accepted = engine.handle(
        request(
            "host-maintenance",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )

    assert accepted["operation"]["deadlines"] == {
        "target_acceptance_s": 1800,
        "hard_deadline_s": 7200,
        "rollback_target_s": 60,
    }
    assert receiver.control.load()["lease"]["operation_id"] == operation_id
    competing = engine.handle(
        request(
            "plan-host-reboot",
            "host-reboot-blocked",
            receiver.operator_id,
            {
                "maintenance_id": host.maintenance_id,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    with pytest.raises(ContractError, match="lease is held"):
        engine.handle(
            request(
                "host-reboot",
                "host-reboot-blocked",
                receiver.operator_id,
                {"plan": competing["plan"]},
                competing["nonce"],
            )
        )


@pytest.mark.parametrize(
    "postboot_phase,expected_state", [("completed", "completed"), ("failed", "failed")]
)
def test_host_reboot_journal_reconciles_postboot_validation(
    receiver, postboot_phase: str, expected_state: str
) -> None:
    host = FakeHostMaintenance()
    engine = receiver.build(host_maintenance=host)
    operation_id = "host-reboot-reconcile"
    planned = engine.handle(
        request(
            "plan-host-reboot",
            operation_id,
            receiver.operator_id,
            {
                "maintenance_id": host.maintenance_id,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    engine.handle(
        request(
            "host-reboot",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    assert receiver.journals.load(operation_id)["state"] == "accepted"

    host.phase = postboot_phase
    restarted = receiver.build(
        selected_executor=QueuedExecutor(), host_maintenance=host
    )
    recovered = restarted.reconcile()

    journal = receiver.journals.load(operation_id)
    assert journal["state"] == expected_state
    assert receiver.control.load()["lease"] is None
    assert operation_id in recovered["recovered_operations"]
    if postboot_phase == "completed":
        assert journal["result"]["reconciled_after_boot"] is True
    else:
        assert journal["failure"]["code"] == "postboot-failed"


def test_host_reboot_execution_remains_nonterminal_until_postboot_reconcile(
    receiver,
) -> None:
    host = FakeHostMaintenance()
    engine = receiver.build(
        selected_executor=ImmediateExecutor(), host_maintenance=host
    )
    operation_id = "host-reboot-deferred"
    planned = engine.handle(
        request(
            "plan-host-reboot",
            operation_id,
            receiver.operator_id,
            {
                "maintenance_id": host.maintenance_id,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )

    engine.handle(
        request(
            "host-reboot",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )

    journal = receiver.journals.load(operation_id)
    assert journal["state"] == "running"
    assert journal["result"] is None
    assert receiver.control.load()["lease"]["operation_id"] == operation_id
    assert host.phase == "reboot-scheduled"


def test_failed_host_maintenance_reconciliation_blocks_normal_boot() -> None:
    with pytest.raises(ContractError, match="keep runtime stopped"):
        assert_reconciliation_boot_safe(
            {
                "schema": "iii.receiver-result/v1",
                "host_maintenance_recovery": {
                    "schema": "iii.host-maintenance-reconcile/v1",
                    "state": "failed",
                },
            }
        )

    assert_reconciliation_boot_safe(
        {
            "schema": "iii.receiver-result/v1",
            "host_maintenance_recovery": {
                "schema": "iii.host-maintenance-reconcile/v1",
                "state": "completed",
            },
        }
    )


def test_network_apply_is_redacted_nonterminal_and_bound_confirmation_completes(
    receiver,
) -> None:
    network = FakeNetworkController()
    engine = receiver.build(network_controller=network)
    operation_id = "network-operation-0001"
    profile = {
        "schema": "iii.operator-network-input/v1",
        "ethernet_dhcp4": True,
        "wifi": [{"ssid": "private-field", "password": "private-password"}],
    }
    planned = engine.handle(
        request(
            "network-plan",
            operation_id,
            receiver.operator_id,
            {
                "profile": profile,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    assert "private-field" not in str(planned["plan"])
    assert "private-password" not in str(planned["plan"])
    REGISTRY.validate("network-plan", planned["plan"]["parameters"])
    REGISTRY.validate("receiver-mutation-plan", planned["plan"])
    engine.handle(
        request(
            "network-apply",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"], "profile": profile},
            planned["nonce"],
        )
    )
    receiver.executor.run_next()
    pending_journal = receiver.journals.load(operation_id)
    assert pending_journal["state"] == "running"
    assert "private-field" not in str(pending_journal)
    assert "private-password" not in str(pending_journal)
    assert receiver.control.load()["lease"]["operation_id"] == operation_id

    confirmation_operation = "network-confirm-0001"
    confirmation = engine.handle(
        request(
            "network-confirm-plan",
            confirmation_operation,
            receiver.operator_id,
            {
                "target_operation_id": operation_id,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    REGISTRY.validate("network-confirmation-plan", confirmation["confirmation"])
    engine.handle(
        request(
            "network-confirm",
            confirmation_operation,
            receiver.operator_id,
            {"confirmation": confirmation["confirmation"]},
            confirmation["nonce"],
        )
    )
    journal = receiver.journals.load(operation_id)
    assert journal["state"] == "completed"
    assert journal["result"]["state"] == "confirmed"
    assert receiver.control.load()["lease"] is None


def test_network_automatic_reversion_is_terminalized_on_receiver_restart(
    receiver,
) -> None:
    network = FakeNetworkController()
    engine = receiver.build(network_controller=network)
    operation_id = "network-operation-0002"
    profile = {
        "schema": "iii.operator-network-input/v1",
        "ethernet_dhcp4": True,
        "wifi": [{"ssid": "lost-link", "password": "lost-password"}],
    }
    planned = engine.handle(
        request(
            "network-plan",
            operation_id,
            receiver.operator_id,
            {
                "profile": profile,
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    engine.handle(
        request(
            "network-apply",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"], "profile": profile},
            planned["nonce"],
        )
    )
    receiver.executor.run_next()
    network.transactions[operation_id]["state"] = "reverted"
    restarted = receiver.build(
        selected_executor=QueuedExecutor(), network_controller=network
    )
    result = restarted.reconcile()
    assert operation_id in result["recovered_operations"]
    assert receiver.journals.load(operation_id)["state"] == "failed"
    assert (
        receiver.journals.load(operation_id)["failure"]["code"]
        == "network-not-confirmed"
    )
    assert receiver.control.load()["lease"] is None


def test_authenticated_hardware_inspection_is_read_only_and_receiver_owned(receiver):
    report = {
        "schema": "iii.hardware-inspection/v1",
        "inspection_id": "1" * 64,
        "accepted": False,
    }

    class Inspector:
        def inspect(self):
            return report

    engine = receiver.build(hardware_inspector=Inspector())
    result = engine.handle(
        request(
            "hardware-inspect",
            "hardware-inspect-test",
            receiver.operator_id,
            {},
        )
    )
    assert result["inspection"] is report
    assert result["action"] == "hardware-inspect"
    assert receiver.control.load()["lease"] is None


def test_hardware_inspection_rejects_inactive_machine(receiver):
    engine = receiver.build(hardware_inspector=object())
    with pytest.raises(ContractError, match="not an active authorized operator"):
        engine.handle(
            request(
                "hardware-inspect",
                "hardware-inspect-unknown",
                "f" * 64,
                {},
            )
        )


def test_authenticated_composite_host_inspection_is_read_only(receiver):
    report = {
        "schema": "iii.host-inspection/v1",
        "inspection_id": "2" * 64,
        "accepted": False,
    }

    class Inspector:
        def inspect(self):
            return report

    engine = receiver.build(host_inspector=Inspector())
    result = engine.handle(
        request(
            "host-inspect",
            "composite-host-inspect",
            receiver.operator_id,
            {},
        )
    )
    assert result["inspection"] is report
    assert result["action"] == "host-inspect"
    assert receiver.control.load()["lease"] is None


class FakeBackupController:
    def __init__(self, root: Path) -> None:
        self.paths = SimpleNamespace(backup_root=root / "backups")
        self.paths.backup_root.mkdir()
        self.sealed: list[str] = []
        self.restored: list[str] = []
        self.backup_id = "d" * 64
        archive_root = self.paths.backup_root / self.backup_id
        archive_root.mkdir()
        (archive_root / "portable-state.tar").write_bytes(b"portable")

    def status(self):
        return {
            "schema": "iii.portable-backup-status/v1",
            "backup_fresh": bool(self.sealed),
            "backup_count": len(self.sealed),
        }

    def list(self):
        return [{"schema": "iii.host-backup-receipt/v1", "backup_id": self.backup_id}]

    def show(self, backup_id):
        assert backup_id == self.backup_id
        return {"receipt": self.list()[0], "verification": {"verified": True}}

    def chunk(self, backup_id, *, offset, length):
        assert backup_id == self.backup_id
        return {"backup_id": backup_id, "offset": offset, "bytes": length}

    def seal(self, *, operation_id, source):
        assert source == "receiver"
        self.sealed.append(operation_id)
        return {"backup_id": self.backup_id, "verified": True}

    def plan_restore(self, archive_path, *, operation_id):
        archive_sha256 = hashlib.sha256(Path(archive_path).read_bytes()).hexdigest()
        value = {
            "schema": "iii.portable-restore-plan/v1",
            "plan_id": "0" * 64,
            "operation_id": operation_id,
            "backup_id": self.backup_id,
            "archive_path": str(archive_path),
            "archive_sha256": archive_sha256,
            "policy_id": "f" * 64,
            "portable_schema_version": 1,
            "active_release_id": None,
            "clean_converged_host": True,
            "mutations": ["atomic-portable-root-selector"],
        }
        value["plan_id"] = hashlib.sha256(
            canonical_json(
                {key: item for key, item in value.items() if key != "plan_id"}
            )
        ).hexdigest()
        return value

    def restore(self, plan, *, archive_path=None):
        assert archive_path is not None and archive_path.is_file()
        self.restored.append(plan["operation_id"])
        return {"backup_id": self.backup_id, "verified": True}


def test_portable_backup_uses_receiver_plan_lease_and_read_only_export(receiver):
    backup = FakeBackupController(receiver.root)
    engine = receiver.build(backup_controller=backup)
    target = {"logical_id": "drone", "profile": "real"}

    listed = engine.handle(
        request("backup-list", "backup-list-test", receiver.operator_id, {})
    )
    assert listed["backups"][0]["backup_id"] == backup.backup_id
    chunk = engine.handle(
        request(
            "backup-chunk",
            "backup-chunk-test",
            receiver.operator_id,
            {"backup_id": backup.backup_id, "offset": 0, "length": 8},
        )
    )
    assert chunk["chunk"]["bytes"] == 8
    assert receiver.control.load()["lease"] is None

    operation_id = "backup-seal-test"
    planned = engine.handle(
        request(
            "plan-backup-seal",
            operation_id,
            receiver.operator_id,
            {"target": target},
        )
    )
    engine.handle(
        request(
            "backup-seal",
            operation_id,
            receiver.operator_id,
            {"plan": planned["plan"]},
            planned["nonce"],
        )
    )
    receiver.executor.run_next()
    assert backup.sealed == [operation_id]
    assert receiver.journals.load(operation_id)["state"] == "completed"
    assert receiver.control.load()["lease"] is None

    restore_id = "backup-restore-test"
    restore_plan = engine.handle(
        request(
            "plan-backup-restore",
            restore_id,
            receiver.operator_id,
            {"backup_id": backup.backup_id, "target": target},
        )
    )
    engine.handle(
        request(
            "backup-restore",
            restore_id,
            receiver.operator_id,
            {"plan": restore_plan["plan"]},
            restore_plan["nonce"],
        )
    )
    receiver.executor.run_next()
    assert backup.restored == [restore_id]
