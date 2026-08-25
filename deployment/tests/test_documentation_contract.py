from __future__ import annotations

import json
from pathlib import Path

from iii_deployment.verification.documentation import (
    audit_manifest,
    load_policy,
    materialize_manifest,
    read_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deployment" / "documentation-policy.json"
MANIFEST = ROOT / "deployment" / "documentation-manifest.json"


def test_documentation_manifest_covers_governed_repositories() -> None:
    policy = load_policy(POLICY)
    manifest = read_manifest(MANIFEST)
    assert manifest == materialize_manifest(ROOT, policy)
    repositories = {row["repository"] for row in manifest["documents"]}
    assert repositories == {entry["id"] for entry in policy["repositories"]}


def test_agent_skill_sources_are_explicitly_excluded() -> None:
    manifest = read_manifest(MANIFEST)
    skill_rows = [
        row for row in manifest["documents"]
        if row["repository"] == "workspace" and row["path"].startswith(".agents/skills/")
    ]
    assert skill_rows
    assert all(row["classification"] == "excluded" and row["exclusion_reason"] for row in skill_rows)


def test_clean_documentation_audit_has_no_drift() -> None:
    policy = load_policy(POLICY)
    manifest = read_manifest(MANIFEST)
    errors = audit_manifest(ROOT, policy, manifest)
    assert errors == [], "\n".join(errors)


def test_manifest_detects_missing_document() -> None:
    policy = load_policy(POLICY)
    manifest = materialize_manifest(ROOT, policy)
    manifest["documents"].pop()
    errors = audit_manifest(ROOT, policy, manifest)
    assert any("missing" in error for error in errors)

