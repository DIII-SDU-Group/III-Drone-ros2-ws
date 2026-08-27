"""Authenticated local evidence for target-equivalent and physical matrix rows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.signers import load_private_key, signer_id_for_public_key
from iii_deployment.signers import trusted_public_key, verify


EVIDENCE_SCHEMA = "iii.deployment-verification-evidence/v1"
SIGNATURE_DOMAIN = b"iii.deployment-verification-evidence/v1\0"
STATUSES = {"pass", "warn", "fail", "skipped"}


def _unsigned(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "signature"}


def _signature_message(record: Mapping[str, Any]) -> bytes:
    return SIGNATURE_DOMAIN + canonical_json(_unsigned(record))


def _candidate_set(
    value: Mapping[str, Any], required_fields: Iterable[str]
) -> dict[str, Any]:
    required = tuple(required_fields)
    if set(value) != {*required, "candidate_set_id"}:
        raise ContractError("candidate set has missing or unapproved fields")
    body = {field: value[field] for field in required}
    if any(not isinstance(item, str) or not item for item in body.values()):
        raise ContractError("candidate set fields must be non-empty strings")
    if value["candidate_set_id"] != content_identity(body):
        raise ContractError("candidate set identity mismatch")
    return dict(value)


def build_evidence(
    *,
    matrix: Mapping[str, Any],
    policy: Mapping[str, Any],
    level: str,
    candidate_set: Mapping[str, Any],
    started_at: str,
    finished_at: str,
    environment: Mapping[str, str],
    impact_categories: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    if level not in policy["levels"]:
        raise ContractError("unsupported verification evidence level")
    candidate = _candidate_set(candidate_set, policy["candidate_set_fields"])
    selected_categories = sorted(set(impact_categories))
    allowed_categories = set(policy["levels"][level]["impact_categories"])
    if not selected_categories or not set(selected_categories) <= allowed_categories:
        raise ContractError(
            "evidence impact categories differ from verification policy"
        )
    definitions = {row["id"]: row for row in matrix["rows"]}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        identifier = source.get("id")
        if identifier in seen or identifier not in definitions:
            raise ContractError("evidence contains duplicate or unknown matrix row")
        seen.add(identifier)
        if definitions[identifier]["level"] != level:
            raise ContractError(f"{identifier}: evidence level differs from matrix")
        if not set(definitions[identifier]["impact_categories"]) & set(
            selected_categories
        ):
            raise ContractError(f"{identifier}: row was not selected by Q121 impact")
        status = source.get("status")
        if status not in STATUSES:
            raise ContractError(f"{identifier}: unsupported evidence status")
        reason = source.get("reason")
        if status != "pass" and (not isinstance(reason, str) or not reason.strip()):
            raise ContractError(f"{identifier}: non-pass evidence requires a reason")
        artifacts = source.get("evidence", [])
        if not isinstance(artifacts, list):
            raise ContractError(f"{identifier}: evidence artifacts must be a list")
        normalized.append(
            {
                "id": identifier,
                "status": status,
                "reason": reason,
                "evidence": [dict(item) for item in artifacts],
            }
        )
    value: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "record_id": "0" * 64,
        "matrix_id": matrix["matrix_id"],
        "policy_id": matrix["policy_id"],
        "level": level,
        "candidate_set": candidate,
        "started_at": started_at,
        "finished_at": finished_at,
        "environment": dict(environment),
        "impact_categories": selected_categories,
        "rows": sorted(normalized, key=lambda row: row["id"]),
        "signature": None,
    }
    value["record_id"] = content_identity(
        {
            key: item
            for key, item in value.items()
            if key not in {"record_id", "signature"}
        }
    )
    return value


def sign_evidence(record: Mapping[str, Any], private_key_path: Path) -> dict[str, Any]:
    validate_evidence(record, require_signature=False)
    key = load_private_key(private_key_path)
    authority = (
        "ci-qualified"
        if record.get("level") == "host-independent"
        else "workstation-field"
    )
    signed = dict(record)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "authority": authority,
        "signer_id": signer_id_for_public_key(key.public_key()),
        "value": __import__("base64")
        .b64encode(key.sign(_signature_message(record)))
        .decode("ascii"),
    }
    return signed


def validate_evidence(
    record: Mapping[str, Any],
    *,
    matrix: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    trusted_signers: Mapping[str, Any] | None = None,
    registry: ContractRegistry | None = None,
    require_signature: bool = True,
) -> None:
    if registry is not None:
        registry.validate("deployment-verification-evidence", record)
    if record.get("schema") != EVIDENCE_SCHEMA:
        raise ContractError("unsupported deployment verification evidence")
    expected = content_identity(
        {
            key: value
            for key, value in record.items()
            if key not in {"record_id", "signature"}
        }
    )
    if record.get("record_id") != expected:
        raise ContractError("deployment verification evidence identity mismatch")
    if record.get("finished_at", "") < record.get("started_at", ""):
        raise ContractError("verification evidence time interval is reversed")
    if matrix is not None:
        if record.get("matrix_id") != matrix.get("matrix_id"):
            raise ContractError("verification evidence matrix identity mismatch")
        if record.get("policy_id") != matrix.get("policy_id"):
            raise ContractError("verification evidence policy identity mismatch")
        if record.get("candidate_set", {}).get("verification_policy_id") != matrix.get(
            "policy_id"
        ):
            raise ContractError("candidate set verification policy identity mismatch")
        definitions = {row["id"]: row for row in matrix["rows"]}
        seen: set[str] = set()
        for row in record.get("rows", []):
            identifier = row.get("id")
            if identifier in seen or identifier not in definitions:
                raise ContractError("verification evidence row is duplicate or unknown")
            seen.add(identifier)
            if definitions[identifier]["level"] != record.get("level"):
                raise ContractError(
                    f"{identifier}: verification evidence level mismatch"
                )
            if not set(definitions[identifier]["impact_categories"]) & set(
                record.get("impact_categories", [])
            ):
                raise ContractError(
                    f"{identifier}: evidence is outside Q121 impact selection"
                )
    if policy is not None:
        _candidate_set(record.get("candidate_set", {}), policy["candidate_set_fields"])
        selected = set(record.get("impact_categories", []))
        allowed = set(policy["levels"][record["level"]]["impact_categories"])
        if not selected or not selected <= allowed:
            raise ContractError("verification evidence impact categories are invalid")
    signature = record.get("signature")
    expected_authority = (
        "ci-qualified"
        if record.get("level") == "host-independent"
        else "workstation-field"
    )
    if signature is None:
        if require_signature:
            raise ContractError("verification evidence is unsigned")
        return
    if trusted_signers is None:
        if require_signature:
            raise ContractError("trusted signer store is required")
        return
    if (
        signature.get("algorithm") != "Ed25519"
        or signature.get("authority") != expected_authority
    ):
        raise ContractError("verification evidence signer authority is invalid")
    key = trusted_public_key(
        trusted_signers,
        signature["signer_id"],
        expected_authority,
    )
    verify(key, signature["value"], _signature_message(record))


def read_evidence(
    path: Path,
    *,
    matrix: Mapping[str, Any],
    policy: Mapping[str, Any],
    registry: ContractRegistry,
    trusted_signers: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"verification evidence is missing or linked: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid verification evidence JSON: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError("verification evidence is not canonical JSON")
    validate_evidence(
        value,
        matrix=matrix,
        policy=policy,
        trusted_signers=trusted_signers,
        registry=registry,
    )
    evidence_root = path.parent.resolve()
    for row in value["rows"]:
        for artifact in row["evidence"]:
            relative = Path(artifact["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ContractError(
                    "verification artifact path escapes its evidence root"
                )
            unresolved = path.parent / relative
            cursor = path.parent
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ContractError(
                        f"verification artifact path contains a symlink: {relative}"
                    )
            try:
                resolved = unresolved.resolve(strict=True)
                resolved.relative_to(evidence_root)
            except (OSError, ValueError) as exc:
                raise ContractError(
                    f"verification artifact escapes its evidence root: {relative}"
                ) from exc
            if not resolved.is_file():
                raise ContractError(
                    f"verification artifact is missing or linked: {relative}"
                )
            actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if actual != artifact["sha256"]:
                raise ContractError(f"verification artifact hash mismatch: {relative}")
    return value
