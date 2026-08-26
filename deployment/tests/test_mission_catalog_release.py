from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.mission_catalog import (
    install_qualified_mission_catalog,
    verify_mission_catalog,
)


def _write_catalog(directory: Path, *, scope: str, classification: str = "production") -> dict:
    assets = directory / "assets/sha256"
    assets.mkdir(parents=True)
    raw_asset = b"mission\n"
    asset_id = "sha256:" + hashlib.sha256(raw_asset).hexdigest()
    (assets / asset_id.removeprefix("sha256:")).write_bytes(raw_asset)
    models_raw = b"<root/>\n"
    state = {
        "schema": "iii.mission-source-state/v1",
        "state_hash": "",
        "source_files": {"mission_specification/mission.yaml": asset_id},
        "interface_contracts": {"MissionModeStatus.msg": "sha256:" + "1" * 64},
        "registration_manifest_sha256": "sha256:" + "2" * 64,
        "behavior_node_contract_sha256": "sha256:" + "3" * 64,
        "models_xml_sha256": "sha256:" + hashlib.sha256(models_raw).hexdigest(),
    }
    state["state_hash"] = "sha256:" + content_identity({k: v for k, v in state.items() if k != "state_hash"})
    profiles = ["hil", "opti_track", "real"] if scope != "local" else ["hil", "opti_track", "real", "sim"]
    entry = {
        "schema": "iii.mission-catalog-entry/v1",
        "id": "inspection-production" if classification == "production" else "inspection-test",
        "entry_hash": "",
        "classification": classification,
        "status": "active",
        "profiles": profiles,
        "default_for": ["opti_track", "real"] if classification == "production" else [],
        "experimental_warning": None,
        "compatibility": {"source_state_sha256": state["state_hash"]},
        "specification": {"executor_owned_mode": "inspection", "entries": [], "intent_services": []},
        "assets": [
            {
                "kind": "mission_specification",
                "logical_name": "mission_specification/mission.yaml",
                "content_hash": asset_id,
                "asset_id": asset_id,
            }
        ],
        "dependencies": [asset_id],
    }
    entry["entry_hash"] = "sha256:" + content_identity({k: v for k, v in entry.items() if k != "entry_hash"})
    profile_map = {
        profile: {
            "commissioned": profile in {"opti_track", "real", "sim"},
            "onboard": profile != "sim",
            "default_entry_id": entry["id"] if profile in entry["default_for"] else None,
        }
        for profile in profiles
    }
    catalog = {
        "schema": "iii.mission-catalog/v1",
        "catalog_hash": "",
        "scope": scope,
        "compatibility": {"source_state_sha256": state["state_hash"]},
        "profiles": profile_map,
        "entries": [entry],
        "assets": entry["assets"],
    }
    if scope == "field":
        included = [entry["id"]] if classification == "experimental" else []
        catalog["field_selection"] = {
            "included_experimental": included,
            "warning": "EXPERIMENTAL mission included" if included else None,
        }
    catalog["catalog_hash"] = "sha256:" + content_identity({k: v for k, v in catalog.items() if k != "catalog_hash"})
    catalog_raw = canonical_json(catalog) + b"\n"
    (directory / "catalog.json").write_bytes(catalog_raw)
    (directory / "catalog.sha256").write_text(hashlib.sha256(catalog_raw).hexdigest() + "  catalog.json\n")
    (directory / "source-state.json").write_bytes(canonical_json(state) + b"\n")
    (directory / "models.xml").write_bytes(models_raw)
    project = {
        "schema": "iii.groot2-project/v1",
        "catalog": "catalog.json",
        "node_model": "models.xml",
        "catalog_hash": catalog["catalog_hash"],
    }
    (directory / "groot2-project.json").write_bytes(canonical_json(project) + b"\n")
    return catalog


def test_qualified_install_removes_local_variants_and_preserves_exact_identity(tmp_path: Path):
    install = tmp_path / "install"
    share = install / "iii_drone_mission/share/iii_drone_mission"
    _write_catalog(share / "mission_catalog", scope="local", classification="test")
    qualified = _write_catalog(
        share / "mission_catalog_variants/qualified", scope="qualified"
    )
    _write_catalog(
        share / "mission_catalog_variants/field-candidates", scope="field-candidates"
    )
    identity = install_qualified_mission_catalog(install)
    assert identity["catalog_hash"] == qualified["catalog_hash"]
    assert identity["scope"] == "qualified"
    assert not (share / "mission_catalog_variants").exists()
    installed = json.loads((share / "mission_catalog/catalog.json").read_text())
    assert {entry["classification"] for entry in installed["entries"]} == {"production"}
    assert "sim" not in installed["profiles"]


def test_release_verifier_rejects_tamper_test_content_and_linked_assets(tmp_path: Path):
    directory = tmp_path / "catalog"
    _write_catalog(directory, scope="qualified")
    verify_mission_catalog(directory, expected_scope="qualified")
    asset = next((directory / "assets/sha256").iterdir())
    asset.write_text("tampered\n")
    with pytest.raises(ContractError, match="differs from identity"):
        verify_mission_catalog(directory, expected_scope="qualified")

    shutil.rmtree(directory)
    _write_catalog(directory, scope="qualified", classification="test")
    with pytest.raises(ContractError, match="non-production"):
        verify_mission_catalog(directory, expected_scope="qualified")

    shutil.rmtree(directory)
    _write_catalog(directory, scope="qualified")
    asset = next((directory / "assets/sha256").iterdir())
    raw = asset.read_bytes()
    asset.unlink()
    target = tmp_path / "external"
    target.write_bytes(raw)
    asset.symlink_to(target)
    with pytest.raises(ContractError, match="differs from identity|missing, extra, or linked"):
        verify_mission_catalog(directory, expected_scope="qualified")


def test_release_verifier_rejects_derived_asset_tamper(tmp_path: Path):
    directory = tmp_path / "catalog"
    _write_catalog(directory, scope="qualified")
    (directory / "models.xml").write_text("<tampered/>\n")
    with pytest.raises(ContractError, match="behavior-node model differs"):
        verify_mission_catalog(directory, expected_scope="qualified")

    shutil.rmtree(directory)
    _write_catalog(directory, scope="qualified")
    project_path = directory / "groot2-project.json"
    project = json.loads(project_path.read_text())
    project["catalog_hash"] = "sha256:" + "0" * 64
    project_path.write_bytes(canonical_json(project) + b"\n")
    with pytest.raises(ContractError, match="not bound"):
        verify_mission_catalog(directory, expected_scope="qualified")


def test_field_catalog_requires_explicit_experimental_selection_and_warning(tmp_path: Path):
    directory = tmp_path / "field"
    _write_catalog(directory, scope="field", classification="experimental")
    identity = verify_mission_catalog(directory, expected_scope="field")
    assert identity["scope"] == "field"
    catalog = json.loads((directory / "catalog.json").read_text())
    catalog["field_selection"]["warning"] = None
    catalog["catalog_hash"] = "sha256:" + content_identity(
        {k: v for k, v in catalog.items() if k != "catalog_hash"}
    )
    raw = canonical_json(catalog) + b"\n"
    (directory / "catalog.json").write_bytes(raw)
    (directory / "catalog.sha256").write_text(hashlib.sha256(raw).hexdigest() + "  catalog.json\n")
    with pytest.raises(ContractError, match="omits the experimental warning"):
        verify_mission_catalog(directory, expected_scope="field")
