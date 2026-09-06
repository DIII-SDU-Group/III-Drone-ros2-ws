from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.log_lifecycle import (
    DegradedClockRing,
    LogInventory,
    LogPolicy,
    LogTransferStore,
    SessionLogStore,
)

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"


def _session(
    store: SessionLogStore,
    number: int,
    *,
    completed_days_ago: int | None,
    debug: bool = False,
) -> str:
    session_id = f"session-{number:02d}"
    store.begin(
        session_id=session_id,
        boot_id=f"boot-{number}",
        started_monotonic_ns=number,
        debug_enabled=debug,
    )
    store.append(session_id, source="runtime", record={"sequence": number})
    if completed_days_ago is not None:
        completed = NOW - timedelta(days=completed_days_ago)
        store.complete(
            session_id,
            completed_utc=completed.isoformat().replace("+00:00", "Z"),
        )
    return session_id


def test_multiboot_retention_preserves_current_and_four_newest_completed(
    tmp_path: Path,
) -> None:
    store = SessionLogStore(tmp_path / "logs", LogPolicy(retention_days=1))
    oldest = _session(store, 1, completed_days_ago=9)
    for number, age in enumerate((8, 7, 6, 5), start=2):
        _session(store, number, completed_days_ago=age)
    current = _session(store, 6, completed_days_ago=None)

    plan = store.retention_plan(
        now=NOW,
        filesystem_total_bytes=100 * 1024**3,
        filesystem_free_bytes=50 * 1024**3,
        deployment_reserve_bytes=2 * 1024**3,
    )

    assert plan["protected_session_ids"] == [
        "session-02",
        "session-03",
        "session-04",
        "session-05",
        current,
    ]
    assert [item["session_id"] for item in plan["remove"]] == [oldest]
    assert store.apply_retention(plan) == [oldest]


def test_retention_uses_lesser_global_cap_and_deployment_reserve(
    tmp_path: Path,
) -> None:
    policy = LogPolicy(
        retention_days=365,
        maximum_bytes=1000,
        maximum_filesystem_percent=5,
        protected_completed_sessions=0,
    )
    store = SessionLogStore(tmp_path / "logs", policy)
    for number in range(1, 4):
        session_id = _session(store, number, completed_days_ago=4 - number)
        store.session_root(session_id).joinpath("logs/runtime.jsonl").write_bytes(
            b"x" * 300
        )

    plan = store.retention_plan(
        now=NOW,
        filesystem_total_bytes=10_000,
        filesystem_free_bytes=200,
        deployment_reserve_bytes=150,
    )

    assert plan["cap_bytes"] == 500
    assert plan["projected_bytes"] <= 500
    assert plan["remove"]


def test_retention_rejects_content_changed_after_plan(tmp_path: Path) -> None:
    store = SessionLogStore(
        tmp_path / "logs",
        LogPolicy(retention_days=1, protected_completed_sessions=0),
    )
    session_id = _session(store, 1, completed_days_ago=2)
    plan = store.retention_plan(
        now=NOW,
        filesystem_total_bytes=100_000,
        filesystem_free_bytes=90_000,
        deployment_reserve_bytes=0,
    )
    with store.session_root(session_id).joinpath("logs/runtime.jsonl").open(
        "ab"
    ) as stream:
        stream.write(b"changed\n")
    with pytest.raises(ContractError, match="content changed"):
        store.apply_retention(plan)


def test_idle_transition_deduplication_and_debug_session_cap(tmp_path: Path) -> None:
    store = SessionLogStore(
        tmp_path / "logs",
        LogPolicy(debug_session_max_bytes=60),
    )
    session_id = _session(store, 1, completed_days_ago=None, debug=True)
    assert store.append(
        session_id,
        source="runtime",
        record={"state": "idle"},
        transition_key="runtime-state",
        transition_value="idle",
    )
    assert not store.append(
        session_id,
        source="runtime",
        record={"state": "idle"},
        transition_key="runtime-state",
        transition_value="idle",
    )
    store.append(session_id, source="runtime", record={"d": "x" * 20}, debug=True)
    with pytest.raises(ContractError, match="debug session log cap"):
        store.append(
            session_id,
            source="runtime",
            record={"d": "x" * 40},
            debug=True,
        )


