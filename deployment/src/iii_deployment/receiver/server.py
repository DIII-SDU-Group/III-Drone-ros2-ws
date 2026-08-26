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

from iii_deployment.bundle import load_bundle_limits
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.receiver.access import AccessManager
from iii_deployment.receiver.config import (
    AUDIT_PATH,
    AUTHORIZED_KEYS_PATH,
    BUNDLE_TRUST_PATH,
    CONFIG_PATH,
    INCOMING_ROOT,
    LIVE_STATE_PATH,
    LOCK_PATH,
    OPERATIONAL_POLICY_PATH,
    RECEIVER_ROOT,
    READINESS_PATH,
    SCHEMA_ROOT,
    SOCKET_PATH,
    STATE_ROOT,
    STATUS_TRUST_PATH,
    ReceiverConfig,
    assert_production_root,
    load_live_state,
)
from iii_deployment.receiver.engine import NONCE_EXPIRY_S, ReceiverEngine
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
    return value


def build_engine(config: ReceiverConfig) -> ReceiverEngine:
    policy = _operational_policy()
    registry = ContractRegistry(SCHEMA_ROOT)
    store = ReleaseStore(
        Path("/"),
        bundle_trust=BUNDLE_TRUST_PATH,
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
