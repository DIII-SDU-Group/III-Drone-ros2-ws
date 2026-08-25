from __future__ import annotations

import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json, content_identity
from iii_deployment.governance import (
    governed_source_identity, load_json, required_evidence, validate_attestation_binding,
    validate_mechanical_diff, validate_pr_source, validate_waivers, verify_attestation,
)


ROOT = Path(__file__).resolve().parents[2]
BRANCH = load_json(ROOT / "deployment/governance/branch-policy.json", "iii.branch-policy/v1")
IMPACT = load_json(ROOT / "deployment/governance/change-impact-policy.json", "iii.change-impact-policy/v1")
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")


@pytest.mark.parametrize("base,head", [("develop", "deployment-redesign"), ("main", "promote/develop-to-main/2026-08"), ("release", "main")])
def test_allowed_workspace_sources(base: str, head: str) -> None:
    validate_pr_source(BRANCH, repository_kind="workspace", base=base, head=head)


@pytest.mark.parametrize("base,head", [("main", "feature"), ("release", "promote/develop-to-main/x"), ("develop", "main"), ("staging", "feature")])
def test_rejected_workspace_sources(base: str, head: str) -> None:
    with pytest.raises(ContractError):
        validate_pr_source(BRANCH, repository_kind="workspace", base=base, head=head)


def test_submodules_have_no_release_branch() -> None:
    with pytest.raises(ContractError):
        validate_pr_source(BRANCH, repository_kind="submodule", base="release", head="main")


def test_mechanical_diff_accepts_only_lock_and_gitlinks() -> None:
    validate_mechanical_diff(BRANCH, ["deps/submodule-lock.txt", "src/III-Drone-Core"])
    with pytest.raises(ContractError, match="non-gitlink"):
        validate_mechanical_diff(BRANCH, ["README.md"])


def test_impact_policy_explains_flight_and_docs_categories() -> None:
    reasons = required_evidence(IMPACT, ["src/III-Drone-Mission/CMakeLists.txt", "docs/README.md"])
    assert "field-flight" in reasons
    assert any("FLIGHT_CRITICAL" in reason for reason in reasons["field-flight"])
    assert "static-unit" in reasons


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _signed_attestation() -> tuple[dict, dict[str, str]]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signer_id = content_identity({"algorithm": "Ed25519", "public_key": _b64(public)})
    value = {
        "schema_version": "1", "workspace_commit": "a" * 40,
        "source_content_identity": "b" * 64, "dependency_lock_sha256": "c" * 64,
        "policy_sha256": "d" * 64,
        "categories": [{"id": "static-unit", "status": "passed", "summary_sha256": "e" * 64}],
        "waivers": [], "artifacts": [], "signer_id": signer_id,
    }
    value["attestation_id"] = content_identity(value)
    value["signature"] = _b64(key.sign(canonical_json(value)))
    return value, {signer_id: _b64(public)}


def test_signed_attestation_verification_and_tamper_rejection() -> None:
    value, signers = _signed_attestation()
    verify_attestation(value, registry=REGISTRY, trusted_signers=signers)
    tampered = deepcopy(value)
    tampered["workspace_commit"] = "f" * 40
    with pytest.raises(ContractError, match="identity"):
        verify_attestation(tampered, registry=REGISTRY, trusted_signers=signers)


def test_attestation_binding_requires_current_content_and_policy() -> None:
    value, _signers = _signed_attestation()
    value["source_content_identity"] = governed_source_identity(ROOT)
    value["policy_sha256"] = content_identity(IMPACT)
    validate_attestation_binding(
        value, source_identity=governed_source_identity(ROOT), impact_policy=IMPACT
    )
    value["source_content_identity"] = "0" * 64
    with pytest.raises(ContractError, match="source-content"):
        validate_attestation_binding(
            value, source_identity=governed_source_identity(ROOT), impact_policy=IMPACT
        )


def test_waiver_only_allows_not_performed_physical_category() -> None:
    required = ["static-unit", "field-flight"]
    categories = {"static-unit": "passed", "field-flight": "not-performed"}
    waiver = [{"category": "field-flight", "rationale": "weather", "risk": "untested flight", "compensating_evidence": ["bench"]}]
    validate_waivers(IMPACT, required, categories, waiver)
    with pytest.raises(ContractError, match="non-waivable"):
        validate_waivers(IMPACT, ["static-unit"], {"static-unit": "not-performed"}, [{"category": "static-unit", "rationale": "x", "risk": "y", "compensating_evidence": ["z"]}])
