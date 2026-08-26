"""Deterministic field preparation and connected-system readiness records."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidSignature

from .contracts import ContractError, canonical_json, content_identity
from .signers import load_private_key, signer_id_for_public_key


READINESS_SCHEMA = "iii.field-readiness/v1"
ACK_SCHEMA = "iii.field-warning-acknowledgement/v1"
READINESS_DOMAIN = b"iii.field-readiness/v1\0"
ACK_DOMAIN = b"iii.field-warning-acknowledgement/v1\0"


_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "FIELD.COMMISSIONING.INVALID",
        "commissioning_valid",
        "FAIL",
        "Commissioning or the allowed field overlay is invalid.",
    ),
    (
        "FIELD.RELEASE_PAIR.INCOMPATIBLE",
        "release_pair_compatible",
        "FAIL",
        "GC and drone releases are incompatible.",
    ),
    (
        "FIELD.CLOCK_GATE.INVALID",
        "clock_gate_valid",
        "FAIL",
        "The aircraft clock gate is not operational.",
    ),
    (
        "FIELD.RECEIVER.UNAVAILABLE",
        "receiver_available",
        "FAIL",
        "The authenticated receiver is unavailable.",
    ),
    (
        "FIELD.CONTROL_PLANE.UNAVAILABLE",
        "control_plane_available",
        "FAIL",
        "The minimal runtime control plane is unavailable.",
    ),
    (
        "FIELD.HARDWARE.REQUIRED_MISSING",
        "required_hardware_ready",
        "FAIL",
        "Required hardware roles are unavailable.",
    ),
    (
        "FIELD.PX4.FIRMWARE_MISMATCH",
        "px4_firmware_matches",
        "FAIL",
        "PX4 firmware differs from the commissioned identity.",
    ),
    (
        "FIELD.PX4.PARAMETER_DRIFT",
        "px4_required_parameters_match",
        "FAIL",
        "Required PX4 parameters differ from the commissioned values.",
    ),
    (
        "FIELD.PARAMETERS.UNRESOLVED",
        "parameter_reconciliation_complete",
        "FAIL",
        "III parameter reconciliation or reintroduction is unresolved.",
    ),
    (
        "FIELD.MISSION.INVALID",
        "selected_mission_valid",
        "FAIL",
        "The selected mission is unavailable or incompatible with the active profile.",
    ),
    (
        "FIELD.STORAGE.RESERVE_VIOLATED",
        "storage_reserve_valid",
        "FAIL",
        "Deployment, rollback, logging, or rosbag storage reserve is violated.",
    ),
    (
        "FIELD.CREDENTIALS.INVALID",
        "credentials_valid",
        "FAIL",
        "Required operator credentials are unavailable or invalid.",
    ),
    (
        "FIELD.RUNTIME.UNHEALTHY",
        "runtime_healthy",
        "FAIL",
        "Runtime health does not satisfy the field contract.",
    ),
    (
        "FIELD.CONFIG.COLD_RESTART_PENDING",
        "cold_restart_clear",
        "FAIL",
        "A required configuration cold restart is pending.",
    ),
    (
        "FIELD.QGC.SETTINGS_DRIFT",
        "qgc_managed_settings_match",
        "WARN",
        "QGC managed settings differ from the prepared baseline.",
    ),
    (
        "FIELD.HARDWARE.OPTIONAL_MISSING",
        "optional_hardware_ready",
        "WARN",
        "Optional hardware is unavailable.",
    ),
    (
        "FIELD.BACKUP.STALE",
        "backup_fresh",
        "WARN",
        "The sealed backup no longer represents current persistent state.",
    ),
    (
        "FIELD.ARCHIVE.STALE",
        "external_archive_recent",
        "WARN",
        "No verified external archive was recorded in the last 30 days.",
    ),
    (
        "FIELD.OFFLINE_CACHE.STALE",
        "offline_cache_fresh",
        "WARN",
        "Prepared offline recovery assets are stale.",
    ),
    (
        "FIELD.LOGGING.CAPACITY_LOW",
        "logging_capacity_ready",
        "WARN",
        "Logging or rosbag capacity is below the preferred reserve.",
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_command(code: str) -> list[str]:
    if code.startswith("FIELD.CLOCK"):
        return ["iii", "system", "clock", "sync", "--target", "real"]
    if code.startswith("FIELD.OFFLINE_CACHE"):
        return ["iii", "field", "prepare"]
    if code.startswith("FIELD.BACKUP") or code.startswith("FIELD.ARCHIVE"):
        return ["iii", "records", "archive", "--help"]
    if code.startswith("FIELD.QGC"):
        return ["iii", "qgc", "status"]
    if code.startswith("FIELD.PX4"):
        return ["iii", "px4", "status"]
    if code.startswith("FIELD.PARAMETERS") or code.startswith("FIELD.CONFIG"):
        return ["iii", "configuration", "status"]
    return ["iii", "field", "check", "--json"]


def evaluate_readiness(
    observations: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    policy_hash: str,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Create a sealed, non-authorizing readiness record from live observations."""

    required_identity = (
        "boot_id",
        "drone_release_id",
        "gc_release_id",
        "profile",
        "configuration_hash",
        "commissioning_hash",
        "px4_required_state_hash",
        "mission_id",
        "qgc_pair_id",
    )
    missing = [name for name in required_identity if not observations.get(name)]
    if missing:
        raise ContractError(
            "readiness observations lack identity fields: " + ", ".join(missing)
        )
    findings: list[dict[str, Any]] = []
    for code, field, severity, message in _CHECKS:
        value = observations.get(field)
        if value is True:
            continue
        if value is not False:
            # Unknown safety state fails closed; unknown advisory state warns.
            message = f"{message} Observation {field} is unavailable."
        findings.append(
            {
                "id": code,
                "severity": severity,
                "message": message,
                "observation": field,
                "next_command": _next_command(code),
                "acknowledgeable": severity == "WARN",
            }
        )
    overall = (
        "FAIL"
        if any(item["severity"] == "FAIL" for item in findings)
        else ("WARN" if findings else "PASS")
    )
    record: dict[str, Any] = {
        "schema": READINESS_SCHEMA,
        "record_id": "0" * 64,
        "recorded_at": recorded_at or _utc_now(),
        "target": dict(target),
        "identity": {name: observations[name] for name in required_identity},
        "policy_hash": policy_hash,
        "overall": overall,
        "findings": findings,
        "observations": dict(observations),
        "evidence_class": "diagnostic",
        "authorization": False,
        "signature": None,
    }
    record["record_id"] = content_identity(
        {
            key: value
            for key, value in record.items()
            if key not in {"record_id", "signature"}
        }
    )
    return record