def test_degraded_clock_ring_drops_oldest_and_flushes_exactly_once(
    tmp_path: Path,
) -> None:
    policy = LogPolicy(degraded_max_records=2, degraded_max_bytes=10_000)
    store = SessionLogStore(tmp_path / "logs", policy)
    session_id = _session(store, 1, completed_days_ago=None)
    ring = DegradedClockRing(boot_id="boot-1", policy=policy)
    for sequence in range(3):
        ring.append(
            monotonic_ns=100 + sequence,
            source="runtime",
            severity="info",
            message=str(sequence),
        )

    result = ring.flush(
        store,
        session_id,
        synchronized_monotonic_ns=100,
        synchronized_utc_ns=1_000,
        uncertainty_ns=7,
    )

    assert result == {
        "schema": "iii.preclock-flush/v1",
        "records_flushed": 2,
        "dropped_records": 1,
    }
    rows = [
        json.loads(line)
        for line in store.session_root(session_id)
        .joinpath("logs/preclock.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["message"] for row in rows[:-1]] == ["1", "2"]
    assert rows[0]["utc_lower_ns"] == 994
    assert rows[0]["utc_upper_ns"] == 1008
    assert rows[-1]["dropped_records"] == 1
    with pytest.raises(ContractError, match="already flushed"):
        ring.flush(
            store,
            session_id,
            synchronized_monotonic_ns=100,
            synchronized_utc_ns=1_000,
            uncertainty_ns=7,
        )


def test_transfer_is_verified_idempotent_and_receipt_bound(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "session.log").write_bytes(b"immutable log")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=["session.log"])
    assert transfer.create_manifest(domain="logs", locators=["session.log"]) == manifest
    chunk = transfer.chunk(
        manifest_id=manifest["manifest_id"],
        content_id=manifest["files"][0]["content_id"],
        offset=0,
        length=512,
    )
    assert base64.b64decode(chunk["data"]) == b"immutable log"
    verified = [
        {
            "locator": item["locator"],
            "content_id": item["content_id"],
            "size": item["size"],
        }
        for item in manifest["files"]
    ]
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=verified,
    )
    assert (
        transfer.receipt(
            manifest_id=manifest["manifest_id"],
            client_id="a" * 64,
            verified_files=verified,
        )
        == receipt
    )
    plan = transfer.prune_plan(receipt_id=receipt["receipt_id"])
    assert transfer.apply_prune(plan) == ["session.log"]
    assert not (source / "session.log").exists()


def test_interrupted_multifile_prune_resumes_from_durable_quarantine(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.log").write_bytes(b"one")
    (source / "two.log").write_bytes(b"two")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=["one.log", "two.log"])
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=[
            {
                "locator": item["locator"],
                "content_id": item["content_id"],
                "size": item["size"],
            }
            for item in manifest["files"]
        ],
    )
    plan = transfer.prune_plan(receipt_id=receipt["receipt_id"])
    real_replace = os.replace

    def interrupt_second(source_path, destination_path):
        if Path(source_path) == source / "two.log":
            raise OSError("simulated power loss")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", interrupt_second)
    with pytest.raises(OSError, match="power loss"):
        transfer.apply_prune(plan)
    assert not (source / "one.log").exists()
    assert (source / "two.log").is_file()
    state = json.loads(
        (tmp_path / f"state/prunes/{plan['plan_id']}/state.json").read_text()
    )
    assert state["state"] == "moving"

    monkeypatch.setattr(os, "replace", real_replace)
    assert transfer.apply_prune(plan) == ["one.log", "two.log"]
    assert transfer.apply_prune(plan) == ["one.log", "two.log"]
    assert not (source / "two.log").exists()
    state = json.loads(
        (tmp_path / f"state/prunes/{plan['plan_id']}/state.json").read_text()
    )
    assert state["state"] == "completed"
    assert not (tmp_path / f"state/prunes/{plan['plan_id']}/files").exists()


