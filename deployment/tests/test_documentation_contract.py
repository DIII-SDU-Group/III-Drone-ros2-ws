from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from iii_deployment.verification.documentation import (
    audit_manifest,
    generated_references,
    load_policy,
    materialize_manifest,
    materialize_review,
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
    assert all(
        len(row["sha256"]) == 64 and set(row["sha256"]) <= set("0123456789abcdef")
        for row in manifest["documents"]
    )


def test_agent_skill_sources_are_explicitly_excluded() -> None:
    manifest = read_manifest(MANIFEST)
    skill_rows = [
        row
        for row in manifest["documents"]
        if row["repository"] == "workspace"
        and row["path"].startswith(".agents/skills/")
    ]
    assert skill_rows
    assert all(
        row["classification"] == "excluded" and row["exclusion_reason"]
        for row in skill_rows
    )


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


def test_untracked_documents_never_enter_authoritative_manifest(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.md").write_text("# Tracked\n", encoding="utf-8")
    (tmp_path / "untracked.md").write_text("# Untracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    policy = {
        "schema": "iii.documentation-policy/v1",
        "repositories": [{"id": "fixture", "path": ".", "governed": True}],
        "exclusions": [],
        "canonical_roots": [],
        "forbidden_current_terms": [],
    }
    manifest = materialize_manifest(tmp_path, policy)
    assert [row["path"] for row in manifest["documents"]] == ["tracked.md"]


def test_manifest_identity_binds_exact_document_content(tmp_path: Path) -> None:
    policy, first = _fixture(tmp_path, "# First\n")
    first_document = first["documents"][0]
    (tmp_path / "README.md").write_text("# Second\n", encoding="utf-8")
    second = materialize_manifest(tmp_path, policy)
    second_document = second["documents"][0]
    assert second_document["sha256"] != first_document["sha256"]
    assert second["manifest_id"] != first["manifest_id"]


def _fixture_policy() -> dict:
    return {
        "schema": "iii.documentation-policy/v1",
        "repositories": [
            {
                "id": "workspace",
                "path": ".",
                "governed": True,
                "entrypoint": "README.md",
            }
        ],
        "exclusions": [],
        "canonical_roots": ["README.md"],
        "editable_repository_roots": [],
        "governed_lock": "deps/submodule-lock.txt",
        "router_links": [],
        "generated_references": [],
        "canonical_authorities": [{"id": "root", "path": "README.md"}],
        "authoring_contract": "README.md",
        "historical_index": "README.md",
        "migration_review": "documentation-review.json",
        "development_workspace_path_allowlist": [],
        "forbidden_current_terms": ["sshpass"],
    }


def _fixture(tmp_path: Path, readme: str):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    (tmp_path / "deps").mkdir()
    (tmp_path / "deps/submodule-lock.txt").write_text("# none\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    policy = _fixture_policy()
    manifest = materialize_manifest(tmp_path, policy)
    (tmp_path / "documentation-review.json").write_text(
        json.dumps(materialize_review(manifest, reviewer="fixture")),
        encoding="utf-8",
    )
    return policy, manifest


def test_docs_audit_rejects_broken_anchor_unknown_command_option_and_forbidden_term(
    tmp_path: Path,
) -> None:
    policy, manifest = _fixture(
        tmp_path,
        """# Fixture

[broken](README.md#missing)

```bash
iii imaginary mutate
iii docs check --imaginary-option
sshpass secret
```
""",
    )
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("broken local anchor" in error for error in errors)
    assert any("unknown III command" in error for error in errors)
    assert any("unsupported option" in error for error in errors)
    assert any("forbidden current term 'sshpass'" in error for error in errors)


def test_policy_rejects_duplicate_authority_and_missing_editable_repository(
    tmp_path: Path,
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["canonical_authorities"].append(dict(policy["canonical_authorities"][0]))
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(Exception, match="authorities must be unique"):
        load_policy(path)
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["editable_repository_roots"].pop()
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(Exception, match="must exactly match"):
        load_policy(path)


def test_policy_rejects_duplicate_router_and_invalid_forbidden_pattern(
    tmp_path: Path,
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["router_links"].append(dict(policy["router_links"][0]))
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(Exception, match="router links must be unique"):
        load_policy(path)

    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["forbidden_current_patterns"] = ["["]
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(Exception, match="invalid forbidden documentation pattern"):
        load_policy(path)


def test_docs_audit_rejects_router_cycle(tmp_path: Path) -> None:
    policy, manifest = _fixture(tmp_path, "# Fixture\n\n[child](child.md)\n")
    (tmp_path / "child.md").write_text(
        "# Child\n\n[parent](README.md)\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "child.md"], cwd=tmp_path, check=True)
    policy["router_links"] = [
        {"source": "README.md", "target": "child.md"},
        {"source": "child.md", "target": "README.md"},
    ]
    manifest = materialize_manifest(tmp_path, policy)
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("router cycle" in error for error in errors)


def test_generated_command_and_schema_references_are_deterministic() -> None:
    policy = load_policy(POLICY)
    first = generated_references(ROOT, policy)
    second = generated_references(ROOT, policy)
    assert first == second
    assert all(
        path.read_text(encoding="utf-8") == content for path, content in first.items()
    )
    command = first[ROOT / "docs/generated/iii-command-reference.md"]
    schemas = first[ROOT / "docs/generated/deployment-schema-reference.md"]
    assert "## `iii docs check`" in command
    assert "`documentation-check.schema.json`" in schemas


def test_docs_audit_rejects_missing_stale_and_unapproved_migration_review(
    tmp_path: Path,
) -> None:
    policy, manifest = _fixture(tmp_path, "# Fixture\n")
    path = tmp_path / "documentation-review.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    review["documents"].pop()
    review["review_id"] = "0" * 64
    path.write_text(json.dumps(review), encoding="utf-8")
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("identity mismatch" in error for error in errors)

    review = materialize_review(manifest, reviewer="fixture")
    review["documents"][0]["status"] = "pending"
    body = {key: value for key, value in review.items() if key != "review_id"}
    from iii_deployment.contracts import content_identity

    review["review_id"] = content_identity(body)
    path.write_text(json.dumps(review), encoding="utf-8")
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("did not pass" in error for error in errors)


def test_docs_audit_rejects_cross_repository_relative_link(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    policy, manifest = _fixture(tmp_path, "# Fixture\n\n[outside](../outside.md)\n")
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("escapes standalone repository" in error for error in errors)


def test_docs_audit_requires_explicit_development_path_scope(tmp_path: Path) -> None:
    policy, manifest = _fixture(
        tmp_path, "# Fixture\n\nDevelopment path: `/home/iii/ws`.\n"
    )
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any(
        "development workspace path is not explicitly allowed" in error
        for error in errors
    )


def test_docs_audit_requires_every_historical_document_in_index(
    tmp_path: Path,
) -> None:
    policy, _manifest = _fixture(tmp_path, "# Fixture\n")
    (tmp_path / "old-plan.md").write_text("# Old plan\n", encoding="utf-8")
    subprocess.run(["git", "add", "old-plan.md"], cwd=tmp_path, check=True)
    manifest = materialize_manifest(tmp_path, policy)
    (tmp_path / "documentation-review.json").write_text(
        json.dumps(materialize_review(manifest, reviewer="fixture")),
        encoding="utf-8",
    )
    errors = audit_manifest(tmp_path, policy, manifest)
    assert any("historical documentation is not indexed" in error for error in errors)