def _signature_message(document: Mapping[str, Any], domain: bytes) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return domain + canonical_json(unsigned)


def validate_readiness(record: Mapping[str, Any]) -> None:
    """Validate the sealed content identity before it can be signed or cited."""

    if record.get("schema") != READINESS_SCHEMA:
        raise ContractError("unsupported readiness record")
    expected = content_identity(
        {
            key: value
            for key, value in record.items()
            if key not in {"record_id", "signature"}
        }
    )
    if record.get("record_id") != expected:
        raise ContractError("readiness record content identity mismatch")
    if record.get("authorization") is not False:
        raise ContractError("readiness record must not grant authorization")
    if record.get("overall") not in {"PASS", "WARN", "FAIL"}:
        raise ContractError("readiness record severity is invalid")
    if record.get("signature") is None:
        if record.get("evidence_class") != "diagnostic":
            raise ContractError("unsigned readiness must remain diagnostic evidence")
    elif record.get("evidence_class") != "release-commissioning-evidence":
        raise ContractError("signed readiness evidence class is invalid")


def _require_authorized_field_key(
    key: Ed25519PrivateKey, trusted_signers: Mapping[str, Any]
) -> str:
    signer_id = signer_id_for_public_key(key.public_key())
    matches = [
        item
        for item in trusted_signers.get("signers", [])
        if item.get("signer_id") == signer_id
        and item.get("authority") == "workstation-field"
        and item.get("state") == "active"
    ]
    if len(matches) != 1:
        raise ContractError(
            "readiness signer is not an active authorized workstation-field signer"
        )
    return signer_id


