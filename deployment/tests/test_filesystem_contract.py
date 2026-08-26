from __future__ import annotations

import json
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.filesystem import (
    FilesystemContract,
    StorageProjection,
    assert_regular_safe_tree,
    ensure_storage_reserve,
)
from iii_deployment.receiver.protocol import Request


ROOT = Path(__file__).resolve().parents[2]


def test_filesystem_contract_materializes_only_under_temporary_root(tmp_path: Path) -> None:
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


def test_storage_projection_enforces_greater_of_absolute_or_percent_reserve() -> None:
    projection = StorageProjection(1, 2, 3, 4, 5, 6)
    assert ensure_storage_reserve(projection, available_bytes=5 * 1024**3, filesystem_bytes=20 * 1024**3) > 0
    with pytest.raises(ContractError, match="insufficient"):
        ensure_storage_reserve(projection, available_bytes=2 * 1024**3, filesystem_bytes=20 * 1024**3)
    with pytest.raises(ContractError, match="insufficient"):
        ensure_storage_reserve(projection, available_bytes=15 * 1024**3, filesystem_bytes=150 * 1024**3)


def test_candidate_tree_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "ok").write_text("ok", encoding="utf-8")
    assert_regular_safe_tree(root)
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(ContractError, match="unsafe"):
        assert_regular_safe_tree(root)


def _request(action: str, nonce: str | None, payload: dict | None = None) -> bytes:
    return canonical_json({
        "protocol_version": "1", "action": action, "operation_id": "operation-123",
        "client_id": "a" * 64, "payload": payload or {}, "nonce": nonce,
    })


def test_receiver_protocol_exposes_only_fixed_actions() -> None:
    assert Request.parse(_request("status", None)).action.value == "status"
    assert Request.parse(_request("activate", "b" * 64)).action.value == "activate"
    with pytest.raises(ContractError, match="unsupported receiver action"):
        Request.parse(_request("run-shell", "b" * 64, {"command": "id"}))


def test_receiver_protocol_rejects_path_traversal_and_missing_nonce() -> None:
    with pytest.raises(ContractError, match="forbidden path"):
        Request.parse(_request("activate", "b" * 64, {"path": "../../etc/shadow"}))
    with pytest.raises(ContractError, match="bound nonce"):
        Request.parse(_request("rollback", None))
