from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    check_target_compatibility,
    classify_release,
    content_identity,
    validate_status_transition,
)
from iii_deployment.policy import load_operational_policy, merge_stricter_policy, policy_reference


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
FIXTURE = ROOT / "deployment" / "tests" / "fixtures" / "release_manifest.json"


@pytest.fixture
def manifest() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _preflight(manifest: dict) -> dict:
    return {
        "schema": "iii.qualification-preflight-result/v1",
        "mode": "build",
        "version": manifest["version"],
        "source_commit": manifest["source"]["workspace_commit"],
        "release_commit": manifest["source"]["workspace_commit"],
        "verified": True,
        "checks": [{"id": "test", "passed": True, "detail": "fixture"}],
    }


def test_clean_qualified_manifest(manifest: dict) -> None:
    REGISTRY.validate("release-manifest", manifest)
    assert classify_release(manifest, requested="qualified", preflight=_preflight(manifest)) == "qualified"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["source"].update(branch="develop"), "branch"),
        (lambda value: value["source"].update(clean=False), "clean"),
        (lambda value: value["dependency_lock"].update(verified=False), "lock"),
        (lambda value: value["qualification"].update(tests_complete=False), "tests"),
        (lambda value: value["signing"].update(authority="workstation-field"), "signer"),
    ],
)
def test_invalid_qualified_claim_fails_closed(manifest: dict, mutation, reason: str) -> None:
    mutation(manifest)
    with pytest.raises(ContractError, match=reason):
        classify_release(manifest, requested="qualified", preflight=_preflight(manifest))
    assert classify_release(manifest, requested="field-development") == "field-development"


def test_qualified_classification_requires_independent_bound_build_preflight(manifest: dict) -> None:
    with pytest.raises(ContractError, match="preflight is absent"):
        classify_release(manifest, requested="qualified")
    preflight = _preflight(manifest)
    preflight["source_commit"] = "0" * 40
    with pytest.raises(ContractError, match="commit differs"):
        classify_release(manifest, requested="qualified", preflight=preflight)


def test_modified_submodule_and_untracked_content_identify_field_state(manifest: dict) -> None:
    clean = content_identity(manifest["source"])
    manifest["source"]["submodules"][0]["state"] = "modified"
    modified = content_identity(manifest["source"])
    manifest["source"]["untracked"].append({"path": "src/new.cpp", "sha256": "f" * 64})
    untracked = content_identity(manifest["source"])
    assert len({clean, modified, untracked}) == 3


def test_unknown_schema_version_and_absolute_path_are_rejected(manifest: dict) -> None:
    manifest["schema_version"] = "99"
    with pytest.raises(ContractError, match="schema_version"):
        REGISTRY.validate("release-manifest", manifest)
    manifest["schema_version"] = "1"
    manifest["checksums"] = {"/tmp/escape": "a" * 64}
    with pytest.raises(ContractError, match="checksums"):
        REGISTRY.validate("release-manifest", manifest)


def test_target_incompatibility_is_detected_pre_transfer(manifest: dict) -> None:
    check_target_compatibility(manifest, manifest["target"])
    incompatible = dict(manifest["target"], architecture="x86_64")
    with pytest.raises(ContractError, match="architecture"):
        check_target_compatibility(manifest, incompatible)


def test_operational_policy_is_valid_hashed_and_cannot_be_weakened() -> None:
    policy = load_operational_policy(ROOT / "deployment" / "operational-policy.json", REGISTRY)
    assert policy_reference(policy)["sha256"] == content_identity(policy)
    stricter = merge_stricter_policy(policy, {"safety": {"telemetry_max_age_ms": 500}}, REGISTRY)
    assert stricter["safety"]["telemetry_max_age_ms"] == 500
    with pytest.raises(ContractError):
        merge_stricter_policy(policy, {"safety": {"telemetry_max_age_ms": 2000}}, REGISTRY)


def _status(
    sequence: int,
    status: str,
    previous_global: dict | None,
    previous_release: dict | None = None,
) -> dict:
    return {
        "schema_version": "1", "statement_id": f"{sequence:064x}", "sequence": sequence,
        "operation_id": f"release-status-test-{sequence}",
        "release_id": "a" * 64, "version": "v1.2.3", "status": status, "reason": "test",
        "superseding_version": None, "recorded_at": "2026-08-25T12:00:00Z",
        "signer_id": "b" * 64, "signature_algorithm": "Ed25519",
        "previous_statement": None if previous_global is None else {"statement_id": previous_global["statement_id"], "sha256": content_identity(previous_global)},
        "previous_release_statement": None if previous_release is None else {"statement_id": previous_release["statement_id"], "sha256": content_identity(previous_release)},
        "signature": "A" * 86 + "==",
    }


def test_release_status_is_append_only_and_monotonic() -> None:
    first = _status(1, "qualified", None)
    second = _status(2, "withdrawn", first, first)
    REGISTRY.validate("release-status", first)
    REGISTRY.validate("release-status", second)
    validate_status_transition(None, first)
    validate_status_transition(first, second, previous_global=first)
    with pytest.raises(ContractError, match="non-monotonic"):
        validate_status_transition(
            second,
            _status(3, "qualified", second, second),
            previous_global=second,
        )


def test_record_contract_correlates_capture_and_release_without_absolute_paths() -> None:
    record = {
        "schema_version": "1", "record_type": "capture", "record_id": "c" * 64,
        "created_at": "2026-08-25T12:00:00Z", "source": "runtime-api", "target": "iii.local",
        "profile": "real", "release_id": "a" * 64,
        "content": [{"path": "payload/values.json", "sha256": "d" * 64, "size": 42}],
        "references": [{"domain": "operation", "record_id": "e" * 64}],
        "integrity": {"algorithm": "sha256", "manifest_sha256": "f" * 64, "state": "verified"},
    }
    REGISTRY.validate("record", record)
    record["content"][0]["path"] = "/home/operator/private"
    with pytest.raises(ContractError):
        REGISTRY.validate("record", record)
