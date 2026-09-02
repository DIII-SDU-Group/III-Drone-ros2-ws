from __future__ import annotations

import json
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.verification.backlog import BacklogError, parse_backlog
from iii_deployment.verification.evidence import (
    build_evidence,
    read_evidence,
    sign_evidence,
    validate_evidence,
)
from iii_deployment.verification.matrix import (
    audit_matrix,
    clause_baseline,
    junit_xml,
    load_clause_migrations,
    load_policy,
    materialize,
    read_matrix,
    verification_result,
    verify_clause_baseline,
)
from iii_deployment.signers import signer_id_for_public_key


ROOT = Path(__file__).resolve().parents[2]
BACKLOG = ROOT / "codex-backlogs" / "deployment-infrastructure-redesign.md"
BASELINE = ROOT / "deployment" / "verification" / "clause-baseline.json"
MIGRATIONS = ROOT / "deployment" / "verification" / "clause-migrations.json"
POLICY = ROOT / "deployment" / "verification" / "policy.json"
MATRIX = ROOT / "deployment" / "verification" / "matrix.json"
SCHEMAS = ROOT / "deployment" / "schemas" / "v1"


def test_all_decisions_and_tasks_are_parseable() -> None:
    backlog = parse_backlog(BACKLOG)
    assert {clause.decision for clause in backlog.clauses} == {
        f"Q{i}" for i in range(1, 133)
    }
    assert "P0.T0" in backlog.tasks
    assert "P5.T6" in backlog.tasks
    assert all(task.acceptance and task.tests for task in backlog.tasks.values())


def test_clause_baseline_is_current() -> None:
    backlog = parse_backlog(BACKLOG)
    assert verify_clause_baseline(backlog, BASELINE, MIGRATIONS) == []
    assert load_clause_migrations(MIGRATIONS)["migrations"] == []


def test_clause_change_requires_reviewed_baseline(tmp_path: Path) -> None:
    backlog = parse_backlog(BACKLOG)
    baseline = clause_baseline(backlog)
    baseline["clauses"][0]["text"] += " drift"
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    errors = verify_clause_baseline(backlog, path)
    assert errors and "Q1.c1" in errors[0]


def test_unknown_coverage_owner_is_rejected(tmp_path: Path) -> None:
    source = BACKLOG.read_text(encoding="utf-8").replace(
        "| Q1 | P0.T2, P3.T5, P5.T1 |", "| Q1 | P9.T9 |"
    )
    path = tmp_path / "backlog.md"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(BacklogError, match="unknown owner task P9.T9"):
        parse_backlog(path)


def test_duplicate_coverage_owner_is_rejected(tmp_path: Path) -> None:
    source = BACKLOG.read_text(encoding="utf-8").replace(
        "| Q1 | P0.T2, P3.T5, P5.T1 |",
        "| Q1 | P0.T2, P0.T2 |",
    )
    path = tmp_path / "backlog.md"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(BacklogError, match="duplicate coverage owner for Q1"):
        parse_backlog(path)


def test_committed_matrix_covers_every_clause_acceptance_and_q131_scenario() -> None:
    backlog = parse_backlog(BACKLOG)
    policy = load_policy(POLICY)
    matrix = read_matrix(MATRIX)
    assert audit_matrix(backlog, policy, matrix) == []
    assert matrix == materialize(backlog, policy)
    identifiers = {row["id"] for row in matrix["rows"]}
    assert {clause.id for clause in backlog.clauses} <= identifiers
    assert {
        f"{task.id}.a{index}"
        for task in backlog.tasks.values()
        for index, _ in enumerate(task.acceptance, start=1)
    } <= identifiers
    cutover = [row for row in matrix["rows"] if row["kind"] == "cutover-scenario"]
    assert {row["scenario"] for row in cutover} == {
        "factory",
        "release",
        "field",
        "failure",
        "configuration",
        "evidence",
        "offline",
        "documentation",
        "retirement",
    }
    assert all(row["acceptance_refs"] and row["test_refs"] for row in matrix["rows"])
    assert all(isinstance(row["owner_command"], list) for row in matrix["rows"])


def test_matrix_drift_reports_exact_row() -> None:
    backlog = parse_backlog(BACKLOG)
    policy = load_policy(POLICY)
    matrix = materialize(backlog, policy)
    matrix["rows"][0]["owner_command"].append("--drift")
    errors = audit_matrix(backlog, policy, matrix)
    assert any("Q1.c1: verification definition drift" in error for error in errors)


def _candidate(matrix, policy):
    body = {
        "workspace_commit": "a" * 40,
        "submodule_lock_sha256": "b" * 64,
        "release_id": "c" * 64,
        "release_version": "v1.2.3",
        "documentation_manifest_id": "d" * 64,
        "verification_policy_id": matrix["policy_id"],
    }
    return {**body, "candidate_set_id": content_identity(body)}


def _private_key(tmp_path: Path, authority: str = "workstation-field"):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "field.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    signer_id = signer_id_for_public_key(key.public_key())
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trusted = {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": [
            {
                "signer_id": signer_id,
                "algorithm": "Ed25519",
                "authority": authority,
                "public_key": __import__("base64").b64encode(public).decode("ascii"),
                "state": "active",
            }
        ],
    }
    return path, trusted