def sign_readiness(
    record: Mapping[str, Any],
    private_key_path: Path,
    trusted_signers: Mapping[str, Any],
) -> dict[str, Any]:
    validate_readiness(record)
    key = load_private_key(private_key_path)
    signer_id = _require_authorized_field_key(key, trusted_signers)
    value = dict(record)
    value["evidence_class"] = "release-commissioning-evidence"
    value["record_id"] = content_identity(
        {
            name: item
            for name, item in value.items()
            if name not in {"record_id", "signature"}
        }
    )
    value["signature"] = {
        "algorithm": "Ed25519",
        "signer_id": signer_id,
        "value": base64.b64encode(
            key.sign(_signature_message(value, READINESS_DOMAIN))
        ).decode("ascii"),
    }
    return value


def acknowledge_warnings(
    record: Mapping[str, Any],
    warning_ids: Sequence[str],
    rationale: str,
    private_key_path: Path,
    trusted_signers: Mapping[str, Any],
    *,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    validate_readiness(record)
    if not rationale.strip():
        raise ContractError("warning acknowledgement rationale is required")
    warnings = {
        item["id"]
        for item in record.get("findings", [])
        if item.get("severity") == "WARN" and item.get("acknowledgeable") is True
    }
    selected = sorted(set(warning_ids))
    unknown = sorted(set(selected) - warnings)
    if unknown:
        raise ContractError(
            "only present warning findings can be acknowledged: " + ", ".join(unknown)
        )
    if not selected:
        raise ContractError("at least one warning finding must be acknowledged")
    if any(
        item.get("severity") == "FAIL" and item.get("id") in selected
        for item in record.get("findings", [])
    ):
        raise ContractError("failure findings cannot be acknowledged")
    key: Ed25519PrivateKey = load_private_key(private_key_path)
    signer_id = _require_authorized_field_key(key, trusted_signers)
    signature = record.get("signature")
    if signature is not None:
        if not isinstance(signature, dict) or set(signature) != {
            "algorithm",
            "signer_id",
            "value",
        }:
            raise ContractError("readiness record signature is malformed")
        if (
            signature.get("algorithm") != "Ed25519"
            or signature.get("signer_id") != signer_id
        ):
            raise ContractError(
                "readiness record signature does not match the acknowledgement signer"
            )
        try:
            key.public_key().verify(
                base64.b64decode(signature["value"], validate=True),
                _signature_message(record, READINESS_DOMAIN),
            )
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise ContractError("readiness record signature is invalid") from exc
    value: dict[str, Any] = {
        "schema": ACK_SCHEMA,
        "acknowledgement_id": "0" * 64,
        "readiness_record_id": record["record_id"],
        "recorded_at": recorded_at or _utc_now(),
        "warning_ids": selected,
        "rationale": rationale.strip(),
        "severity_changed": False,
        "authorization": False,
        "signer_authority": "workstation-field",
        "signature": None,
    }
    value["acknowledgement_id"] = content_identity(
        {
            name: item
            for name, item in value.items()
            if name not in {"acknowledgement_id", "signature"}
        }
    )
    value["signature"] = {
        "algorithm": "Ed25519",
        "signer_id": signer_id,
        "value": base64.b64encode(
            key.sign(_signature_message(value, ACK_DOMAIN))
        ).decode("ascii"),
    }
    return value


def field_cache_report(cached_releases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = sorted(
        (dict(item) for item in cached_releases), key=lambda item: item["version"]
    )
    complete = all(
        item.get("verified") is True
        and item.get("status") == "qualified"
        and item.get("components") == ["drone", "gc"]
        for item in rows
    )
    stale = sorted(
        item["version"]
        for item in rows
        if isinstance(item.get("status_age_days"), (int, float))
        and item["status_age_days"] > 7
    )
    return {
        "schema": "iii.field-cache-completeness/v1",
        "complete": bool(rows) and complete,
        "releases": rows,
        "warnings": (
            [
                {
                    "id": "FIELD.RELEASE_STATUS.STALE",
                    "severity": "WARN",
                    "versions": stale,
                    "message": "The newest verified release-status index is older than seven days.",
                }
            ]
            if stale
            else []
        ),
        "cache_id": content_identity({"releases": rows}),
    }
