from __future__ import annotations

import json
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.filesystem import (
    FilesystemContract,
    StorageProjection,
    assert_regular_safe_tree,
    ensure_storage_reserve,
)
from iii_deployment.receiver.protocol import Request

ROOT = Path(__file__).resolve().parents[2]


def test_filesystem_contract_materializes_only_under_temporary_root(
    tmp_path: Path,
) -> None:
    contract = FilesystemContract.load(ROOT / "deployment" / "filesystem-contract.json")
    contract.materialize_for_test(tmp_path)
    assert contract.validate_test_root(tmp_path) == []
    assert (tmp_path / "opt/iii/releases").is_dir()
    assert (tmp_path / "var/lib/iii").is_dir()
    assert not (tmp_path / "opt/iii/current").exists()


def test_release_and_persistent_roots_are_disjoint() -> None:
    contract = FilesystemContract.load(ROOT / "deployment" / "filesystem-contract.json")
    paths = {str(item.path): item.kind for item in contract.paths}
    assert paths["/opt/iii/releases"] == "immutable-release-root"
    assert paths["/var/lib/iii"] == "persistent-state"
    assert not Path("/var/lib/iii").is_relative_to(Path("/opt/iii/releases"))
    assert {"manifest.json", "bundle-manifest.json", "release-manifest.json"}.issubset(
        contract.protected_release_subpaths
    )


def test_forced_command_upload_root_is_private_and_reachable() -> None:
    value = json.loads(
        (ROOT / "deployment/filesystem-contract.json").read_text(encoding="utf-8")
    )
    paths = {item["path"]: item for item in value["paths"]}

    assert paths["/var/lib/iii"]["mode"] == "0751"
    assert paths["/var/lib/iii/incoming"] == {
        "path": "/var/lib/iii/incoming",
        "owner": "iii-deploy",
        "group": "iii-deploy",
        "mode": "0700",
        "kind": "unprivileged-upload",
        "persistence": "bounded-partial",
    }
    assert paths["/run/iii"]["mode"] == "0751"
    assert paths["/run/iii/deployment-upload"] == {
        "path": "/run/iii/deployment-upload",
        "owner": "iii-deploy",
        "group": "iii-deploy",
        "mode": "0700",
        "kind": "unprivileged-upload-lock",
        "persistence": "recreated-on-boot",
    }


def test_storage_projection_enforces_greater_of_absolute_or_percent_reserve() -> None:
    projection = StorageProjection(1, 2, 3, 4, 5, 6)
    assert (
        ensure_storage_reserve(
            projection, available_bytes=5 * 1024**3, filesystem_bytes=20 * 1024**3
        )
        > 0
    )
    with pytest.raises(ContractError, match="insufficient"):
        ensure_storage_reserve(
            projection, available_bytes=2 * 1024**3, filesystem_bytes=20 * 1024**3
        )
    with pytest.raises(ContractError, match="insufficient"):
        ensure_storage_reserve(
            projection, available_bytes=15 * 1024**3, filesystem_bytes=150 * 1024**3
        )


def test_candidate_tree_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "ok").write_text("ok", encoding="utf-8")
    assert_regular_safe_tree(root)
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(ContractError, match="unsafe"):
        assert_regular_safe_tree(root)


def _request(action: str, nonce: str | None, payload: dict | None = None) -> bytes:
    return canonical_json(
        {
            "protocol_version": "1",
            "action": action,
            "operation_id": "operation-123",
            "client_id": "a" * 64,
            "payload": payload or {},
            "nonce": nonce,
        }
    )


def _px4_evidence(release_id: str) -> dict:
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
    snapshot_id = content_identity(
        {
            "profile": "real",
            "target": target,
            "parameter_count": 1,
            "parameters": parameters,
        }
    )
    snapshot = {
        "schema": "iii.px4-parameter-snapshot/v1",
        "snapshot_id": snapshot_id,
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
        "manifest_id": "d" * 64,
        "snapshot_id": snapshot_id,
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
        "manifest_id": "d" * 64,
        "snapshot": snapshot,
        "comparison": comparison,
        "healthy": True,
        "writes_performed": 0,
    }
    evidence["evidence_id"] = content_identity(
        {key: value for key, value in evidence.items() if key != "evidence_id"}
    )
    return evidence


def test_receiver_protocol_exposes_only_fixed_actions() -> None:
    assert Request.parse(_request("status", None)).action.value == "status"
    planned = Request.parse(
        _request(
            "plan-activate",
            None,
            {
                "activation": {
                    "release_id": "b" * 64,
                    "configuration_checkpoint_id": "c" * 64,
                    "explicit_qualified_action": False,
                    "px4_activation_evidence": _px4_evidence("b" * 64),
                },
                "target": {"logical_id": "drone", "profile": "real"},
            },
        )
    )
    assert planned.action.value == "plan-activate"
    with pytest.raises(ContractError, match="unsupported receiver action"):
        Request.parse(_request("run-shell", "b" * 64, {"command": "id"}))


def test_receiver_protocol_rejects_tampered_px4_activation_inventory() -> None:
    evidence = _px4_evidence("b" * 64)
    evidence["snapshot"]["parameters"][0]["value"] = 0
    evidence["evidence_id"] = content_identity(
        {key: value for key, value in evidence.items() if key != "evidence_id"}
    )
    with pytest.raises(ContractError, match="snapshot identity mismatch"):
        Request.parse(
            _request(
                "plan-activate",
                None,
                {
                    "activation": {
                        "release_id": "b" * 64,
                        "configuration_checkpoint_id": "c" * 64,
                        "explicit_qualified_action": False,
                        "px4_activation_evidence": evidence,
                    },
                    "target": {"logical_id": "drone", "profile": "real"},
                },
            )
        )


def test_receiver_protocol_rejects_path_traversal_and_missing_nonce() -> None:
    with pytest.raises(ContractError, match="forbidden path"):
        Request.parse(_request("activate", "b" * 64, {"path": "../../etc/shadow"}))
    with pytest.raises(ContractError, match="bound nonce"):
        Request.parse(_request("rollback", None))