def test_signed_local_evidence_is_matrix_candidate_and_artifact_bound(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY)
    matrix = read_matrix(MATRIX)
    row = next(row for row in matrix["rows"] if row["level"] == "target-equivalent")
    artifact = tmp_path / "target.log"
    artifact.write_text("target passed\n", encoding="utf-8")
    record = build_evidence(
        matrix=matrix,
        policy=policy,
        level="target-equivalent",
        candidate_set=_candidate(matrix, policy),
        started_at="2026-08-27T10:00:00Z",
        finished_at="2026-08-27T10:01:00Z",
        environment={"machine": "aarch64"},
        impact_categories=["provisioned-drone-bench-smoke"],
        rows=[
            {
                "id": row["id"],
                "status": "pass",
                "reason": None,
                "evidence": [
                    {
                        "path": artifact.name,
                        "sha256": __import__("hashlib")
                        .sha256(artifact.read_bytes())
                        .hexdigest(),
                    }
                ],
            }
        ],
    )
    key, trusted = _private_key(tmp_path)
    record = sign_evidence(record, key)
    path = tmp_path / "evidence.json"
    path.write_bytes(canonical_json(record) + b"\n")
    loaded = read_evidence(
        path,
        matrix=matrix,
        policy=policy,
        registry=ContractRegistry(SCHEMAS),
        trusted_signers=trusted,
    )
    assert loaded["record_id"] == record["record_id"]
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ContractError, match="artifact hash mismatch"):
        read_evidence(
            path,
            matrix=matrix,
            policy=policy,
            registry=ContractRegistry(SCHEMAS),
            trusted_signers=trusted,
        )


def test_unsigned_physical_evidence_and_cross_level_rows_fail_closed() -> None:
    policy = load_policy(POLICY)
    matrix = read_matrix(MATRIX)
    physical = next(row for row in matrix["rows"] if row["level"] == "physical")
    host = next(row for row in matrix["rows"] if row["level"] == "host-independent")
    record = build_evidence(
        matrix=matrix,
        policy=policy,
        level="physical",
        candidate_set=_candidate(matrix, policy),
        started_at="2026-08-27T10:00:00Z",
        finished_at="2026-08-27T10:01:00Z",
        environment={},
        impact_categories=["field-flight"],
        rows=[{"id": physical["id"], "status": "pass", "reason": None, "evidence": []}],
    )
    with pytest.raises(ContractError, match="unsigned"):
        validate_evidence(record, matrix=matrix, policy=policy)
    with pytest.raises(ContractError, match="level differs"):
        build_evidence(
            matrix=matrix,
            policy=policy,
            level="physical",
            candidate_set=_candidate(matrix, policy),
            started_at="2026-08-27T10:00:00Z",
            finished_at="2026-08-27T10:01:00Z",
            environment={},
            impact_categories=["field-flight"],
            rows=[{"id": host["id"], "status": "pass", "reason": None, "evidence": []}],
        )


def test_host_evidence_requires_ci_qualified_signature_and_junit_artifact(
    tmp_path: Path,
) -> None:
    policy = load_policy(POLICY)
    matrix = read_matrix(MATRIX)
    host = next(row for row in matrix["rows"] if row["level"] == "host-independent")
    junit = tmp_path / "host.xml"
    junit.write_text("<testsuite tests='1' failures='0'/>\n", encoding="utf-8")
    record = build_evidence(
        matrix=matrix,
        policy=policy,
        level="host-independent",
        candidate_set=_candidate(matrix, policy),
        started_at="2026-08-27T10:00:00Z",
        finished_at="2026-08-27T10:01:00Z",
        environment={"github_run_id": "123"},
        impact_categories=["static-unit"],
        rows=[
            {
                "id": host["id"],
                "status": "pass",
                "reason": None,
                "evidence": [
                    {
                        "path": junit.name,
                        "sha256": __import__("hashlib")
                        .sha256(junit.read_bytes())
                        .hexdigest(),
                    }
                ],
            }
        ],
    )
    key, trusted = _private_key(tmp_path, "ci-qualified")
    record = sign_evidence(record, key)
    assert record["signature"]["authority"] == "ci-qualified"
    path = tmp_path / "host-evidence.json"
    path.write_bytes(canonical_json(record) + b"\n")
    loaded = read_evidence(
        path,
        matrix=matrix,
        policy=policy,
        registry=ContractRegistry(SCHEMAS),
        trusted_signers=trusted,
    )
    assert loaded["rows"][0]["id"] == host["id"]


def test_result_and_junit_keep_not_run_rows_explicit() -> None:
    matrix = read_matrix(MATRIX)
    result = verification_result(matrix)
    assert result["complete"] is False
    assert result["counts"]["not_run"] == len(matrix["rows"])
    xml = junit_xml(result)
    assert b'tests="1199"' in xml
    assert xml.count(b"<skipped") == len(matrix["rows"])