def test_corrupt_or_incomplete_pull_never_creates_receipt(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    path = source / "session.log"
    path.write_bytes(b"original")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=["session.log"])
    with pytest.raises(ContractError, match="incomplete or mismatched"):
        transfer.receipt(
            manifest_id=manifest["manifest_id"],
            client_id="a" * 64,
            verified_files=[],
        )
    assert not (tmp_path / "state/receipts").exists()
    path.write_bytes(b"corrupt")
    chunk = transfer.chunk(
        manifest_id=manifest["manifest_id"],
        content_id=hashlib.sha256(b"original").hexdigest(),
        offset=0,
        length=100,
    )
    assert base64.b64decode(chunk["data"]) == b"original"
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=[
            {
                "locator": "session.log",
                "content_id": hashlib.sha256(b"original").hexdigest(),
                "size": len(b"original"),
            }
        ],
    )
    with pytest.raises(ContractError, match="changed before prune"):
        transfer.apply_prune(transfer.prune_plan(receipt_id=receipt["receipt_id"]))


def test_receipt_distinguishes_multiple_files_with_identical_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.log").write_bytes(b"")
    (source / "two.log").write_bytes(b"")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=["one.log", "two.log"])
    verified = [
        {
            "locator": item["locator"],
            "content_id": item["content_id"],
            "size": item["size"],
        }
        for item in manifest["files"]
    ]
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=verified,
    )
    assert [item["locator"] for item in receipt["files"]] == ["one.log", "two.log"]


def test_export_snapshot_refuses_deployment_reserve_violation(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.log").write_bytes(b"12345")
    usage = shutil._ntuple_diskusage(total=100, used=90, free=10)
    monkeypatch.setattr(
        "iii_deployment.log_lifecycle.shutil.disk_usage", lambda _path: usage
    )
    transfer = LogTransferStore(
        source_root=source,
        state_root=tmp_path / "state",
        minimum_reserve_bytes=8,
    )
    with pytest.raises(ContractError, match="storage reserve"):
        transfer.create_manifest(domain="logs", locators=["large.log"])


@pytest.mark.parametrize(
    "locator",
    [
        "rosbags/flight.db3",
        "datasets/capture.bin",
        "tuning/journal.json",
        "configuration/shadow-checkpoint.json",
    ],
)
def test_protected_domain_is_never_prunable(tmp_path: Path, locator: str) -> None:
    source = tmp_path / "source"
    path = source / locator
    path.parent.mkdir(parents=True)
    path.write_bytes(b"protected")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=[locator])
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=[
            {
                "locator": manifest["files"][0]["locator"],
                "content_id": manifest["files"][0]["content_id"],
                "size": manifest["files"][0]["size"],
            }
        ],
    )
    plan = transfer.prune_plan(receipt_id=receipt["receipt_id"])
    assert plan["remove"] == []
    assert transfer.apply_prune(plan) == []
    assert path.exists()


