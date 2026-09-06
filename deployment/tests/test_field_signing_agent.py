from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json
from iii_deployment.field_signing_agent import (
    SIGNING_DOMAIN,
    FieldSigningAgent,
    generate_field_signer,
)
from iii_deployment.signers import load_private_key, signer_id_for_public_key


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"
REGISTRY = ContractRegistry(SCHEMAS)


def _readiness() -> dict:
    return {
        "schema": "iii.field-readiness/v1",
        "record_id": "1" * 64,
        "recorded_at": "2026-08-26T12:00:00Z",
        "target": {
            "endpoint": "iii.local",
            "logical_id": "drone",
            "profile": "real",
        },
        "identity": {
            "boot_id": "boot-a",
            "drone_release_id": "2" * 64,
            "gc_release_id": "3" * 64,
            "profile": "real",
            "configuration_hash": "4" * 64,
            "commissioning_hash": "5" * 64,
            "px4_required_state_hash": "6" * 64,
            "mission_id": "mission-a",
            "qgc_pair_id": "7" * 64,
        },
        "policy_hash": "8" * 64,
        "overall": "PASS",
        "findings": [],
        "observations": {},
        "evidence_class": "diagnostic",
        "authorization": False,
        "signature": None,
    }


def test_field_signer_is_encrypted_ttl_bounded_and_digest_only(tmp_path: Path) -> None:
    private = tmp_path / "credentials/field.pem"
    public = tmp_path / "credentials/field-public.json"
    passphrase = b"correct horse battery staple"
    descriptor = generate_field_signer(
        private_key_path=private,
        public_descriptor_path=public,
        passphrase=passphrase,
        registry=REGISTRY,
    )
    assert b"ENCRYPTED PRIVATE KEY" in private.read_bytes()
    assert private.stat().st_mode & 0o077 == 0
    now = [100.0]
    agent = FieldSigningAgent(
        private_key_path=private,
        registry=REGISTRY,
        monotonic=lambda: now[0],
    )
    with pytest.raises(ContractError, match="unlock failed"):
        agent.unlock(b"incorrect passphrase")
    assert agent.unlock(passphrase)["signer_id"] == descriptor["signer_id"]
    signature = agent.sign_document(contract="field-readiness", document=_readiness())
    assert signature["signer_id"] == descriptor["signer_id"]
    assert signature["digest_sha256"]
    expected_digest = hashlib.sha256(canonical_json(_readiness())).hexdigest()
    Ed25519PublicKey.from_public_bytes(
        base64.b64decode(descriptor["public_key"])
    ).verify(
        base64.b64decode(signature["signature"]),
        SIGNING_DOMAIN + b"field-readiness\0" + bytes.fromhex(expected_digest),
    )
    with pytest.raises(ContractError, match="arbitrary"):
        agent.sign_document(contract="raw-builder-input", document={})
    with pytest.raises(ContractError, match="contract rejected"):
        agent.sign_document(contract="field-readiness", document={})
    now[0] += 8 * 3600
    with pytest.raises(ContractError, match="locked or expired"):
        agent.sign_document(contract="field-readiness", document=_readiness())


def test_field_signing_agent_refuses_ttl_above_24_hours(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="at most 24"):
        FieldSigningAgent(
            private_key_path=tmp_path / "unused.pem",
            registry=REGISTRY,
            ttl_hours=24.1,
        )


def test_encrypted_field_key_uses_only_named_keyring_account(tmp_path: Path, monkeypatch) -> None:
    private = tmp_path / "credentials/field.pem"
    public = tmp_path / "credentials/field-public.json"
    descriptor = generate_field_signer(
        private_key_path=private,
        public_descriptor_path=public,
        passphrase=b"correct horse battery staple",
        registry=REGISTRY,
    )
    monkeypatch.setenv("III_FIELD_SIGNING_KEYRING_ACCOUNT", "field-test")
    monkeypatch.setattr(
        "iii_deployment.field_signing_agent.passphrase_from_keyring",
        lambda account: b"correct horse battery staple" if account == "field-test" else b"",
    )
    assert signer_id_for_public_key(load_private_key(private).public_key()) == descriptor["signer_id"]
