"""Receiver-owned planning, durable acceptance, execution, and reconciliation."""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import threading
from typing import Any, Callable, Mapping

from iii_deployment.bundle import ARCHIVE_NAME, COMPONENT_FILES, RELEASE_MANIFEST_NAME
from iii_deployment.activation_health import ActivationCoordinator
from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.log_lifecycle import LogInventory, LogTransferStore
from iii_deployment.receiver.access import AccessManager
from iii_deployment.receiver.clock import ClockController
from iii_deployment.receiver.protocol import (
    Action,
    Request,
    create_mutation_plan,
    validate_mutation_plan,
)
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    ReceiverControlStore,
    TERMINAL_STATES,
)
from iii_deployment.receiver.upload import (
    MANIFEST_NAME as UPLOAD_MANIFEST_NAME,
    RECEIVER_UPDATE_FILES,
    ReceiverUpdateUploadStore,
)
from iii_deployment.staging import STATUS_INDEX_NAME, ReleaseStore


def _sha256_regular(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ContractError("receiver-owned input is not a regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


RESULT_SCHEMA = "iii.receiver-result/v1"
NONCE_EXPIRY_S = 300


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ContractError(f"cannot hash fixed incoming bundle file: {exc}") from exc
    return digest.hexdigest()


def _canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


class ReceiverEngine:
    """Serialize mutation acceptance while keeping observation always available."""

    def __init__(
        self,
        *,
        release_store: ReleaseStore,
        control: ReceiverControlStore,
        journals: OperationJournalStore,
        audit: AuditLog,
        access: AccessManager,
        incoming_root: Path,
        receiver_root: Path,
        logical_target: str,
        profile: str,
        live_state: Callable[[], Mapping[str, Any]],
        executor: Executor | None = None,
        now: Callable[[], datetime] | None = None,
        maximum_claim_bytes: int = 21 * 1024**3,
        activation_coordinator: ActivationCoordinator | None = None,
        clock_controller: ClockController | None = None,
        log_inventory: LogInventory | None = None,
        log_transfer: LogTransferStore | None = None,
        host_maintenance: Any | None = None,
        hardware_inspector: Any | None = None,
        host_inspector: Any | None = None,
        network_controller: Any | None = None,
        backup_controller: Any | None = None,
        receiver_slots: Any | None = None,
        receiver_generation: int | None = None,
        prepare_receiver_handoff: Callable[[], None] | None = None,
        schedule_receiver_handoff: Callable[[], None] | None = None,
    ) -> None:
        if control.nonce_expiry_s != NONCE_EXPIRY_S:
            raise ContractError(
                "receiver nonce expiry differs from the fixed five-minute policy"
            )
        self.release_store = release_store
        self.control = control
        self.journals = journals
        self.audit = audit
        self.access = access
        if incoming_root.is_symlink() or receiver_root.is_symlink():
            raise ContractError("receiver fixed host roots cannot be symbolic links")
        self.incoming_root = incoming_root.absolute()
        self.receiver_root = receiver_root.absolute()
        self.logical_target = logical_target
        self.profile = profile
        self.live_state_provider = live_state
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="iii-receiver"
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        if maximum_claim_bytes <= 0:
            raise ContractError("receiver accepted-input size limit must be positive")
        self.maximum_claim_bytes = maximum_claim_bytes
        self.activation_coordinator = activation_coordinator
        self.clock_controller = clock_controller
        self.log_inventory = log_inventory
        self.log_transfer = log_transfer
        self.host_maintenance = host_maintenance
        self.hardware_inspector = hardware_inspector
        self.host_inspector = host_inspector
        self.network_controller = network_controller
        self.backup_controller = backup_controller
        self.receiver_slots = receiver_slots
        self.receiver_generation = (
            control.receiver_generation
            if receiver_generation is None
            else receiver_generation
        )
        self.prepare_receiver_handoff = prepare_receiver_handoff
        self.schedule_receiver_handoff = schedule_receiver_handoff
        if (log_inventory is None) != (log_transfer is None):
            raise ContractError(
                "receiver log inventory and transfer must be configured together"
            )
        self._mutex = threading.RLock()
        self._running: set[str] = set()
        releases_root = self.release_store.releases_root.resolve()
        if self.receiver_root.is_relative_to(releases_root):
            raise ContractError(
                "receiver binaries cannot reside inside application releases"
            )
        if self.incoming_root.is_relative_to(releases_root):
            raise ContractError(
                "incoming uploads cannot reside inside application releases"
            )

    def handle(self, request: Request) -> dict[str, Any]:
        with self._mutex:
            try:
                result = self._handle(request)
            except ContractError:
                self.audit.append(
                    event="request",
                    outcome="rejected",
                    operation_id=request.operation_id,
                    client_id=request.client_id,
                    action=request.action.value,
                    detail_code="contract-rejected",
                )
                raise
            self.audit.append(
                event="request",
                outcome="accepted",
                operation_id=request.operation_id,
                client_id=request.client_id,
                action=request.action.value,
                detail_code="request-complete",
                evidence_hash=content_identity(result),
            )
            return result

    def _handle(self, request: Request) -> dict[str, Any]:
        if request.action == Action.STATUS:
            try:
                self.access.require_active(request.client_id)
            except ContractError:
                journal = self.journals.load(request.operation_id)
                self.access.require_pending(request.client_id)
                if (
                    journal is None
                    or journal.get("client_id") != request.client_id
                    or journal.get("action") != Action.ACCESS_ADD.value
                ):
                    raise ContractError(
                        "pending machine may inspect only its own enrollment proof"
                    )
                return self._result(
                    request,
                    operation=journal,
                    pending_enrollment_proof=True,
                )
            return self._status(request.operation_id)
        if request.action == Action.ACCESS_LIST:
            self.access.require_active(request.client_id)
            return self._result(
                request,
                target={"logical_id": self.logical_target, "profile": self.profile},
                clients=self.access.list_clients(),
            )
        if request.action == Action.HOST_MAINTENANCE_STATUS:
            self.access.require_active(request.client_id)
            if self.host_maintenance is None:
                raise ContractError("receiver host maintenance is unavailable")
            return self._result(request, maintenance=self.host_maintenance.status())
        if request.action == Action.HARDWARE_INSPECT:
            self.access.require_active(request.client_id)
            if self.hardware_inspector is None:
                raise ContractError("receiver hardware inspection is unavailable")
            return self._result(request, inspection=self.hardware_inspector.inspect())
        if request.action == Action.HOST_INSPECT:
            self.access.require_active(request.client_id)
            if self.host_inspector is None:
                raise ContractError("receiver host inspection is unavailable")
            return self._result(request, inspection=self.host_inspector.inspect())
        if request.action == Action.NETWORK_STATUS:
            self.access.require_active(request.client_id)
            if self.network_controller is None:
                raise ContractError("receiver network controller is unavailable")
            return self._result(
                request,
                network=self.network_controller.status(
                    request.payload["target_operation_id"]
                ),
            )
        if request.action == Action.BACKUP_STATUS:
            self.access.require_active(request.client_id)
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            return self._result(request, backup=self.backup_controller.status())
        if request.action == Action.BACKUP_LIST:
            self.access.require_active(request.client_id)
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            return self._result(request, backups=self.backup_controller.list())
        if request.action == Action.BACKUP_SHOW:
            self.access.require_active(request.client_id)
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            return self._result(
                request,
                backup=self.backup_controller.show(request.payload["backup_id"]),
            )
        if request.action == Action.BACKUP_CHUNK:
            self.access.require_active(request.client_id)
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            return self._result(
                request,
                chunk=self.backup_controller.chunk(
                    request.payload["backup_id"],
                    offset=request.payload["offset"],
                    length=request.payload["length"],
                ),
            )
        if request.action == Action.NETWORK_CONFIRM_PLAN:
            return self._plan_network_confirmation(request)
        if request.action == Action.NETWORK_CONFIRM:
            return self._confirm_network(request)
        if request.action == Action.LOG_EXPORT:
            self.access.require_active(request.client_id)
            if self.log_inventory is None:
                raise ContractError("receiver log inventory is unavailable")
            return self._result(
                request,
                manifest=self.log_inventory.create_manifest(request.payload["domain"]),
            )
        if request.action == Action.LOG_CHUNK:
            self.access.require_active(request.client_id)
            if self.log_transfer is None:
                raise ContractError("receiver log transfer is unavailable")
            return self._result(
                request, chunk=self.log_transfer.chunk(**request.payload)
            )
        if (
            self.clock_controller is not None
            and request.action
            not in {
                Action.PLAN_CLOCK_SYNC,
                Action.CLOCK_SYNC,
                Action.CANCEL,
                Action.NETWORK_PLAN,
                Action.NETWORK_APPLY,
                Action.NETWORK_CONFIRM_PLAN,
                Action.NETWORK_CONFIRM,
                Action.NETWORK_STATUS,
                Action.PLAN_BACKUP_SEAL,
                Action.PLAN_BACKUP_RESTORE,
                Action.BACKUP_STATUS,
                Action.BACKUP_LIST,
                Action.BACKUP_SHOW,
                Action.BACKUP_CHUNK,
            }
            and self.clock_controller.status()["gate"] == "CLOCK_FAULT_ACTIVE"
        ):
            raise ContractError("CLOCK_FAULT_ACTIVE blocks every new receiver mutation")
        if request.action in {
            Action.PLAN_STAGE,
            Action.PLAN_ACTIVATE,
            Action.PLAN_ROLLBACK,
            Action.PLAN_ACCESS,
            Action.PLAN_CLOCK_SYNC,
            Action.PLAN_LOG_RECEIPT,
            Action.PLAN_LOG_PRUNE,
            Action.PLAN_HOST_MAINTENANCE,
            Action.PLAN_HOST_REBOOT,
            Action.NETWORK_PLAN,
            Action.PLAN_BACKUP_SEAL,
            Action.PLAN_BACKUP_RESTORE,
            Action.PLAN_RECEIVER_UPDATE,
        }:
            return self._plan(request)
        if request.action in {
            Action.STAGE,
            Action.ACTIVATE,
            Action.ROLLBACK,
            Action.ACCESS_ADD,
            Action.ACCESS_REVOKE,
            Action.CLOCK_SYNC,
            Action.LOG_RECEIPT,
            Action.LOG_PRUNE,
            Action.HOST_MAINTENANCE,
            Action.HOST_REBOOT,
            Action.NETWORK_APPLY,
            Action.BACKUP_SEAL,
            Action.BACKUP_RESTORE,
            Action.RECEIVER_UPDATE,
        }:
            return self._accept(request)
        if request.action == Action.CANCEL:
            self.access.require_active(request.client_id)
            target = request.payload["target_operation_id"]
            journal = self.journals.load(target)
            if journal is None:
                raise ContractError("receiver cancellation target is unknown")
            if journal["client_id"] != request.client_id:
                raise ContractError(
                    "receiver cancellation client does not own the operation"
                )
            journal = self.journals.request_cancel(target)
            self.audit.append(
                event="cancellation",
                outcome="accepted",
                operation_id=target,
                client_id=request.client_id,
                action=journal["action"],
                detail_code="safe-cancel-requested",
            )
            return self._result(request, operation=journal)
        raise ContractError("receiver action is not implemented by this generation")

    def _plan_network_confirmation(self, request: Request) -> dict[str, Any]:
        self.access.require_active(request.client_id)
        if self.network_controller is None:
            raise ContractError("receiver network controller is unavailable")
        target = request.payload["target"]
        if target != {"logical_id": self.logical_target, "profile": self.profile}:
            raise ContractError("network confirmation targets another host or profile")
        target_operation_id = request.payload["target_operation_id"]
        journal = self.journals.load(target_operation_id)
        if (
            journal is None
            or journal["action"] != Action.NETWORK_APPLY.value
            or journal["client_id"] != request.client_id
            or journal["state"] != "running"
        ):
            raise ContractError(
                "network confirmation target is not an owned pending operation"
            )
        network = self.network_controller.status(target_operation_id)
        if network["state"] != "pending-confirmation":
            raise ContractError("network transaction is not awaiting confirmation")
        confirmation: dict[str, Any] = {
            "schema": "iii.network-confirmation-plan/v1",
            "confirmation_id": "0" * 64,
            "operation_id": request.operation_id,
            "client_id": request.client_id,
            "target_operation_id": target_operation_id,
            "network_id": journal["plan"]["parameters"]["network_id"],
        }
        confirmation["confirmation_id"] = content_identity(
            {
                key: item
                for key, item in confirmation.items()
                if key != "confirmation_id"
            }
        )
        nonce, _ = self.control.issue_nonce(
            operation_id=request.operation_id,
            client_id=request.client_id,
            plan_id=confirmation["confirmation_id"],
        )
        return self._result(
            request,
            confirmation=confirmation,
            nonce=nonce,
            nonce_expires_in_s=NONCE_EXPIRY_S,
            deadline_monotonic_ns=network["deadline_monotonic_ns"],
        )

    def _confirm_network(self, request: Request) -> dict[str, Any]:
        self.access.require_active(request.client_id)
        if self.network_controller is None:
            raise ContractError("receiver network controller is unavailable")
        confirmation = request.payload["confirmation"]
        target_operation_id = confirmation["target_operation_id"]
        journal = self.journals.load(target_operation_id)
        if journal is None or journal["state"] != "running":
            raise ContractError("network confirmation target is not running")
        self.control.consume_for_existing_lease(
            nonce=request.nonce or "",
            operation_id=request.operation_id,
            client_id=request.client_id,
            plan_id=confirmation["confirmation_id"],
            leased_operation_id=target_operation_id,
            leased_action=Action.NETWORK_APPLY,
        )
        try:
            result = self.network_controller.confirm(
                target_operation_id,
                client_id=request.client_id,
                network_id=confirmation["network_id"],
            )
        except Exception:
            network = self.network_controller.status(target_operation_id)
            if network["state"] == "reverted":
                self.journals.transition(
                    target_operation_id,
                    state="failed",
                    checkpoint="automatic-network-rollback",
                    cancellation_safe=False,
                    event="network-confirmation-expired",
                    failure={
                        "code": "network-not-confirmed",
                        "message": "the prior network profile was restored",
                    },
                )
                self.control.release(target_operation_id)
            raise
        evidence_hash = content_identity(result)
        self.journals.transition(
            target_operation_id,
            state="completed",
            checkpoint="network-confirmed",
            cancellation_safe=False,
            event="network-confirmed",
            evidence_hash=evidence_hash,
            result=result,
        )
        self.control.release(target_operation_id)
        return self._result(
            request, network=result, target_operation_id=target_operation_id
        )

    def _live_state(self) -> dict[str, Any]:
        supplied = dict(self.live_state_provider())
        required = {
            "active_release_id",
            "configuration_hash",
            "commissioning_hash",
            "profile",
            "target_state_hash",
        }
        if set(supplied) != required:
            raise ContractError(
                "receiver live-state provider returned unexpected fields"
            )
        if supplied["profile"] != self.profile:
            raise ContractError("receiver live profile differs from configured profile")
        supplied["access_state_id"] = self.access.load()["access_id"]
        return supplied

    def _authorize_planner(self, request: Request) -> None:
        if request.action == Action.PLAN_ACCESS:
            action = request.payload["action"]
            parameters = request.payload["parameters"]
            if action == Action.ACCESS_ADD.value and parameters["phase"] == "prove":
                self.access.require_pending_proof(
                    requester=request.client_id,
                    enrollment=parameters["enrollment"],
                )
                return
        self.access.require_active(request.client_id)

    def _plan(self, request: Request) -> dict[str, Any]:
        self._authorize_planner(request)
        target = request.payload["target"]
        if target != {"logical_id": self.logical_target, "profile": self.profile}:
            raise ContractError(
                "receiver request targets another logical host or profile"
            )
        parameter_override = None
        if request.action in {Action.PLAN_LOG_RECEIPT, Action.PLAN_LOG_PRUNE}:
            if self.log_inventory is None or self.log_transfer is None:
                raise ContractError("receiver log lifecycle is unavailable")
            if request.action == Action.PLAN_LOG_RECEIPT:
                receipt = self.log_transfer.receipt_plan(
                    manifest_id=request.payload["manifest_id"],
                    client_id=request.client_id,
                    verified_files=request.payload["verified_files"],
                )
                parameter_override = {
                    "manifest_id": receipt["manifest_id"],
                    "receipt_id": receipt["receipt_id"],
                    "verified_files": receipt["files"],
                }
            else:
                parameter_override = {
                    "prune_plan": self.log_inventory.prune_plan(
                        request.payload["receipt_id"]
                    )
                }
        elif request.action == Action.PLAN_HOST_MAINTENANCE:
            if self.host_maintenance is None:
                raise ContractError("receiver host maintenance is unavailable")
            parameter_override = self.host_maintenance.plan(
                operation_id=request.operation_id,
                client_id=request.client_id,
                request=request.payload["request"],
                live_state=self._live_state(),
            )
        elif request.action == Action.PLAN_HOST_REBOOT:
            if self.host_maintenance is None:
                raise ContractError("receiver host maintenance is unavailable")
            parameter_override = self.host_maintenance.plan_reboot(
                operation_id=request.operation_id,
                client_id=request.client_id,
                maintenance_id=request.payload["maintenance_id"],
            )
        elif request.action == Action.NETWORK_PLAN:
            if self.network_controller is None:
                raise ContractError("receiver network controller is unavailable")
            parameter_override = self.network_controller.plan(
                operation_id=request.operation_id,
                client_id=request.client_id,
                profile=request.payload["profile"],
            )
        elif request.action == Action.PLAN_BACKUP_SEAL:
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            parameter_override = {"source": "receiver"}
        elif request.action == Action.PLAN_BACKUP_RESTORE:
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            local_archive = (
                self.backup_controller.paths.backup_root
                / request.payload["backup_id"]
                / "portable-state.tar"
            )
            imported_archive = (
                self.incoming_root
                / "backups"
                / request.payload["backup_id"]
                / "portable-state.tar"
            )
            archive_path = (
                local_archive if local_archive.is_file() else imported_archive
            )
            parameter_override = self.backup_controller.plan_restore(
                archive_path, operation_id=request.operation_id
            )
        elif request.action == Action.PLAN_RECEIVER_UPDATE:
            verified = self._preflight_receiver_update(
                request.payload["artifact"], client_id=request.client_id
            )
            parameter_override = {
                "receiver_id": verified.manifest["receiver_id"],
                "generation": verified.manifest["generation"],
                "archive_sha256": request.payload["artifact"]["archive_sha256"],
                "upload_id": request.payload["artifact"]["upload_id"],
            }
        plan = create_mutation_plan(
            request,
            receiver_generation=self.receiver_generation,
            live_state=self._live_state(),
            parameter_override=parameter_override,
        )
        nonce, _ = self.control.issue_nonce(
            operation_id=request.operation_id,
            client_id=request.client_id,
            plan_id=plan["plan_id"],
        )
        fields: dict[str, Any] = {
            "plan": plan,
            "nonce": nonce,
            "nonce_expires_in_s": NONCE_EXPIRY_S,
        }
        if request.action in {Action.PLAN_ACTIVATE, Action.PLAN_ROLLBACK}:
            if self.activation_coordinator is None:
                raise ContractError("receiver activation coordinator is unavailable")
            parameters = plan["parameters"]
            reconciliation_arguments = (
                {
                    "configuration_reconciliation_decisions": parameters[
                        "configuration_reconciliation_decisions"
                    ]
                }
                if "configuration_reconciliation_decisions" in parameters
                else {}
            )
            fields["preflight"] = self.activation_coordinator.preflight(
                release_id=parameters["release_id"],
                configuration_checkpoint_id=parameters["configuration_checkpoint_id"],
                px4_activation_evidence=parameters["px4_activation_evidence"],
                operator_rollback=request.action == Action.PLAN_ROLLBACK,
                **reconciliation_arguments,
            )
        if request.action == Action.PLAN_CLOCK_SYNC:
            if self.clock_controller is None:
                raise ContractError("receiver clock controller is unavailable")
            self.clock_controller.validate_samples(plan["parameters"]["samples"])
            fields["preflight"] = {
                "schema": "iii.clock-sync-preflight/v1",
                "ready": True,
                "samples": len(plan["parameters"]["samples"]),
                "clock": self.clock_controller.status(),
            }
        return self._result(
            request,
            **fields,
        )

    def _authorize_plan_client(self, request: Request, plan: Mapping[str, Any]) -> None:
        if (
            plan["action"] == Action.ACCESS_ADD.value
            and plan["parameters"]["phase"] == "prove"
        ):
            parameters = plan["parameters"]
            self.access.require_pending_proof(
                requester=request.client_id,
                enrollment=parameters["enrollment"],
            )
            return
        self.access.require_active(request.client_id)

    def _accept(self, request: Request) -> dict[str, Any]:
        plan = request.payload["plan"]
        validate_mutation_plan(
            plan,
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if plan["action"] != request.action.value:
            raise ContractError("receiver apply action differs from retained plan")
        if plan["receiver_generation"] != self.receiver_generation:
            raise ContractError("receiver plan belongs to another receiver generation")
        if plan["target"] != {
            "logical_id": self.logical_target,
            "profile": self.profile,
        }:
            raise ContractError("receiver apply plan targets another host or profile")
        self._authorize_plan_client(request, plan)
        if plan["expected_state"] != self._live_state():
            raise ContractError("receiver plan is stale against current target state")
        if self.host_maintenance is not None:
            self.host_maintenance.assert_mutation_allowed(request.action.value)
        if request.action == Action.STAGE:
            self._preflight_staging(plan)
        if request.action == Action.RECEIVER_UPDATE:
            self._preflight_receiver_update(
                plan["parameters"], client_id=request.client_id
            )
        if request.action in {Action.ACTIVATE, Action.ROLLBACK}:
            if self.activation_coordinator is None:
                raise ContractError("receiver activation coordinator is unavailable")
            parameters = plan["parameters"]
            reconciliation_arguments = (
                {
                    "configuration_reconciliation_decisions": parameters[
                        "configuration_reconciliation_decisions"
                    ]
                }
                if "configuration_reconciliation_decisions" in parameters
                else {}
            )
            preflight = self.activation_coordinator.preflight(
                release_id=parameters["release_id"],
                configuration_checkpoint_id=parameters["configuration_checkpoint_id"],
                px4_activation_evidence=parameters["px4_activation_evidence"],
                operator_rollback=request.action == Action.ROLLBACK,
                **reconciliation_arguments,
            )
            if not preflight["ready"]:
                raise ContractError(
                    "activation preflight rejected: "
                    + "; ".join(preflight["rejection_reasons"])
                )
        if request.action == Action.CLOCK_SYNC and self.clock_controller is None:
            raise ContractError("receiver clock controller is unavailable")
        if request.action in {Action.LOG_RECEIPT, Action.LOG_PRUNE} and (
            self.log_inventory is None or self.log_transfer is None
        ):
            raise ContractError("receiver log lifecycle is unavailable")
        if request.action == Action.NETWORK_APPLY and self.network_controller is None:
            raise ContractError("receiver network controller is unavailable")
        if (
            request.action in {Action.BACKUP_SEAL, Action.BACKUP_RESTORE}
            and self.backup_controller is None
        ):
            raise ContractError("receiver portable backup is unavailable")
        self.control.consume_and_acquire(
            nonce=request.nonce or "",
            operation_id=request.operation_id,
            client_id=request.client_id,
            action=request.action,
            plan_id=plan["plan_id"],
        )
        try:
            if request.action == Action.STAGE:
                self._claim_staging_input(plan)
            if request.action == Action.RECEIVER_UPDATE:
                self._claim_receiver_update(plan)
            if request.action == Action.NETWORK_APPLY:
                self.network_controller.claim(
                    plan["parameters"], request.payload["profile"]
                )
            if request.action == Action.BACKUP_RESTORE:
                self._claim_backup_restore(plan)
            journal = self.journals.create(
                plan=plan,
                **(
                    {
                        "target_acceptance_s": 1800,
                        "hard_deadline_s": 7200,
                        "rollback_target_s": 60,
                    }
                    if request.action
                    in {
                        Action.HOST_MAINTENANCE,
                        Action.BACKUP_SEAL,
                        Action.BACKUP_RESTORE,
                    }
                    else {}
                ),
            )
        except Exception:
            if request.action == Action.BACKUP_RESTORE:
                shutil.rmtree(
                    self.control.root / "accepted-inputs" / request.operation_id,
                    ignore_errors=True,
                )
            self.control.release(request.operation_id)
            raise
        self.audit.append(
            event="operation",
            outcome="accepted",
            operation_id=request.operation_id,
            client_id=request.client_id,
            action=request.action.value,
            detail_code="durable-acceptance",
        )
        self._submit(request.operation_id)
        return self._result(request, operation=journal, detached=True)

    def _incoming_paths(self, plan: Mapping[str, Any]) -> tuple[Path, Path | None]:
        parameters = plan["parameters"]
        upload_root = self.incoming_root / parameters["upload_id"]
        component = upload_root / "drone"
        status = upload_root / STATUS_INDEX_NAME
        for path in (self.incoming_root, upload_root, component):
            if path.is_symlink() or not path.is_dir():
                raise ContractError(
                    "fixed incoming bundle directory is missing or linked"
                )
        if component.resolve() != component:
            raise ContractError(
                "fixed incoming bundle resolves outside its upload slot"
            )
        expected_status = parameters["status_index_id"]
        if expected_status is None:
            if status.exists() or status.is_symlink():
                raise ContractError(
                    "unplanned release-status index accompanies incoming bundle"
                )
            return component, None
        value = _canonical_object(status, label="incoming release-status index")
        if value.get("index_id") != expected_status:
            raise ContractError(
                "incoming release-status index differs from retained plan"
            )
        return component, status

    def _claim_backup_restore(self, plan: Mapping[str, Any]) -> None:
        parameters = plan["parameters"]
        source = Path(parameters["archive_path"])
        incoming_backups = self.incoming_root / "backups"
        expected_parent = incoming_backups / parameters["backup_id"]
        if (
            source != expected_parent / "portable-state.tar"
            or incoming_backups.is_symlink()
            or expected_parent.is_symlink()
            or not expected_parent.is_dir()
        ):
            # A locally retained receiver backup is also valid, but it is already
            # root-owned and immutable through this interface.
            local = (
                self.backup_controller.paths.backup_root
                / parameters["backup_id"]
                / "portable-state.tar"
            )
            if source != local:
                raise ContractError("portable restore input is outside fixed roots")
        accepted = self.control.root / "accepted-inputs" / plan["operation_id"]
        accepted.mkdir(parents=True, mode=0o700)
        destination = accepted / "portable-state.tar"
        self._copy_regular(source, destination, maximum_bytes=self.maximum_claim_bytes)
        if _sha256_regular(destination) != parameters["archive_sha256"]:
            shutil.rmtree(accepted, ignore_errors=True)
            raise ContractError(
                "claimed portable restore archive changed after planning"
            )
        if source == expected_parent / "portable-state.tar":
            shutil.rmtree(expected_parent)
            descriptor = os.open(incoming_backups, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _accepted_backup_restore(self, operation_id: str) -> Path:
        path = (
            self.control.root / "accepted-inputs" / operation_id / "portable-state.tar"
        )
        if path.is_symlink() or not path.is_file():
            raise ContractError("receiver-owned portable restore input is missing")
        return path

    def _accepted_paths(self, plan: Mapping[str, Any]) -> tuple[Path, Path | None]:
        root = self.control.root / "accepted-inputs" / plan["operation_id"]
        component = root / "drone"
        status = root / STATUS_INDEX_NAME
        if (
            root.is_symlink()
            or not root.is_dir()
            or component.is_symlink()
            or not component.is_dir()
        ):
            raise ContractError("receiver-owned accepted input is missing or linked")
        expected_status = plan["parameters"]["status_index_id"]
        if expected_status is None:
            if status.exists() or status.is_symlink():
                raise ContractError(
                    "receiver-owned input has an unplanned status index"
                )
            return component, None
        value = _canonical_object(status, label="accepted release-status index")
        if value.get("index_id") != expected_status:
            raise ContractError(
                "accepted release-status index differs from retained plan"
            )
        return component, status

    @staticmethod
    def _copy_regular(source: Path, destination: Path, *, maximum_bytes: int) -> int:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        destination_descriptor = -1
        try:
            observed = os.fstat(source_descriptor)
            if not stat.S_ISREG(observed.st_mode):
                raise ContractError("incoming bundle contains a non-regular file")
            if observed.st_size > maximum_bytes:
                raise ContractError(
                    "incoming bundle exceeds the receiver-owned input limit"
                )
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
            )
            copied = 0
            while True:
                block = os.read(source_descriptor, 1024 * 1024)
                if not block:
                    break
                copied += len(block)
                if copied > maximum_bytes:
                    raise ContractError(
                        "incoming bundle grew beyond the receiver-owned input limit"
                    )
                view = memoryview(block)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise ContractError(
                            "receiver-owned input copy made no progress"
                        )
                    view = view[written:]
            os.fsync(destination_descriptor)
        except OSError as exc:
            raise ContractError(
                f"cannot claim fixed incoming bundle file: {exc}"
            ) from exc
        finally:
            os.close(source_descriptor)
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
        return copied

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _claim_staging_input(self, plan: Mapping[str, Any]) -> None:
        destination = self.control.root / "accepted-inputs" / plan["operation_id"]
        if destination.exists() or destination.is_symlink():
            self._preflight_staging(plan, accepted=True)
            return
        component, status_path = self._incoming_paths(plan)
        try:
            observed_files = {path.name for path in component.iterdir()}
            if observed_files != COMPONENT_FILES or any(
                path.is_symlink() or not path.is_file() for path in component.iterdir()
            ):
                raise ContractError(
                    "incoming drone bundle does not contain the exact fixed file set"
                )
            claimed_sources = [component / name for name in sorted(COMPONENT_FILES)]
            if status_path is not None:
                claimed_sources.append(status_path)
            total_bytes = sum(
                path.stat(follow_symlinks=False).st_size for path in claimed_sources
            )
        except OSError as exc:
            raise ContractError(f"cannot inspect fixed incoming upload: {exc}") from exc
        if total_bytes > self.maximum_claim_bytes:
            raise ContractError(
                "incoming upload exceeds the receiver-owned input limit"
            )
        upload_root = component.parent
        expected_upload_entries = {"drone"} | (
            {STATUS_INDEX_NAME} if status_path is not None else set()
        )
        try:
            observed_upload_entries = {path.name for path in upload_root.iterdir()}
        except OSError as exc:
            raise ContractError(f"cannot inspect incoming upload slot: {exc}") from exc
        if observed_upload_entries != expected_upload_entries:
            raise ContractError("incoming upload slot contains unplanned entries")
        accepted_root = destination.parent
        accepted_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        minimum_bytes = getattr(self.release_store, "minimum_reserve_bytes", 0)
        minimum_percent = getattr(self.release_store, "minimum_reserve_percent", 0.0)
        usage = shutil.disk_usage(accepted_root)
        reserve = max(
            int(minimum_bytes), int(usage.total * float(minimum_percent) / 100.0)
        )
        if usage.free - total_bytes < reserve:
            raise ContractError(
                "insufficient storage to claim receiver-owned input and preserve reserve"
            )
        temporary = accepted_root / f".{plan['operation_id']}.partial"
        if temporary.exists() or temporary.is_symlink():
            raise ContractError("partial receiver-owned input requires reconciliation")
        temporary.mkdir(mode=0o700)
        try:
            claimed_component = temporary / "drone"
            claimed_component.mkdir(mode=0o700)
            remaining = self.maximum_claim_bytes
            for name in sorted(COMPONENT_FILES):
                copied = self._copy_regular(
                    component / name,
                    claimed_component / name,
                    maximum_bytes=remaining,
                )
                remaining -= copied
            self._fsync_directory(claimed_component)
            if status_path is not None:
                self._copy_regular(
                    status_path,
                    temporary / STATUS_INDEX_NAME,
                    maximum_bytes=remaining,
                )
            self._fsync_directory(temporary)
            os.replace(temporary, destination)
            self._fsync_directory(accepted_root)
        except Exception:
            # A partial is never consumed. It is retained fail-closed so startup
            # reconciliation or host inspection can report the interrupted claim.
            raise
        self._preflight_staging(plan, accepted=True)

    def _preflight_staging(
        self, plan: Mapping[str, Any], *, accepted: bool = False
    ) -> None:
        component, _ = (
            self._accepted_paths(plan) if accepted else self._incoming_paths(plan)
        )
        parameters = plan["parameters"]
        archive = component / ARCHIVE_NAME
        manifest = _canonical_object(
            component / RELEASE_MANIFEST_NAME,
            label="incoming release manifest",
        )
        if manifest.get("release_id") != parameters["release_id"]:
            raise ContractError("incoming release identity differs from retained plan")
        if archive.is_symlink() or not archive.is_file():
            raise ContractError("incoming bundle archive is missing or linked")
        if _sha256(archive) != parameters["archive_sha256"]:
            raise ContractError("incoming bundle archive differs from retained plan")

    def _receiver_update_paths(
        self, parameters: Mapping[str, Any], *, accepted: bool = False
    ) -> tuple[Path, Path | None]:
        if accepted:
            bundle = (
                self.control.root
                / "accepted-inputs"
                / str(parameters["operation_id"])
                / "receiver-update"
            )
            return bundle, None
        upload_root = (
            self.incoming_root
            / "receiver-updates"
            / str(parameters["receiver_id"])
        )
        return upload_root / "bundle", upload_root / UPLOAD_MANIFEST_NAME

    def _preflight_receiver_update(
        self,
        parameters: Mapping[str, Any],
        *,
        client_id: str | None = None,
        accepted: bool = False,
    ) -> Any:
        if self.receiver_slots is None:
            raise ContractError("receiver self-update is unavailable")
        bundle, upload_manifest_path = self._receiver_update_paths(
            parameters, accepted=accepted
        )
        if bundle.is_symlink() or not bundle.is_dir():
            raise ContractError("incoming receiver update bundle is missing or linked")
        if {path.name for path in bundle.iterdir()} != RECEIVER_UPDATE_FILES:
            raise ContractError("incoming receiver update file set is not exact")
        if any(path.is_symlink() or not path.is_file() for path in bundle.iterdir()):
            raise ContractError("incoming receiver update contains an unsafe file")
        if not accepted:
            assert upload_manifest_path is not None
            upload = _canonical_object(
                upload_manifest_path, label="receiver update upload manifest"
            )
            ReceiverUpdateUploadStore.validate_manifest(
                upload,
                receiver_id=str(parameters["receiver_id"]),
                client_id=str(client_id or upload.get("client_id", "")),
            )
            if upload["upload_id"] != parameters["upload_id"]:
                raise ContractError(
                    "receiver update upload differs from retained artifact"
                )
        verified = self.receiver_slots.verify_update(bundle)
        manifest = verified.manifest
        if (
            manifest["receiver_id"] != parameters["receiver_id"]
            or manifest["generation"] != parameters["generation"]
            or _sha256(bundle / "receiver-update.tar")
            != parameters["archive_sha256"]
        ):
            raise ContractError(
                "signed receiver update differs from retained artifact identity"
            )
        return verified

    def _claim_receiver_update(self, plan: Mapping[str, Any]) -> None:
        parameters = {**plan["parameters"], "operation_id": plan["operation_id"]}
        destination, _ = self._receiver_update_paths(parameters, accepted=True)
        if destination.exists() or destination.is_symlink():
            self._preflight_receiver_update(parameters, accepted=True)
            return
        source, _ = self._receiver_update_paths(parameters)
        try:
            if {path.name for path in source.iterdir()} != RECEIVER_UPDATE_FILES:
                raise ContractError("incoming receiver update file set is not exact")
            sources = [source / name for name in sorted(RECEIVER_UPDATE_FILES)]
            if any(path.is_symlink() or not path.is_file() for path in sources):
                raise ContractError("incoming receiver update contains an unsafe file")
            total_bytes = sum(
                path.stat(follow_symlinks=False).st_size for path in sources
            )
        except OSError as exc:
            raise ContractError(f"cannot inspect receiver update input: {exc}") from exc
        if total_bytes > self.maximum_claim_bytes:
            raise ContractError(
                "incoming receiver update exceeds the receiver-owned input limit"
            )
        accepted_root = destination.parent
        accepted_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        minimum_bytes = getattr(self.release_store, "minimum_reserve_bytes", 0)
        minimum_percent = getattr(self.release_store, "minimum_reserve_percent", 0.0)
        usage = shutil.disk_usage(accepted_root)
        reserve = max(
            int(minimum_bytes), int(usage.total * float(minimum_percent) / 100.0)
        )
        if usage.free - total_bytes < reserve:
            raise ContractError(
                "insufficient storage to claim receiver update and preserve reserve"
            )
        temporary = accepted_root / ".receiver-update.partial"
        if temporary.exists() or temporary.is_symlink():
            raise ContractError("partial receiver update claim requires reconciliation")
        temporary.mkdir(mode=0o700)
        remaining = self.maximum_claim_bytes
        for name in sorted(RECEIVER_UPDATE_FILES):
            copied = self._copy_regular(
                source / name, temporary / name, maximum_bytes=remaining
            )
            remaining -= copied
        self._fsync_directory(temporary)
        os.replace(temporary, destination)
        self._fsync_directory(accepted_root)
        self._preflight_receiver_update(parameters, accepted=True)

    def _submit(self, operation_id: str) -> None:
        if operation_id in self._running:
            return
        self._running.add(operation_id)
        try:
            self.executor.submit(self._run, operation_id)
        except Exception:
            self._running.discard(operation_id)
            raise

    def _run(self, operation_id: str) -> None:
        try:
            with self._mutex:
                journal = self.journals.load(operation_id)
                if journal is None:
                    raise ContractError("accepted operation journal disappeared")
                if journal["state"] in TERMINAL_STATES:
                    return
                if journal["cancel_requested"]:
                    self.journals.transition(
                        operation_id,
                        state="cancelled",
                        checkpoint="accepted",
                        cancellation_safe=True,
                        event="cancelled-before-mutation",
                    )
                    self.audit.append(
                        event="operation",
                        outcome="cancelled",
                        operation_id=operation_id,
                        client_id=journal["client_id"],
                        action=journal["action"],
                        detail_code="cancelled-at-safe-checkpoint",
                    )
                    self.control.release(operation_id)
                    return
                if self.journals.remaining_budget(operation_id) <= 0:
                    raise ContractError(
                        "receiver hard deadline expired before mutation"
                    )
                if journal["state"] == "accepted":
                    journal = self.journals.transition(
                        operation_id,
                        state="running",
                        checkpoint="privileged-mutation",
                        cancellation_safe=False,
                        event="privileged-mutation-started",
                    )
            result = self._execute(journal["plan"])
            if journal["action"] == Action.HOST_REBOOT.value:
                # A successful reboot request is deliberately nonterminal. The
                # same durable journal and mutation lease survive shutdown; only
                # startup reconciliation may complete it after the boot ID and
                # protected qualified release have both been validated.
                self.audit.append(
                    event="operation",
                    outcome="accepted",
                    operation_id=operation_id,
                    client_id=journal["client_id"],
                    action=journal["action"],
                    detail_code="reboot-scheduled-awaiting-postboot-validation",
                )
                return
            if journal["action"] == Action.RECEIVER_UPDATE.value:
                self.audit.append(
                    event="operation",
                    outcome="accepted",
                    operation_id=operation_id,
                    client_id=journal["client_id"],
                    action=journal["action"],
                    detail_code="receiver-handoff-scheduled",
                    evidence_hash=content_identity(result),
                )
                return
            if (
                journal["action"] == Action.NETWORK_APPLY.value
                and result.get("state") == "pending-confirmation"
            ):
                self.audit.append(
                    event="operation",
                    outcome="accepted",
                    operation_id=operation_id,
                    client_id=journal["client_id"],
                    action=journal["action"],
                    detail_code="network-applied-awaiting-onboard-confirmation",
                    evidence_hash=content_identity(result),
                )
                return
            with self._mutex:
                remaining = self.journals.remaining_budget(operation_id)
                if remaining <= 0:
                    raise ContractError(
                        "receiver hard deadline expired during mutation"
                    )
                result["deadlines"] = journal["deadlines"]
                result["remaining_hard_s"] = remaining
                elapsed_active = journal["deadlines"]["hard_deadline_s"] - remaining
                result["deadline_measurements"] = {
                    "active_elapsed_s": elapsed_active,
                    "target_acceptance_met": (
                        elapsed_active <= journal["deadlines"]["target_acceptance_s"]
                    ),
                    "hard_deadline_met": True,
                    "rollback_elapsed_s": None,
                    "rollback_target_met": None,
                }
                evidence_hash = content_identity(result)
                self.journals.transition(
                    operation_id,
                    state="completed",
                    checkpoint="complete",
                    cancellation_safe=False,
                    event="operation-completed",
                    evidence_hash=evidence_hash,
                    result=result,
                )
                self.audit.append(
                    event="operation",
                    outcome="completed",
                    operation_id=operation_id,
                    client_id=journal["client_id"],
                    action=journal["action"],
                    detail_code="mutation-complete",
                    evidence_hash=evidence_hash,
                )
                self.control.release(operation_id)
        except Exception as exc:
            with self._mutex:
                journal = self.journals.load(operation_id)
                if journal is not None and journal["state"] not in TERMINAL_STATES:
                    self.journals.transition(
                        operation_id,
                        state="failed",
                        checkpoint="failed",
                        cancellation_safe=False,
                        event="operation-failed",
                        failure={"code": "mutation-failed", "message": str(exc)},
                    )
                    self.audit.append(
                        event="operation",
                        outcome="failed",
                        operation_id=operation_id,
                        client_id=journal["client_id"],
                        action=journal["action"],
                        detail_code="mutation-failed",
                    )
                control = self.control.load()
                if (
                    control["lease"] is not None
                    and control["lease"]["operation_id"] == operation_id
                ):
                    self.control.release(operation_id)
        finally:
            with self._mutex:
                self._running.discard(operation_id)

    def _execute(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        action = plan["action"]
        parameters = plan["parameters"]
        if action == Action.STAGE.value:
            self._preflight_staging(plan, accepted=True)
            component, status_path = self._accepted_paths(plan)
            status_index = (
                None
                if status_path is None
                else _canonical_object(
                    status_path, label="incoming release-status index"
                )
            )
            result = self.release_store.stage(
                component,
                status_index=status_index,
                staged_at=self.now().isoformat().replace("+00:00", "Z"),
            )
            if result.release_id != parameters["release_id"]:
                raise ContractError("staged release differs from accepted operation")
            return {"kind": "stage", **asdict(result)}
        if action == Action.RECEIVER_UPDATE.value:
            if (
                self.receiver_slots is None
                or self.prepare_receiver_handoff is None
                or self.schedule_receiver_handoff is None
            ):
                raise ContractError("receiver self-update handoff is unavailable")
            parameters_with_operation = {
                **parameters,
                "operation_id": plan["operation_id"],
            }
            self._preflight_receiver_update(
                parameters_with_operation, accepted=True
            )
            self.prepare_receiver_handoff()
            bundle, _ = self._receiver_update_paths(
                parameters_with_operation, accepted=True
            )
            state = self.receiver_slots.stage(
                bundle,
                operation_id=plan["operation_id"],
                client_id=plan["client_id"],
            )
            try:
                self.schedule_receiver_handoff()
            except Exception as exc:
                self.receiver_slots.abort_staged(
                    operation_id=plan["operation_id"],
                    client_id=plan["client_id"],
                    reason=f"receiver handoff could not be scheduled: {exc}",
                )
                raise ContractError(
                    f"receiver handoff could not be scheduled: {exc}"
                ) from exc
            return {
                "kind": "receiver-update",
                "receiver_id": parameters["receiver_id"],
                "generation": parameters["generation"],
                "state_id": state["state_id"],
                "stage": "handoff-scheduled",
                "autonomy_started": False,
            }
        if action == Action.ACTIVATE.value:
            if self.activation_coordinator is None:
                raise ContractError("receiver activation coordinator is unavailable")
            reconciliation_arguments = (
                {
                    "configuration_reconciliation_decisions": parameters[
                        "configuration_reconciliation_decisions"
                    ]
                }
                if "configuration_reconciliation_decisions" in parameters
                else {}
            )
            return self.activation_coordinator.activate(
                operation_id=plan["operation_id"],
                release_id=parameters["release_id"],
                configuration_checkpoint_id=parameters["configuration_checkpoint_id"],
                explicit_qualified_action=parameters["explicit_qualified_action"],
                px4_activation_evidence=parameters["px4_activation_evidence"],
                **reconciliation_arguments,
            )
        if action == Action.ROLLBACK.value:
            if self.activation_coordinator is None:
                raise ContractError("receiver activation coordinator is unavailable")
            return self.activation_coordinator.operator_rollback(
                operation_id=plan["operation_id"],
                release_id=parameters["release_id"],
                configuration_checkpoint_id=parameters["configuration_checkpoint_id"],
                px4_activation_evidence=parameters["px4_activation_evidence"],
            )
        if action == Action.CLOCK_SYNC.value:
            if self.clock_controller is None:
                raise ContractError("receiver clock controller is unavailable")
            return self.clock_controller.synchronize(
                operation_id=plan["operation_id"], samples=parameters["samples"]
            )
        if action == Action.BACKUP_SEAL.value:
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            return {
                "kind": "backup-seal",
                **self.backup_controller.seal(
                    operation_id=plan["operation_id"], source=parameters["source"]
                ),
            }
        if action == Action.BACKUP_RESTORE.value:
            if self.backup_controller is None:
                raise ContractError("receiver portable backup is unavailable")
            accepted = self._accepted_backup_restore(plan["operation_id"])
            try:
                return {
                    "kind": "backup-restore",
                    **self.backup_controller.restore(parameters, archive_path=accepted),
                }
            finally:
                shutil.rmtree(accepted.parent, ignore_errors=True)
        if action == Action.ACCESS_ADD.value:
            if parameters["phase"] == "add":
                state = self.access.add_pending(
                    requester=plan["client_id"],
                    enrollment=parameters["enrollment"],
                )
                state_name = "pending"
            else:
                state = self.access.prove(
                    requester=plan["client_id"],
                    enrollment=parameters["enrollment"],
                )
                state_name = "active"
            return {
                "kind": "access",
                "access_id": state["access_id"],
                "generation": state["generation"],
                "client_id": parameters["enrollment"]["ssh"]["client_id"],
                "machine_id": parameters["enrollment"]["machine_id"],
                "state": state_name,
            }
        if action == Action.ACCESS_REVOKE.value:
            if parameters["authority"] == "machine":
                state = self.access.revoke(
                    requester=plan["client_id"], machine_id=parameters["machine_id"]
                )
                identity = {"machine_id": parameters["machine_id"]}
                state_name = "revoked"
            else:
                state = self.access.revoke_field_signer(
                    requester=plan["client_id"],
                    field_signer_id=parameters["field_signer_id"],
                )
                identity = {"field_signer_id": parameters["field_signer_id"]}
                state_name = "signing-revoked-runtime-active"
            return {
                "kind": "access",
                "access_id": state["access_id"],
                "generation": state["generation"],
                **identity,
                "state": state_name,
            }
        if action == Action.HOST_MAINTENANCE.value:
            if self.host_maintenance is None:
                raise ContractError("receiver host maintenance is unavailable")
            transaction = self.host_maintenance.apply(parameters)
            return {
                "kind": "host-maintenance",
                "maintenance_id": transaction["maintenance_id"],
                "phase": transaction["phase"],
                "transaction_id": transaction["transaction_id"],
                "reboot_required": transaction["reboot"]["required"],
                "commissioning": transaction["commissioning"],
            }
        if action == Action.HOST_REBOOT.value:
            if self.host_maintenance is None:
                raise ContractError("receiver host maintenance is unavailable")
            transaction = self.host_maintenance.schedule_reboot(
                parameters["maintenance_id"]
            )
            return {
                "kind": "host-reboot",
                "maintenance_id": transaction["maintenance_id"],
                "phase": transaction["phase"],
                "transaction_id": transaction["transaction_id"],
                "reboot_scheduled": True,
            }
        if action == Action.NETWORK_APPLY.value:
            if self.network_controller is None:
                raise ContractError("receiver network controller is unavailable")
            return self.network_controller.apply(parameters)
        if action == Action.LOG_RECEIPT.value:
            if self.log_transfer is None:
                raise ContractError("receiver log transfer is unavailable")
            receipt = self.log_transfer.receipt(
                manifest_id=parameters["manifest_id"],
                client_id=plan["client_id"],
                verified_files=parameters["verified_files"],
            )
            if receipt["receipt_id"] != parameters["receipt_id"]:
                raise ContractError("recorded log receipt differs from accepted plan")
            return {"kind": "log-receipt", "receipt": receipt}
        if action == Action.LOG_PRUNE.value:
            if self.log_inventory is None:
                raise ContractError("receiver log inventory is unavailable")
            removed = self.log_inventory.apply_prune(parameters["prune_plan"])
            return {
                "kind": "log-prune",
                "receipt_id": parameters["prune_plan"]["receipt_id"],
                "removed": removed,
            }
        raise ContractError("accepted receiver plan action is not implemented")

    def reconcile(self) -> dict[str, Any]:
        """Recover durable work only; never activate or start application autonomy."""

        with self._mutex:
            self.access.reconcile_derived_access()
            activation_recovery = (
                self.activation_coordinator.reconcile()
                if self.activation_coordinator is not None
                else None
            )
            host_maintenance_recovery = (
                self.host_maintenance.reconcile()
                if self.host_maintenance is not None
                else None
            )
            network_recovery = (
                self.network_controller.reconcile()
                if self.network_controller is not None
                else None
            )
            control = self.control.load()
            lease = control["lease"]
            journals = {item["operation_id"]: item for item in self.journals.list()}
            recovered: list[str] = []
            failed: list[str] = []
            if lease is not None:
                journal = journals.get(lease["operation_id"])
                if journal is None or journal["state"] in TERMINAL_STATES:
                    self.audit.append(
                        event="reconciliation",
                        outcome="recovered",
                        operation_id=lease["operation_id"],
                        client_id=lease["client_id"],
                        action=lease["action"],
                        detail_code="stale-lease-released",
                    )
                    self.control.recover_stale_lease(lease["operation_id"])
                    lease = None
                elif (
                    journal["client_id"] != lease["client_id"]
                    or journal["action"] != lease["action"]
                    or journal["plan"]["plan_id"] != lease["plan_id"]
                ):
                    raise ContractError(
                        "receiver lease and operation journal binding disagree"
                    )
            for operation_id, journal in journals.items():
                if journal["state"] in TERMINAL_STATES:
                    continue
                if journal["action"] == Action.RECEIVER_UPDATE.value:
                    if self.receiver_slots is None:
                        raise ContractError("receiver self-update is unavailable")
                    update = self.receiver_slots.update_state()
                    if (
                        update is None
                        or update["operation_id"] != operation_id
                        or update["client_id"] != journal["client_id"]
                    ):
                        raise ContractError(
                            "receiver update journal and A/B state disagree"
                        )
                    if update["stage"] == "committed":
                        if update["candidate_generation"] != self.receiver_generation:
                            raise ContractError(
                                "committed receiver update generation is not active"
                            )
                        if self.control.receiver_generation != self.receiver_generation:
                            self.control.adopt_generation(
                                self.receiver_generation, operation_id=operation_id
                            )
                            control = self.control.load()
                            lease = control["lease"]
                        result = {
                            "kind": "receiver-update",
                            "receiver_id": update["candidate_receiver_id"],
                            "generation": update["candidate_generation"],
                            "state_id": update["state_id"],
                            "stage": "committed",
                            "readiness": update["readiness"],
                            "autonomy_started": False,
                            "reconciled_after_handoff": True,
                        }
                        self.journals.transition(
                            operation_id,
                            state="completed",
                            checkpoint="receiver-handoff-committed",
                            cancellation_safe=False,
                            event="receiver-handoff-reconciled",
                            evidence_hash=content_identity(result),
                            result=result,
                        )
                        if lease is not None and lease["operation_id"] == operation_id:
                            self.control.release(operation_id)
                            lease = None
                        recovered.append(operation_id)
                        continue
                    if update["stage"] == "reverted":
                        self.journals.transition(
                            operation_id,
                            state="failed",
                            checkpoint="receiver-handoff-reverted",
                            cancellation_safe=False,
                            event="receiver-handoff-reverted",
                            failure={
                                "code": "receiver-update-reverted",
                                "message": update["failure"]
                                or "receiver candidate did not become ready",
                            },
                        )
                        if lease is not None and lease["operation_id"] == operation_id:
                            self.control.release(operation_id)
                            lease = None
                        recovered.append(operation_id)
                        continue
                    if update["stage"] == "staged":
                        if self.schedule_receiver_handoff is None:
                            raise ContractError(
                                "receiver update handoff scheduler is unavailable"
                            )
                        self.schedule_receiver_handoff()
                    recovered.append(operation_id)
                    continue
                if journal["action"] == Action.NETWORK_APPLY.value:
                    if self.network_controller is None:
                        raise ContractError(
                            "receiver network controller is unavailable"
                        )
                    network = self.network_controller.status(operation_id)
                    if network["state"] in {"confirmed", "reverted"}:
                        self.network_controller.resume_after_transaction()
                        if journal["state"] == "accepted":
                            journal = self.journals.transition(
                                operation_id,
                                state="running",
                                checkpoint="network-reconciliation",
                                cancellation_safe=False,
                                event="network-reconciliation-started",
                            )
                        if network["state"] == "confirmed":
                            result = {
                                "kind": "network-confirm",
                                "network_id": network["network_id"],
                                "state": "confirmed",
                                "reconciled_after_restart": True,
                            }
                            self.journals.transition(
                                operation_id,
                                state="completed",
                                checkpoint="network-confirmed",
                                cancellation_safe=False,
                                event="network-confirmation-reconciled",
                                evidence_hash=content_identity(result),
                                result=result,
                            )
                        else:
                            self.journals.transition(
                                operation_id,
                                state="failed",
                                checkpoint="automatic-network-rollback",
                                cancellation_safe=False,
                                event="network-rollback-reconciled",
                                failure={
                                    "code": "network-not-confirmed",
                                    "message": "the prior network profile was restored",
                                },
                            )
                        if lease is not None and lease["operation_id"] == operation_id:
                            self.control.release(operation_id)
                            lease = None
                        recovered.append(operation_id)
                        continue
                    if network["state"] == "pending-confirmation":
                        recovered.append(operation_id)
                        continue
                if (
                    journal["action"] == Action.HOST_REBOOT.value
                    and host_maintenance_recovery is not None
                    and host_maintenance_recovery.get("maintenance_id")
                    == journal["plan"]["parameters"]["maintenance_id"]
                    and host_maintenance_recovery.get("state")
                    in {"completed", "failed"}
                ):
                    if journal["state"] == "accepted":
                        journal = self.journals.transition(
                            operation_id,
                            state="running",
                            checkpoint="reconciliation",
                            cancellation_safe=False,
                            event="host-reboot-reconciliation-started",
                        )
                    maintenance = self.host_maintenance.status()["transaction"]
                    assert maintenance is not None
                    if host_maintenance_recovery["state"] == "completed":
                        result = {
                            "kind": "host-reboot",
                            "maintenance_id": maintenance["maintenance_id"],
                            "phase": maintenance["phase"],
                            "transaction_id": maintenance["transaction_id"],
                            "reboot_scheduled": True,
                            "reconciled_after_boot": True,
                        }
                        self.journals.transition(
                            operation_id,
                            state="completed",
                            checkpoint="complete",
                            cancellation_safe=False,
                            event="host-reboot-validated",
                            evidence_hash=content_identity(result),
                            result=result,
                        )
                    else:
                        self.journals.transition(
                            operation_id,
                            state="failed",
                            checkpoint="reconciliation",
                            cancellation_safe=False,
                            event="host-reboot-validation-failed",
                            failure={
                                "code": maintenance["failure"]["code"],
                                "message": maintenance["failure"]["message"],
                            },
                        )
                    if lease is not None and lease["operation_id"] == operation_id:
                        self.control.release(operation_id)
                        lease = None
                    recovered.append(operation_id)
                    continue
                if journal["action"] in {
                    Action.ACTIVATE.value,
                    Action.ROLLBACK.value,
                }:
                    activation = (
                        self.activation_coordinator.diagnostics.load_state(operation_id)
                        if self.activation_coordinator is not None
                        else None
                    )
                    if activation is not None and activation["stage"] in {
                        "accepted",
                        "rolled-back",
                        "faulted",
                    }:
                        if journal["state"] == "accepted":
                            journal = self.journals.transition(
                                operation_id,
                                state="running",
                                checkpoint="reconciliation",
                                cancellation_safe=False,
                                event="activation-reconciliation-started",
                            )
                        if activation["stage"] == "accepted":
                            result = {
                                "kind": journal["action"],
                                "release_id": activation["candidate"]["release_id"],
                                "previous_release_id": activation["previous"][
                                    "release_id"
                                ],
                                "accepted_state_id": activation["accepted_state_id"],
                                "acceptance_evidence_id": activation["evidence_id"],
                                "activation_state_id": activation["state_id"],
                                "automatic_rollback_permitted": False,
                                "autonomy_started": False,
                                "reconciled_after_boot": True,
                            }
                            self.journals.transition(
                                operation_id,
                                state="completed",
                                checkpoint="complete",
                                cancellation_safe=False,
                                event="activation-acceptance-reconciled",
                                evidence_hash=content_identity(result),
                                result=result,
                            )
                        else:
                            self.journals.transition(
                                operation_id,
                                state="failed",
                                checkpoint="reconciliation",
                                cancellation_safe=False,
                                event="activation-rollback-reconciled",
                                failure={
                                    "code": "activation-not-accepted",
                                    "message": (
                                        "activation restored the previous selector"
                                        if activation["stage"] == "rolled-back"
                                        else "activation entered a visible fault"
                                    ),
                                },
                            )
                        if lease is not None and lease["operation_id"] == operation_id:
                            self.control.release(operation_id)
                            lease = None
                        recovered.append(operation_id)
                        continue
                if lease is None or lease["operation_id"] != operation_id:
                    self.journals.transition(
                        operation_id,
                        state="failed",
                        checkpoint="reconciliation",
                        cancellation_safe=False,
                        event="missing-lease",
                        failure={
                            "code": "missing-lease",
                            "message": "durable mutation lease is absent",
                        },
                    )
                    self.audit.append(
                        event="reconciliation",
                        outcome="failed",
                        operation_id=operation_id,
                        client_id=journal["client_id"],
                        action=journal["action"],
                        detail_code="nonterminal-journal-missing-lease",
                    )
                    failed.append(operation_id)
                    continue
                self._submit(operation_id)
                recovered.append(operation_id)
            result = {
                "schema": RESULT_SCHEMA,
                "recovered_operations": recovered,
                "failed_operations": failed,
                "autonomy_started": False,
            }
            if activation_recovery is not None:
                result["activation_recovery"] = activation_recovery
            if host_maintenance_recovery is not None:
                result["host_maintenance_recovery"] = host_maintenance_recovery
            if network_recovery is not None:
                result["network_recovery"] = network_recovery
            return result

    def _status(self, operation_id: str) -> dict[str, Any]:
        journal = self.journals.load(operation_id)
        control = self.control.load()
        active_manifest = None
        manifest_provider = getattr(self.release_store, "active_release_manifest", None)
        if callable(manifest_provider):
            active_manifest = manifest_provider()
        activation_safety = None
        safety_provider = getattr(self.activation_coordinator, "safety_provider", None)
        if callable(safety_provider):
            observation = safety_provider()
            observation.validate()
            activation_safety = observation.as_document()
        backup_status = (
            self.backup_controller.status()
            if self.backup_controller is not None
            else {
                "schema": "iii.portable-backup-status/v1",
                "backup_fresh": False,
                "state": "unavailable",
            }
        )
        return {
            "schema": RESULT_SCHEMA,
            "receiver_generation": self.receiver_generation,
            "receiver_update": (
                self.receiver_slots.update_state()
                if self.receiver_slots is not None
                else None
            ),
            "target": {
                "logical_id": self.logical_target,
                "profile": self.profile,
            },
            "operation": journal,
            "lease": control["lease"],
            "recovery": self.release_store.state()["recovery"],
            "boot_id": self.control.boot_id(),
            "live_state": self._live_state(),
            "active_release_manifest": active_manifest,
            "activation_safety": activation_safety,
            "clock": (
                self.clock_controller.status()
                if self.clock_controller is not None
                else {"schema": "iii.receiver-clock-status/v1", "gate": "UNAVAILABLE"}
            ),
            "host_maintenance": (
                self.host_maintenance.status()
                if self.host_maintenance is not None
                else {
                    "schema": "iii.host-maintenance-status/v1",
                    "state": "unavailable",
                }
            ),
            "portable_backup": (backup_status),
            "backup_fresh": backup_status["backup_fresh"],
            "autonomy_started": False,
        }

    @staticmethod
    def _result(request: Request, **fields: Any) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "operation_id": request.operation_id,
            "action": request.action.value,
            **fields,
        }