def test_inventory_protects_current_active_recent_and_retained_release_evidence(
    tmp_path: Path,
) -> None:
    logs_root = tmp_path / "var/log/iii"
    session_store = SessionLogStore(logs_root, LogPolicy())
    current = _session(session_store, 1, completed_days_ago=None)
    completed = session_store.complete(current, completed_utc="2026-08-26T00:00:00Z")
    active_session = _session(session_store, 2, completed_days_ago=None)
    state = tmp_path / "var/lib/iii/deployment"
    activation = state / "activation"
    active_operation = "operation-active"
    recent_operation = "operation-recent"
    release_operation = "operation-release"
    old_operation = "operation-old"
    retained_release = "9" * 64
    for operation, value in (
        (active_operation, {"operation_id": active_operation}),
        (recent_operation, {"operation_id": recent_operation}),
        (
            release_operation,
            {"operation_id": release_operation, "release_id": retained_release},
        ),
        (old_operation, {"operation_id": old_operation}),
    ):
        path = activation / "transactions" / f"{operation}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
    audit = tmp_path / "var/log/iii/deployment/receiver-audit.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_bytes(b"audit\n")
    transfer = LogTransferStore(source_root=tmp_path, state_root=state / "log-transfer")
    inventory = LogInventory(
        source_root=tmp_path,
        logs_root=logs_root,
        deployment_state_root=state,
        activation_root=activation,
        audit_path=audit,
        transfer=transfer,
        active_operation_ids=lambda: [active_operation],
        retained_release_ids=lambda: [retained_release],
        audit_operation_ids=lambda: [recent_operation],
    )

    log_manifest = inventory.create_manifest("logs")
    protected_logs = {
        item["locator"] for item in log_manifest["files"] if item["protected"]
    }
    assert any(active_session in locator for locator in protected_logs)
    assert not any(completed["session_id"] in locator for locator in protected_logs)

    diagnostic_manifest = inventory.create_manifest("diagnostics")
    protection = {
        item["locator"]: item["protected"] for item in diagnostic_manifest["files"]
    }
    assert protection["var/log/iii/deployment/receiver-audit.jsonl"] is True
    assert (
        protection[
            f"var/lib/iii/deployment/activation/transactions/{active_operation}.json"
        ]
        is True
    )
    assert (
        protection[
            f"var/lib/iii/deployment/activation/transactions/{recent_operation}.json"
        ]
        is True
    )
    assert (
        protection[
            f"var/lib/iii/deployment/activation/transactions/{release_operation}.json"
        ]
        is True
    )
    assert (
        protection[
            f"var/lib/iii/deployment/activation/transactions/{old_operation}.json"
        ]
        is False
    )


def test_lifecycle_documents_validate_against_shipped_schemas(tmp_path: Path) -> None:
    registry = ContractRegistry(SCHEMAS)
    session_store = SessionLogStore(tmp_path / "logs", LogPolicy())
    session_id = _session(session_store, 1, completed_days_ago=None)
    session = json.loads(
        (session_store.session_root(session_id) / "session.json").read_text()
    )
    registry.validate("log-session", session)
    retention = session_store.retention_plan(
        now=NOW,
        filesystem_total_bytes=100_000,
        filesystem_free_bytes=90_000,
        deployment_reserve_bytes=1_000,
    )
    registry.validate("log-retention-plan", retention)
    ring = DegradedClockRing(boot_id="boot-1", policy=LogPolicy())
    ring.append(monotonic_ns=1, source="runtime", severity="info", message="boot")
    flushed = ring.flush(
        session_store,
        session_id,
        synchronized_monotonic_ns=1,
        synchronized_utc_ns=10,
        uncertainty_ns=2,
    )
    registry.validate("preclock-flush", flushed)

    source = tmp_path / "source"
    source.mkdir()
    (source / "file.log").write_bytes(b"log")
    transfer = LogTransferStore(source_root=source, state_root=tmp_path / "state")
    manifest = transfer.create_manifest(domain="logs", locators=["file.log"])
    registry.validate("log-export-manifest", manifest)
    chunk = transfer.chunk(
        manifest_id=manifest["manifest_id"],
        content_id=manifest["files"][0]["content_id"],
        offset=0,
        length=100,
    )
    registry.validate("log-chunk", chunk)
    receipt = transfer.receipt(
        manifest_id=manifest["manifest_id"],
        client_id="a" * 64,
        verified_files=[
            {
                "locator": manifest["files"][0]["locator"],
                "content_id": manifest["files"][0]["content_id"],
                "size": manifest["files"][0]["size"],
            }
        ],
    )
    registry.validate("log-pull-receipt", receipt)
    registry.validate(
        "log-prune-plan", transfer.prune_plan(receipt_id=receipt["receipt_id"])
    )
