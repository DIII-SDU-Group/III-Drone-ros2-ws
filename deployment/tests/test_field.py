from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.field import (
    acknowledge_warnings,
    evaluate_readiness,
    field_cache_report,
    sign_readiness,
)
from iii_deployment.signers import generate_signer
from iii_deployment.contracts import ContractRegistry


REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


def observations(**changes):
    value = {
        "boot_id": "boot-a",
        "drone_release_id": "a" * 64,
        "gc_release_id": "b" * 64,
        "profile": "real",
        "configuration_hash": "c" * 64,
        "commissioning_hash": "d" * 64,
        "px4_required_state_hash": "e" * 64,
        "mission_id": "inspection",
        "qgc_pair_id": "qgc-1",
        "commissioning_valid": True,
        "release_pair_compatible": True,
        "clock_gate_valid": True,
        "receiver_available": True,
        "control_plane_available": True,
        "required_hardware_ready": True,
        "px4_firmware_matches": True,
        "px4_required_parameters_match": True,
        "parameter_reconciliation_complete": True,
        "selected_mission_valid": True,
        "storage_reserve_valid": True,
        "credentials_valid": True,
        "runtime_healthy": True,
        "cold_restart_clear": True,
        "qgc_managed_settings_match": True,
        "optional_hardware_ready": True,
        "backup_fresh": True,
        "external_archive_recent": True,
        "offline_cache_fresh": True,
        "logging_capacity_ready": True,
    }
    value.update(changes)
    return value


def target():
    return {"endpoint": "iii.local", "logical_id": "drone", "profile": "real"}


def signer(tmp_path: Path) -> tuple[Path, dict]:
    private = tmp_path / "field.pem"
    descriptor = generate_signer(
        private,
        tmp_path / "field-public.json",
        authority="workstation-field",
        registry=REGISTRY,
    )
    return private, {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": [
            {
                key: descriptor[key]
                for key in ("signer_id", "algorithm", "authority", "public_key")
            }
            | {"state": "active"}
        ],
    }


def test_readiness_pass_warn_fail_are_deterministic() -> None:
    passed = evaluate_readiness(observations(), target=target(), policy_hash="f" * 64)
    warned = evaluate_readiness(
        observations(backup_fresh=False), target=target(), policy_hash="f" * 64
    )
    failed = evaluate_readiness(
        observations(clock_gate_valid=False), target=target(), policy_hash="f" * 64
    )
    assert passed["overall"] == "PASS"
    assert warned["overall"] == "WARN"
    assert warned["findings"][0]["id"] == "FIELD.BACKUP.STALE"
    assert failed["overall"] == "FAIL"
    assert failed["findings"][0]["id"] == "FIELD.CLOCK_GATE.INVALID"
    assert failed["authorization"] is False


def test_live_identity_change_invalidates_sealed_record_identity() -> None:
    first = evaluate_readiness(
        observations(boot_id="boot-a"),
        target=target(),
        policy_hash="f" * 64,
        recorded_at="2026-01-01T00:00:00Z",
    )
    second = evaluate_readiness(
        observations(boot_id="boot-b"),
        target=target(),
        policy_hash="f" * 64,
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert first["record_id"] != second["record_id"]


def test_signed_warning_acknowledgement_does_not_change_severity(
    tmp_path: Path,
) -> None:
    record = evaluate_readiness(
        observations(backup_fresh=False), target=target(), policy_hash="f" * 64
    )
    private, trusted = signer(tmp_path)
    signed = sign_readiness(record, private, trusted)
    acknowledgement = acknowledge_warnings(
        signed,
        ["FIELD.BACKUP.STALE"],
        "Known test-window exception",
        private,
        trusted,
    )
    assert signed["findings"][0]["severity"] == "WARN"
    assert signed["evidence_class"] == "release-commissioning-evidence"
    assert acknowledgement["severity_changed"] is False
    assert acknowledgement["authorization"] is False
    assert acknowledgement["signature"]["algorithm"] == "Ed25519"


def test_failure_cannot_be_acknowledged(tmp_path: Path) -> None:
    record = evaluate_readiness(
        observations(clock_gate_valid=False), target=target(), policy_hash="f" * 64
    )
    with pytest.raises(ContractError, match="only present warning"):
        acknowledge_warnings(
            record,
            ["FIELD.CLOCK_GATE.INVALID"],
            "No",
            *signer(tmp_path),
        )


def test_tampered_readiness_record_cannot_be_acknowledged(tmp_path: Path) -> None:
    private, trusted = signer(tmp_path)
    record = sign_readiness(
        evaluate_readiness(
            observations(backup_fresh=False),
            target=target(),
            policy_hash="f" * 64,
        ),
        private,
        trusted,
    )
    record["findings"][0]["message"] = "tampered"

    with pytest.raises(ContractError, match="content identity mismatch"):
        acknowledge_warnings(
            record,
            ["FIELD.BACKUP.STALE"],
            "Known test-window exception",
            private,
            trusted,
        )


def test_untrusted_field_key_cannot_create_readiness_evidence(tmp_path: Path) -> None:
    private, _trusted = signer(tmp_path)
    record = evaluate_readiness(observations(), target=target(), policy_hash="f" * 64)

    with pytest.raises(ContractError, match="not an active authorized"):
        sign_readiness(
            record,
            private,
            {
                "schema_version": "1",
                "store_type": "iii.trusted-signers",
                "signers": [],
            },
        )


def test_field_cache_requires_verified_qualified_pair() -> None:
    complete = field_cache_report(
        [
            {
                "version": "v1.2.3",
                "verified": True,
                "status": "qualified",
                "components": ["drone", "gc"],
            }
        ]
    )
    withdrawn = field_cache_report(
        [
            {
                "version": "v1.2.3",
                "verified": True,
                "status": "withdrawn",
                "components": ["drone", "gc"],
            }
        ]
    )
    assert complete["complete"] is True
    assert withdrawn["complete"] is False
