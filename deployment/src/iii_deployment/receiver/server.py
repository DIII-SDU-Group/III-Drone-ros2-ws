"""Root-owned Unix-socket receiver service entry point."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import time
from typing import Any, Mapping

from iii_deployment.bundle import load_bundle_limits
from iii_deployment.activation import ActivationTransactionStore
from iii_deployment.activation_health import (
    ActivationCoordinator,
    ActivationDiagnosticStore,
)
from iii_deployment.configuration_reconciliation import (
    ReceiverConfigurationReconciler,
)
from iii_deployment.activation_runtime import (
    OnboardControlPlane,
    OnboardHealthProvider,
    OnboardSafetyProvider,
)
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.log_lifecycle import LogInventory, LogTransferStore
from iii_deployment.host_maintenance import HostMaintenanceController
from iii_deployment.hardware_roles import HardwareInspector
from iii_deployment.boot_baseline import BootInspector
from iii_deployment.host_inspection import HostInspector
from iii_deployment.networking import NetworkController
from iii_deployment.portable_state import PortableBackupController
from iii_deployment.receiver.access import AccessManager
from iii_deployment.receiver.config import (
    AUDIT_PATH,
    AUTHORIZED_KEYS_PATH,
    BUNDLE_TRUST_PATH,
    CONFIG_PATH,
    HARDWARE_ROLE_MANIFEST_PATH,
    BOOT_PROFILE_PATH,
    CLOCK_STATE_PATH,
    FIELD_SIGNERS_PATH,
    INCOMING_ROOT,
    LIVE_STATE_PATH,
    LOG_ROOT,
    LOCK_PATH,
    OPERATIONAL_POLICY_PATH,
    RECEIVER_ROOT,
    READINESS_PATH,
    SCHEMA_ROOT,
    SOCKET_PATH,
    STATE_ROOT,
    STATUS_TRUST_PATH,
    RUNTIME_VERIFIERS_PATH,
    ReceiverConfig,
    assert_production_root,
    load_live_state,
)
from iii_deployment.receiver.engine import NONCE_EXPIRY_S, ReceiverEngine
from iii_deployment.receiver.clock import (
    ClockController,
    ClockFlushCoordinator,
    ClockRecoveryAudit,
)
from iii_deployment.receiver.state import (
    AuditLog,
    OperationJournalStore,
    ReceiverControlStore,
)
from iii_deployment.receiver.transport import UnixReceiverServer
from iii_deployment.receiver.update import READINESS_SCHEMA, ReceiverSlotStore
from iii_deployment.receiver.state import atomic_document
from iii_deployment.staging import ReleaseStore


def _operational_policy() -> dict:
    if OPERATIONAL_POLICY_PATH.is_symlink() or not OPERATIONAL_POLICY_PATH.is_file():
        raise ContractError("receiver operational policy is missing or linked")
    observed = OPERATIONAL_POLICY_PATH.stat(follow_symlinks=False)
    if observed.st_uid != 0 or observed.st_mode & 0o022:
        raise ContractError(
            "receiver operational policy is not root-owned and write-protected"
        )
    try:
        value = json.loads(OPERATIONAL_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load receiver operational policy: {exc}") from exc
    if value.get("authorization", {}).get("nonce_expiry_s") != NONCE_EXPIRY_S:
        raise ContractError(
            "receiver operational policy changes the fixed nonce expiry"
        )
    if value.get("activation") != {
        "target_acceptance_s": 60,
        "hard_deadline_s": 120,
        "rollback_target_s": 60,
    }:
        raise ContractError(
            "receiver operational policy changes the fixed activation deadlines"
        )
    if value.get("host_maintenance") != {
        "target_acceptance_s": 1800,
        "hard_deadline_s": 7200,
        "rollback_target_s": 60,
    }:
        raise ContractError(
            "receiver operational policy changes fixed host-maintenance deadlines"
        )
    if value.get("network") != {
        "confirmation_deadline_s": 90,
        "ethernet_dhcp_required": True,
    }:
        raise ContractError(
            "receiver operational policy changes fixed network recovery policy"
        )
    return value


def build_engine(config: ReceiverConfig) -> ReceiverEngine:
    policy = _operational_policy()
    registry = ContractRegistry(SCHEMA_ROOT)
    store = ReleaseStore(
        Path("/"),
        bundle_trust=BUNDLE_TRUST_PATH,
        field_trust=FIELD_SIGNERS_PATH,
        field_access_state=STATE_ROOT / "access-state.json",
        status_trust=STATUS_TRUST_PATH,
        registry=registry,
        host_limits=load_bundle_limits(OPERATIONAL_POLICY_PATH),
        minimum_reserve_bytes=policy["storage"]["minimum_reserve_bytes"],
        minimum_reserve_percent=policy["storage"]["minimum_reserve_percent"],
    )
    control = ReceiverControlStore(
        STATE_ROOT,
        receiver_generation=config.receiver_generation,
        nonce_expiry_s=NONCE_EXPIRY_S,
        monotonic=time.monotonic,
    )
    journals = OperationJournalStore(STATE_ROOT, monotonic=time.monotonic)
    audit = AuditLog(AUDIT_PATH, monotonic=time.monotonic)
    access = AccessManager(
        state_path=STATE_ROOT / "access-state.json",
        authorized_keys_path=AUTHORIZED_KEYS_PATH,
        registry=registry,
        runtime_verifiers_path=RUNTIME_VERIFIERS_PATH,
        field_signers_path=FIELD_SIGNERS_PATH,
        runtime_uid=config.runtime_uid,
        runtime_gid=config.runtime_gid,
    )
    slots = ReceiverSlotStore(
        Path("/"), trust={}, registry=ContractRegistry(SCHEMA_ROOT)
    )
    active_slot = slots.active_slot()
    if active_slot is None:
        raise ContractError("receiver A/B active slot is unavailable")
    receiver_manifest = slots.verify_slot(active_slot)
    if receiver_manifest["generation"] != config.receiver_generation:
        raise ContractError(
            "receiver configuration generation differs from the active immutable slot"
        )
    control_plane = OnboardControlPlane()
    safety_provider = OnboardSafetyProvider()
    hardware_inspector = HardwareInspector(
        manifest_path=HARDWARE_ROLE_MANIFEST_PATH,
        registry=registry,
        profile=config.profile,
    )
    boot_inspector = BootInspector(
        profile_path=BOOT_PROFILE_PATH,
        registry=registry,
    )
    host_inspector = HostInspector(
        logical_target=config.logical_target,
        profile=config.profile,
        hardware_inspector=hardware_inspector,
        boot_inspector=boot_inspector,
        registry=registry,
    )
    activation = ActivationCoordinator(
        release_store=store,
        transaction_store=ActivationTransactionStore(Path("/")),
        diagnostics=ActivationDiagnosticStore(STATE_ROOT / "activation"),
        safety_provider=safety_provider,
        health_provider=OnboardHealthProvider(
            receiver_id=receiver_manifest["receiver_id"],
            receiver_generation=receiver_manifest["generation"],
            control_plane=control_plane,
            hardware_roles_provider=hardware_inspector.health_roles,
        ),
        stop_all_units=control_plane.stop_all_units,
        start_control_plane=control_plane.start,
        receiver_id=receiver_manifest["receiver_id"],
        receiver_generation=receiver_manifest["generation"],
        bootstrap_protocol_version="1",
        logical_target=config.logical_target,
        profile=config.profile,
        monotonic=time.monotonic,
        sleep=time.sleep,
        boot_id=lambda: Path("/proc/sys/kernel/random/boot_id")
        .read_text(encoding="ascii")
        .strip(),
        configuration_reconciler=ReceiverConfigurationReconciler(
            releases_root=store.releases_root,
            checkpoints_root=Path("/var/lib/iii/configuration/checkpoints"),
            staging_root=Path("/var/lib/iii/configuration/staging"),
            operations_root=STATE_ROOT / "operations",
            target_id=config.logical_target,
            runtime_profile=config.profile,
        ),
    )
    clock = ClockController(
        CLOCK_STATE_PATH,
        flush_before_open=ClockFlushCoordinator(
            required_services=lambda: (
                ("system-daemon", "runtime-api")
                if load_live_state(LIVE_STATE_PATH, profile=config.profile).get(
                    "active_release_id"
                )
                else ()
            )
        ),
        gate_opened=lambda: (
            control_plane.boot_profile(config.profile)
            if load_live_state(LIVE_STATE_PATH, profile=config.profile).get(
                "active_release_id"
            )
            else {
                "schema": "iii.clock-gate-runtime-start/v1",
                "profile": config.profile,
                "unit_states": {},
                "autonomy_started": False,
                "reason": "no active release is selected",
            }
        ),
        maintenance_safe=safety_provider.maintenance_safe_for_clock_recovery,
        stop_runtime=control_plane.stop_runtime_graph,
        audit=ClockRecoveryAudit(),
    )
    log_transfer = LogTransferStore(
        source_root=Path("/"),
        state_root=STATE_ROOT / "log-transfer",
        minimum_reserve_bytes=policy["storage"]["minimum_reserve_bytes"],
        minimum_reserve_percent=policy["storage"]["minimum_reserve_percent"],
    )

    def retained_release_ids() -> set[str]:
        state = store.state()
        retained = {
            state.get("active_release_id"),
            state.get("rollback_release_id"),
            state.get("candidate_release_id"),
            state.get("qualified_anchor_release_id"),
            *state.get("field_history", []),
        }
        return {item for item in retained if isinstance(item, str)}

    log_inventory = LogInventory(
        source_root=Path("/"),
        logs_root=LOG_ROOT,
        deployment_state_root=STATE_ROOT,
        activation_root=STATE_ROOT / "activation",
        audit_path=AUDIT_PATH,
        transfer=log_transfer,
        active_operation_ids=lambda: (
            item["operation_id"]
            for item in journals.list()
            if item["state"] not in {"completed", "cancelled", "failed"}
        ),
        retained_release_ids=retained_release_ids,
        audit_operation_ids=lambda: [
            item["operation_id"]
            for item in audit.entries()
            if item["operation_id"] is not None
        ],
        deployment_audits=policy["logging"]["deployment_audits"],
    )
    host_maintenance = HostMaintenanceController(
        root=Path("/"),
        registry=registry,
        maintenance_safe=safety_provider.maintenance_safe_for_clock_recovery,
        stop_runtime=control_plane.stop_all_units,
        resume_runtime=lambda: control_plane.boot_profile(config.profile),
        active_operator_count=lambda: sum(
            1 for item in access.load()["clients"].values() if item["state"] == "active"
        ),
        recovery_validator=store.validate_protected_qualified_release,
    )
    network_controller = NetworkController(
        monotonic_ns=time.monotonic_ns,
        maintenance_safe=safety_provider.maintenance_safe_for_clock_recovery,
        stop_runtime=control_plane.stop_all_units,
        resume_runtime=lambda: control_plane.boot_profile(config.profile),
    )

    def backup_resume() -> dict[str, Any]:
        runtime = control_plane.boot_profile(config.profile)
        return {"standby_resumed": True, "runtime": runtime}

    def restore_host_clean() -> bool:
        return (
            not host_maintenance.status()["mutation_blocked"]
            and store.state().get("active_release_id") is not None
        )

    def restore_health() -> dict[str, Any]:
        protected = store.validate_protected_qualified_release()
        return {
            "healthy": True,
            "protected_release_id": protected["release_id"],
            "runtime_started": False,
        }

    backup_controller = PortableBackupController(
        source_root=Path("/"),
        policy_path=Path(
            "/opt/iii/receiver/selectors/current/share/iii-deployment/policy/portable-state-policy.json"
        ),
        logical_target=config.logical_target,
        profile=config.profile,
        active_release_id=lambda: store.state().get("active_release_id"),
        maintenance_safe=safety_provider.maintenance_safe_for_clock_recovery,
        quiesce_writers=lambda: {
            "writers_stopped": True,
            "flushed": True,
            "stopped_units": list(control_plane.stop_all_units()),
        },
        resume_standby=backup_resume,
        clean_converged_host=restore_host_clean,
        reconcile_restore=lambda path, manifest: {
            "compatible": manifest["portable_schema_version"] == 1,
            "staged_root": str(path),
            "schema_actions": [],
        },
        validate_health=restore_health,
    )
    return ReceiverEngine(
        release_store=store,
        control=control,
        journals=journals,
        audit=audit,
        access=access,
        incoming_root=INCOMING_ROOT,
        receiver_root=RECEIVER_ROOT,
        logical_target=config.logical_target,
        profile=config.profile,
        live_state=lambda: load_live_state(LIVE_STATE_PATH, profile=config.profile),
        maximum_claim_bytes=load_bundle_limits(OPERATIONAL_POLICY_PATH)[
            "unpacked_bytes"
        ]
        + 16 * 1024**2,
        activation_coordinator=activation,
        clock_controller=clock,
        log_inventory=log_inventory,
        log_transfer=log_transfer,
        host_maintenance=host_maintenance,
        hardware_inspector=hardware_inspector,
        host_inspector=host_inspector,
        network_controller=network_controller,
        backup_controller=backup_controller,
    )


def _acquire_singleton() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise ContractError(
            "another deployment receiver instance owns the host lock"
        ) from exc
    return descriptor


def assert_reconciliation_boot_safe(result: Mapping[str, Any]) -> None:
    """Refuse normal boot after a failed host-maintenance recovery check."""

    maintenance = result.get("host_maintenance_recovery")
    if isinstance(maintenance, Mapping) and maintenance.get("state") == "failed":
        raise ContractError(
            "host maintenance failed protected-release validation; "
            "keep runtime stopped and recover or reprovision"
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-deployment-receiver")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--reconcile-only", action="store_true")
    arguments = parser.parse_args()
    try:
        assert_production_root()
        config = ReceiverConfig.load(arguments.config, production=True)
        singleton = _acquire_singleton()
        engine = build_engine(config)
        result = engine.reconcile()
        if arguments.reconcile_only:
            assert_reconciliation_boot_safe(result)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            os.close(singleton)
            return 0
        server = UnixReceiverServer(
            socket_path=SOCKET_PATH,
            runtime_uid=config.runtime_uid,
            runtime_gid=config.runtime_gid,
            handler=engine.handle,
            rejection_logger=lambda code, _pid, _uid: engine.audit.append(
                event="transport",
                outcome="rejected",
                operation_id=None,
                client_id=None,
                action=None,
                detail_code=code,
            ),
        )
        server.open()
        slots = ReceiverSlotStore(
            Path("/"), trust={}, registry=ContractRegistry(SCHEMA_ROOT)
        )
        active_slot = slots.active_slot()
        if active_slot is None:
            raise ContractError("receiver A/B active slot is unavailable")
        active_manifest = slots.verify_slot(active_slot)
        atomic_document(
            READINESS_PATH,
            {
                "schema": READINESS_SCHEMA,
                "receiver_id": active_manifest["receiver_id"],
                "generation": active_manifest["generation"],
                "socket_open": True,
                "self_tests_passed": True,
                "journal_compatible": True,
                "bootstrap_protocol": "1",
                "cli_protocol": "1",
                "request_protocol": "1",
            },
            mode=0o640,
        )
        assert server.socket is not None
        server.socket.settimeout(1.0)
        stopped = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopped:
                try:
                    server.serve_once()
                except socket.timeout:
                    continue
        finally:
            server.close()
            READINESS_PATH.unlink(missing_ok=True)
            fcntl.flock(singleton, fcntl.LOCK_UN)
            os.close(singleton)
        return 0
    except ContractError as exc:
        parser.error(str(exc))
    return 64
