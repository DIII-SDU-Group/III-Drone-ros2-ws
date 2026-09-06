"""Release-bound verification and qualified installation of mission catalogs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_identity


CATALOG_SCHEMA = "iii.mission-catalog/v1"
SOURCE_STATE_SCHEMA = "iii.mission-source-state/v1"
HASH_PREFIX = "sha256:"
ONBOARD_PROFILES = {"real", "opti_track", "hil"}
GROOT_PROJECT_SCHEMA = "iii.groot2-project/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value, raw


def _hash_identity(value: Mapping[str, Any], identity_field: str) -> str:
    return HASH_PREFIX + content_identity({key: item for key, item in value.items() if key != identity_field})


def verify_mission_catalog(directory: Path, *, expected_scope: str) -> dict[str, Any]:
    """Verify a release-bound catalog and return its trusted public identity."""

    if directory.is_symlink() or not directory.is_dir():
        raise ContractError("mission catalog directory is missing or linked")
    directory = directory.resolve(strict=True)
    catalog, catalog_bytes = _canonical_document(directory / "catalog.json", label="mission catalog")
    if catalog.get("schema") != CATALOG_SCHEMA or catalog.get("scope") != expected_scope:
        raise ContractError(f"mission catalog must be {CATALOG_SCHEMA} scope {expected_scope}")
    if catalog.get("catalog_hash") != _hash_identity(catalog, "catalog_hash"):
        raise ContractError("mission catalog logical identity mismatch")
    checksum = directory / "catalog.sha256"
    expected_checksum = hashlib.sha256(catalog_bytes).hexdigest() + "  catalog.json\n"
    if checksum.is_symlink() or not checksum.is_file() or checksum.read_text(encoding="ascii") != expected_checksum:
        raise ContractError("mission catalog byte checksum mismatch")

    state, _state_bytes = _canonical_document(directory / "source-state.json", label="mission source state")
    if state.get("schema") != SOURCE_STATE_SCHEMA or state.get("state_hash") != _hash_identity(state, "state_hash"):
        raise ContractError("mission source-state identity mismatch")
    if catalog.get("compatibility", {}).get("source_state_sha256") != state["state_hash"]:
        raise ContractError("mission catalog/source-state binding mismatch")

    entries = catalog.get("entries")
    assets = catalog.get("assets")
    profiles = catalog.get("profiles")
    if not isinstance(entries, list) or not entries or not isinstance(assets, list) or not isinstance(profiles, dict):
        raise ContractError("mission catalog entries, assets, or profiles are malformed")
    if expected_scope == "qualified" and any(entry.get("classification") != "production" for entry in entries):
        raise ContractError("qualified mission catalog contains non-production content")
    if expected_scope == "field" and any(
        entry.get("classification") not in {"production", "experimental"} for entry in entries
    ):
        raise ContractError("field mission catalog contains test, legacy, or unclassified content")
    if expected_scope == "field":
        selection = catalog.get("field_selection")
        if not isinstance(selection, dict) or not isinstance(selection.get("included_experimental"), list):
            raise ContractError("field mission catalog lacks explicit experimental selection metadata")
        included = sorted(
            entry["id"] for entry in entries if entry.get("classification") == "experimental"
        )
        if selection["included_experimental"] != included:
            raise ContractError("field mission catalog experimental selection does not match its entries")
        if included and not selection.get("warning"):
            raise ContractError("field mission catalog omits the experimental warning")
    if expected_scope != "local" and any(set(entry.get("profiles", ())) - ONBOARD_PROFILES for entry in entries):
        raise ContractError("onboard mission catalog exposes a non-onboard profile")
    if expected_scope != "local" and set(profiles) - ONBOARD_PROFILES:
        raise ContractError("onboard mission catalog contains non-onboard profile metadata")

    asset_ids: set[str] = set()
    for asset in assets:
        asset_id = asset.get("asset_id") if isinstance(asset, dict) else None
        if not isinstance(asset_id, str) or not asset_id.startswith(HASH_PREFIX) or len(asset_id) != 71:
            raise ContractError("mission catalog contains a malformed asset identity")
        if asset_id in asset_ids or asset.get("content_hash") != asset_id:
            raise ContractError("mission catalog contains duplicate or inconsistent assets")
        asset_ids.add(asset_id)
        path = directory / "assets/sha256" / asset_id.removeprefix(HASH_PREFIX)
        if path.is_symlink() or not path.is_file() or _sha256(path) != asset_id.removeprefix(HASH_PREFIX):
            raise ContractError(f"mission catalog asset differs from identity: {asset_id}")

    entry_ids: set[str] = set()
    referenced: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in entry_ids:
            raise ContractError("mission catalog contains malformed or duplicate entries")
        entry_ids.add(entry["id"])
        if entry.get("entry_hash") != _hash_identity(entry, "entry_hash"):
            raise ContractError(f"mission catalog entry identity mismatch: {entry['id']}")
        dependencies = entry.get("dependencies")
        if not isinstance(dependencies, list) or any(item not in asset_ids for item in dependencies):
            raise ContractError(f"mission catalog entry dependency closure is incomplete: {entry['id']}")
        referenced.update(dependencies)
    if referenced != asset_ids:
        raise ContractError("mission catalog contains unreferenced or missing assets")
    disk_assets = {
        HASH_PREFIX + path.name
        for path in (directory / "assets/sha256").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if disk_assets != asset_ids:
        raise ContractError("mission catalog asset directory has missing, extra, or linked content")
    models_path = directory / "models.xml"
    if models_path.is_symlink() or not models_path.is_file():
        raise ContractError("mission catalog derived asset is missing or linked: models.xml")
    expected_models_hash = state.get("models_xml_sha256")
    if expected_models_hash != HASH_PREFIX + _sha256(models_path):
        raise ContractError("mission catalog behavior-node model differs from source state")
    project, _project_bytes = _canonical_document(
        directory / "groot2-project.json", label="mission Groot project"
    )
    expected_project = {
        "schema": GROOT_PROJECT_SCHEMA,
        "catalog": "catalog.json",
        "node_model": "models.xml",
        "catalog_hash": catalog["catalog_hash"],
    }
    if project != expected_project:
        raise ContractError("mission Groot project is not bound to the catalog and node model")
    return {
        "schema_version": 1,
        "scope": expected_scope,
        "catalog_hash": catalog["catalog_hash"],
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "source_state_sha256": state["state_hash"],
        "entries": sorted(entry_ids),
        "included_experimental": (
            list(catalog["field_selection"]["included_experimental"])
            if expected_scope == "field"
            else []
        ),
        "profiles": profiles,
        "assets": len(asset_ids),
    }


def install_qualified_mission_catalog(install_root: Path) -> dict[str, Any]:
    """Replace the local build catalog with the verified qualified reduction."""

    share = install_root / "iii_drone_mission/share/iii_drone_mission"
    active = share / "mission_catalog"
    variants = share / "mission_catalog_variants"
    qualified = variants / "qualified"
    verify_mission_catalog(active, expected_scope="local")
    identity = verify_mission_catalog(qualified, expected_scope="qualified")
    staging = Path(tempfile.mkdtemp(prefix=".qualified-mission-catalog-", dir=share))
    try:
        shutil.copytree(qualified, staging / "catalog", copy_function=shutil.copy2)
        verify_mission_catalog(staging / "catalog", expected_scope="qualified")
        previous = share / ".local-mission-catalog"
        if previous.exists() or previous.is_symlink():
            raise ContractError("temporary mission catalog promotion path already exists")
        os.replace(active, previous)
        try:
            os.replace(staging / "catalog", active)
        except Exception:
            os.replace(previous, active)
            raise
        shutil.rmtree(previous)
        shutil.rmtree(variants)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if variants.exists() or variants.is_symlink():
        raise ContractError("drone install retained local mission catalog variants")
    observed = verify_mission_catalog(active, expected_scope="qualified")
    if observed != identity:
        raise ContractError("promoted mission catalog identity changed during installation")
    return observed


def install_field_mission_catalog(
    install_root: Path, *, include_experimental: Sequence[str] = ()
) -> dict[str, Any]:
    """Replace build-only catalogs with one explicit field catalog.

    Field payloads always carry a field-scoped catalog, even when the explicit
    experimental selection is empty.  This prevents a dirty build from being
    packaged with qualified mission metadata while retaining the exact same
    content-addressed asset rules used by qualified releases.
    """

    share = install_root / "iii_drone_mission/share/iii_drone_mission"
    active = share / "mission_catalog"
    variants = share / "mission_catalog_variants"
    candidates = variants / "field-candidates"
    verify_mission_catalog(active, expected_scope="local")
    verify_mission_catalog(candidates, expected_scope="field-candidates")
    candidate_catalog, _ = _canonical_document(
        candidates / "catalog.json", label="field-candidate mission catalog"
    )
    requested = sorted(set(include_experimental))
    experimental = {
        entry["id"]
        for entry in candidate_catalog["entries"]
        if entry.get("classification") == "experimental"
    }
    unknown = sorted(set(requested) - experimental)
    if unknown:
        raise ContractError(
            "unknown or non-experimental field mission IDs: " + ", ".join(unknown)
        )
    entries = [
        entry
        for entry in candidate_catalog["entries"]
        if entry.get("classification") == "production" or entry["id"] in requested
    ]
    referenced = {asset_id for entry in entries for asset_id in entry["dependencies"]}
    assets = [
        asset for asset in candidate_catalog["assets"] if asset["asset_id"] in referenced
    ]
    catalog = {
        **candidate_catalog,
        "catalog_hash": "",
        "scope": "field",
        "entries": entries,
        "assets": assets,
        "field_selection": {
            "included_experimental": requested,
            "warning": (
                "EXPERIMENTAL missions are included in this field-development "
                "catalog and are not qualified."
                if requested
                else None
            ),
        },
    }
    catalog["catalog_hash"] = _hash_identity(catalog, "catalog_hash")
    staging = Path(tempfile.mkdtemp(prefix=".field-mission-catalog-", dir=share))
    try:
        output = staging / "catalog"
        output_assets = output / "assets/sha256"
        output_assets.mkdir(parents=True)
        catalog_bytes = canonical_json(catalog) + b"\n"
        (output / "catalog.json").write_bytes(catalog_bytes)
        (output / "catalog.sha256").write_text(
            hashlib.sha256(catalog_bytes).hexdigest() + "  catalog.json\n",
            encoding="ascii",
        )
        shutil.copy2(candidates / "source-state.json", output / "source-state.json")
        shutil.copy2(candidates / "models.xml", output / "models.xml")
        project = {
            "schema": GROOT_PROJECT_SCHEMA,
            "catalog": "catalog.json",
            "node_model": "models.xml",
            "catalog_hash": catalog["catalog_hash"],
        }
        (output / "groot2-project.json").write_bytes(canonical_json(project) + b"\n")
        for asset_id in sorted(referenced):
            shutil.copy2(
                candidates / "assets/sha256" / asset_id.removeprefix(HASH_PREFIX),
                output_assets / asset_id.removeprefix(HASH_PREFIX),
            )
        identity = verify_mission_catalog(output, expected_scope="field")
        previous = share / ".local-mission-catalog"
        if previous.exists() or previous.is_symlink():
            raise ContractError("temporary mission catalog promotion path already exists")
        os.replace(active, previous)
        try:
            os.replace(output, active)
        except Exception:
            os.replace(previous, active)
            raise
        shutil.rmtree(previous)
        shutil.rmtree(variants)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if variants.exists() or variants.is_symlink():
        raise ContractError("drone install retained local mission catalog variants")
    observed = verify_mission_catalog(active, expected_scope="field")
    if observed != identity:
        raise ContractError("field mission catalog identity changed during installation")
    return observed
