"""Detailed source-to-field impact graph for missions, trees, and parameters."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

import yaml

from .contracts import ContractError, content_identity


def _git_show(repo: Path, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{path}"],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _read(path: Path) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    return path.read_bytes()


def _yaml(raw: bytes | None) -> Any:
    if raw is None:
        return None
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ContractError(f"field impact YAML is malformed: {exc}") from exc


def _registrations(raw: bytes | None) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    text = raw.decode("utf-8")
    values: dict[str, dict[str, Any]] = {}
    for body in re.findall(r"iii_register_mission\s*\((.*?)\)", text, re.DOTALL):
        tokens = re.findall(r"[^\s#]+", body)
        fields: dict[str, list[str]] = {}
        key = None
        for token in tokens:
            if token in {
                "ID",
                "SPECIFICATION",
                "CLASSIFICATION",
                "STATUS",
                "PROFILES",
                "DEFAULT_FOR",
            }:
                key = token
                fields[key] = []
            elif key is not None:
                fields[key].append(token)
        if not fields.get("ID") or not fields.get("SPECIFICATION"):
            raise ContractError("mission registration lacks ID or specification")
        mission_id = fields["ID"][0]
        values[mission_id] = {
            "id": mission_id,
            "specification": fields["SPECIFICATION"][0],
            "classification": (fields.get("CLASSIFICATION") or ["unknown"])[0],
            "status": (fields.get("STATUS") or ["active"])[0],
            "profiles": sorted(fields.get("PROFILES", [])),
            "default_for": sorted(fields.get("DEFAULT_FOR", [])),
        }
    return values


def _tree_references(document: Any) -> list[str]:
    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "behavior_tree_xml_file" and isinstance(item, str):
                    values.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(document)
    return sorted(values)


def _mission_state(repo: Path, *, head: bool) -> dict[str, dict[str, Any]]:
    cmake = (
        _git_show(repo, "CMakeLists.txt") if head else _read(repo / "CMakeLists.txt")
    )
    registrations = _registrations(cmake)
    for item in registrations.values():
        raw = (
            _git_show(repo, item["specification"])
            if head
            else _read(repo / item["specification"])
        )
        item["specification_sha256"] = (
            hashlib.sha256(raw).hexdigest() if raw is not None else None
        )
        item["behavior_trees"] = _tree_references(_yaml(raw))
    return registrations


def mission_registry(workspace: Path) -> dict[str, dict[str, Any]]:
    """Return the current source mission registry for deployment selection."""
    return _mission_state(workspace / "src/III-Drone-Mission", head=False)


def mission_impact(workspace: Path, changed_paths: Sequence[str]) -> dict[str, Any]:
    repo = workspace / "src/III-Drone-Mission"
    before = _mission_state(repo, head=True)
    after = _mission_state(repo, head=False)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        mission_id
        for mission_id in set(before) & set(after)
        if before[mission_id] != after[mission_id]
    )
    tree_paths = sorted(
        path.split("src/III-Drone-Mission/", 1)[1]
        for path in changed_paths
        if "src/III-Drone-Mission/behavior_trees/" in path
    )
    tree_changes = []
    for tree in tree_paths:
        impacted = sorted(
            mission_id
            for mission_id in set(before) | set(after)
            if tree
            in set(before.get(mission_id, {}).get("behavior_trees", ()))
            | set(after.get(mission_id, {}).get("behavior_trees", ()))
            or tree.endswith("models.xml")
        )
        tree_changes.append({"path": tree, "impacted_mission_ids": impacted})
    entries = []
    for state, identifiers in (
        ("added", added),
        ("changed", changed),
        ("removed", removed),
    ):
        for mission_id in identifiers:
            metadata = (after if state != "removed" else before)[mission_id]
            entries.append(
                {
                    "id": mission_id,
                    "state": state,
                    "classification": metadata["classification"],
                    "profiles": metadata["profiles"],
                    "behavior_trees": metadata["behavior_trees"],
                }
            )
    return {
        "entries": entries,
        "behavior_trees": tree_changes,
        "catalog_identity": content_identity(after),
    }


def _flatten_manifest(value: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    if "type" in value and "value" in value:
        return {prefix or "/": dict(value)}
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        path = f"{prefix}/{key}" if prefix else f"/{key}"
        result.update(_flatten_manifest(item, path))
    return result


def parameter_impact(workspace: Path, changed_paths: Sequence[str]) -> dict[str, Any]:
    repo = workspace / "src/III-Drone-Configuration"
    manifest = "config/parameters/parameter_manifest.yaml"
    before = _flatten_manifest(_yaml(_git_show(repo, manifest)))
    after = _flatten_manifest(_yaml(_read(repo / manifest)))
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        key for key in set(before) & set(after) if before[key] != after[key]
    )
    defaults_changed = sorted(
        key for key in changed if before[key].get("value") != after[key].get("value")
    )
    sets = sorted(
        path.split("src/III-Drone-Configuration/", 1)[1]
        for path in changed_paths
        if "src/III-Drone-Configuration/config/parameter_sets/" in path
    )
    return {
        "manifest": {
            "added": added,
            "changed": changed,
            "removed": removed,
            "reintroduced": [],
            "reintroduction_candidates": added,
            "reintroduction_determination": "requires-target-legacy-shadow-review",
            "defaults_changed": defaults_changed,
        },
        "reconciliation_actions": [
            {"key": key, "action": "add-or-block-reintroduction"} for key in added
        ]
        + [
            {"key": key, "action": "preserve-live-value-and-review-change"}
            for key in changed
        ]
        + [{"key": key, "action": "retire-to-legacy-shadow"} for key in removed],
        "parameter_sets": [
            {
                "path": path,
                "action": "review-and-reconcile",
                "preserve_unknown_live_values": True,
            }
            for path in sets
        ],
        "configuration_identity": content_identity(after),
    }


def detailed_field_impact(
    workspace: Path, changed_paths: Sequence[str]
) -> dict[str, Any]:
    value = {
        "missions": mission_impact(workspace, changed_paths),
        "parameters": parameter_impact(workspace, changed_paths),
    }
    value["detail_id"] = content_identity(value)
    return value
