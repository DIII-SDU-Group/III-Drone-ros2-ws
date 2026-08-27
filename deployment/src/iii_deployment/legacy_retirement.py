"""Fail-closed audit for removed deployment entry points and archive gating."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .contracts import ContractRegistry


POLICY_SCHEMA = "iii.legacy-retirement-policy/v1"


class LegacyRetirementError(ValueError):
    pass


def load_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyRetirementError(f"invalid legacy retirement policy: {exc}") from exc
    if value.get("schema") != POLICY_SCHEMA:
        raise LegacyRetirementError("legacy retirement policy schema is unsupported")
    for rule in value.get("forbidden_active_patterns", []):
        try:
            re.compile(rule["pattern"])
        except (KeyError, re.error) as exc:
            raise LegacyRetirementError("invalid legacy retirement pattern") from exc
    return value


def _tracked_files(root: Path, scan_roots: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--", *scan_roots],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise LegacyRetirementError(
            f"cannot inventory active deployment paths: {result.stderr.strip()}"
        )
    return sorted({line for line in result.stdout.splitlines() if line})


def validate_archive_metadata(root: Path, policy: Mapping[str, Any]) -> list[str]:
    path = root / policy["archive_metadata"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        ContractRegistry(root / "deployment/schemas/v1").validate(
            "legacy-archive-metadata", value
        )
    except Exception as exc:
        return [f"legacy archive metadata is invalid: {exc}"]
    if value["state"] == "pending-q131" and any(
        value[field] is not None
        for field in (
            "replacement_release_id",
            "replacement_documentation_manifest_id",
            "q131_retirement_evidence_id",
        )
    ):
        return ["pending legacy archive metadata must not imply completed evidence"]
    return []


def audit(root: Path, policy: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for relative in policy.get("required_absent", []):
        if (root / relative).exists() or (root / relative).is_symlink():
            errors.append(f"retired active path still exists: {relative}")

    exclusions = policy.get("excluded_patterns", [])
    for relative in _tracked_files(root, policy["scan_roots"]):
        path = root / relative
        if not path.is_file() or any(
            fnmatch.fnmatch(relative, item) for item in exclusions
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for rule in policy.get("forbidden_active_patterns", []):
            if re.search(rule["pattern"], content):
                errors.append(f"{relative}: retired pattern {rule['id']}")

    for entrypoint in policy.get("retired_entrypoints", []):
        path = root / entrypoint["path"]
        if not path.is_file() or path.is_symlink():
            errors.append(
                f"retired compatibility entry point is missing: {entrypoint['path']}"
            )
            continue
        content = path.read_text(encoding="utf-8")
        for marker in entrypoint["required_markers"]:
            if marker not in content:
                errors.append(
                    f"{entrypoint['path']}: missing retirement marker {marker}"
                )
    errors.extend(validate_archive_metadata(root, policy))
    return errors
