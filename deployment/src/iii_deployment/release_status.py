"""Signed append-only release-status statements and index snapshots."""

from __future__ import annotations

import base64
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
    validate_status_transition,
)
from .signers import (
    load_private_key,
    signer_id_for_public_key,
    trusted_public_key,
    verify,
)

STATEMENT_DOMAIN = b"iii.release-status/v1\0"
INDEX_DOMAIN = b"iii.release-status-index/v1\0"


def _reference(statement: Mapping[str, Any]) -> dict[str, str]:
    return {
        "statement_id": str(statement["statement_id"]),
        "sha256": content_identity(statement),
    }


def _identity(value: Mapping[str, Any], identity_field: str) -> str:
    body = dict(value)
    body.pop(identity_field, None)
    body.pop("signature", None)
    return content_identity(body)


def _signature_message(value: Mapping[str, Any], domain: bytes) -> bytes:
    unsigned = dict(value)
    unsigned.pop("signature", None)
    return domain + canonical_json(unsigned)


def create_status_statement(
    *,
    operation_id: str,
    release_id: str,
    version: str,
    status: str,
    reason: str,
    superseding_version: str | None,
    recorded_at: str,
    private_key_path: Path,
    registry: ContractRegistry,
    previous_global: Mapping[str, Any] | None,
    previous_release: Mapping[str, Any] | None,
) -> dict[str, Any]:
    key = load_private_key(private_key_path)
    signer_id = signer_id_for_public_key(key.public_key())
    value: dict[str, Any] = {
        "schema_version": "1",
        "statement_id": "0" * 64,
        "sequence": (
            1 if previous_global is None else int(previous_global["sequence"]) + 1
        ),
        "operation_id": operation_id,
        "release_id": release_id,
        "version": version,
        "status": status,
        "reason": reason,
        "superseding_version": superseding_version,
        "recorded_at": recorded_at,
        "signer_id": signer_id,
        "signature_algorithm": "Ed25519",
        "previous_statement": (
            None if previous_global is None else _reference(previous_global)
        ),
        "previous_release_statement": (
            None if previous_release is None else _reference(previous_release)
        ),
    }
    value["statement_id"] = _identity(value, "statement_id")
    value["signature"] = base64.b64encode(
        key.sign(_signature_message(value, STATEMENT_DOMAIN))
    ).decode("ascii")
    registry.validate("release-status", value)
    validate_status_transition(previous_release, value, previous_global=previous_global)
    return value


def verify_status_statement(
    statement: Mapping[str, Any],
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
    *,
    history_boundary: Mapping[str, Any] | None = None,
) -> None:
    registry.validate("release-status", statement)
    if _identity(statement, "statement_id") != statement["statement_id"]:
        raise ContractError("release-status statement identity mismatch")
    selected = next(
        (
            item
            for item in trusted_signers["signers"]
            if item["signer_id"] == statement["signer_id"]
        ),
        None,
    )
    if selected is None:
        raise ContractError("release-status signer is unknown")
    allow_revoked = selected["state"] == "revoked"
    if allow_revoked:
        trusted_through = selected.get("trusted_through")
        if (
            not isinstance(trusted_through, dict)
            or history_boundary != trusted_through
            or int(statement["sequence"]) > int(trusted_through["sequence"])
        ):
            raise ContractError(
                "revoked release-status signer is outside its commissioned history boundary"
            )
    public = trusted_public_key(
        trusted_signers,
        statement["signer_id"],
        "release-status",
        allow_revoked_history=allow_revoked,
    )
    verify(
        public,
        statement["signature"],
        _signature_message(statement, STATEMENT_DOMAIN),
    )


def validate_statement_chain(
    statements: Iterable[Mapping[str, Any]],
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> list[dict[str, Any]]:
    ordered = [deepcopy(dict(statement)) for statement in statements]
    boundaries = {
        (
            int(item["trusted_through"]["sequence"]),
            item["trusted_through"]["statement_id"],
        )
        for item in trusted_signers["signers"]
        if item["state"] == "revoked" and isinstance(item.get("trusted_through"), dict)
    }
    available = {(int(item["sequence"]), item["statement_id"]) for item in ordered}
    if not boundaries.issubset(available):
        raise ContractError(
            "release-status history omits a commissioned signer revocation boundary"
        )
    boundary_by_signer = {
        item["signer_id"]: item.get("trusted_through")
        for item in trusted_signers["signers"]
        if item["state"] == "revoked"
    }
    previous_global: Mapping[str, Any] | None = None
    latest: dict[str, Mapping[str, Any]] = {}
    for statement in ordered:
        verify_status_statement(
            statement,
            trusted_signers,
            registry,
            history_boundary=boundary_by_signer.get(statement["signer_id"]),
        )
        key = str(statement["release_id"])
        previous_release = latest.get(key)
        validate_status_transition(
            previous_release, statement, previous_global=previous_global
        )
        latest[key] = statement
        previous_global = statement
    return ordered


def create_status_index(
    statements: Iterable[Mapping[str, Any]],
    *,
    generated_at: str,
    private_key_path: Path,
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    ordered = validate_statement_chain(statements, trusted_signers, registry)
    if not ordered:
        raise ContractError("release-status index cannot be empty")
    key = load_private_key(private_key_path)
    signer_id = signer_id_for_public_key(key.public_key())
    trusted_public_key(trusted_signers, signer_id, "release-status")
    value: dict[str, Any] = {
        "schema_version": "1",
        "index_type": "iii.release-status-index",
        "index_id": "0" * 64,
        "sequence": ordered[-1]["sequence"],
        "generated_at": generated_at,
        "statements": ordered,
        "signer_id": signer_id,
        "signature_algorithm": "Ed25519",
    }
    value["index_id"] = _identity(value, "index_id")
    value["signature"] = base64.b64encode(
        key.sign(_signature_message(value, INDEX_DOMAIN))
    ).decode("ascii")
    registry.validate("release-status-index", value)
    return value


def verify_status_index(
    index: Mapping[str, Any],
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, dict[str, Any]]:
    registry.validate("release-status-index", index)
    if _identity(index, "index_id") != index["index_id"]:
        raise ContractError("release-status index identity mismatch")
    public = trusted_public_key(trusted_signers, index["signer_id"], "release-status")
    verify(public, index["signature"], _signature_message(index, INDEX_DOMAIN))
    ordered = validate_statement_chain(index["statements"], trusted_signers, registry)
    if index["sequence"] != ordered[-1]["sequence"]:
        raise ContractError("release-status index sequence mismatch")
    latest: dict[str, dict[str, Any]] = {}
    versions: dict[str, str] = {}
    for statement in ordered:
        release_id = statement["release_id"]
        version = statement["version"]
        if version in versions and versions[version] != release_id:
            raise ContractError(
                "release-status index maps a version to multiple releases"
            )
        versions[version] = release_id
        latest[release_id] = statement
    return latest


def latest_status(
    index: Mapping[str, Any],
    *,
    release_id: str,
    version: str,
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    latest = verify_status_index(index, trusted_signers, registry).get(release_id)
    if latest is None or latest["version"] != version:
        raise ContractError("qualified release has no verified status statement")
    return latest


def require_fetchable_status(statement: Mapping[str, Any]) -> None:
    if statement["status"] == "withdrawn":
        raise ContractError("withdrawn release cannot be newly fetched as deployable")
    if statement["status"] == "unsafe":
        raise ContractError("unsafe release cannot be fetched or hidden")
    if statement["status"] != "qualified":
        raise ContractError("release status is not deployable")


def append_status(
    previous_index: Mapping[str, Any] | None,
    *,
    operation_id: str,
    release_id: str,
    version: str,
    status: str,
    reason: str,
    superseding_version: str | None,
    expected_statement_id: str | None,
    recorded_at: str,
    private_key_path: Path,
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Append one serialized transition or return no-op for an exact current state."""

    statements: list[dict[str, Any]] = []
    previous_global: Mapping[str, Any] | None = None
    previous_release: Mapping[str, Any] | None = None
    if previous_index is not None:
        latest = verify_status_index(previous_index, trusted_signers, registry)
        statements = [deepcopy(dict(value)) for value in previous_index["statements"]]
        previous_global = statements[-1]
        previous_release = latest.get(release_id)
        for value in latest.values():
            if value["version"] == version and value["release_id"] != release_id:
                raise ContractError(
                    "release version is already bound to another release identity"
                )
    observed_id = (
        None if previous_release is None else str(previous_release["statement_id"])
    )
    if expected_statement_id != observed_id:
        raise ContractError(
            f"stale release-status predecessor: expected {expected_statement_id!r}, observed {observed_id!r}"
        )
    if previous_release is not None and previous_release["status"] == status:
        return None
    statement = create_status_statement(
        operation_id=operation_id,
        release_id=release_id,
        version=version,
        status=status,
        reason=reason,
        superseding_version=superseding_version,
        recorded_at=recorded_at,
        private_key_path=private_key_path,
        registry=registry,
        previous_global=previous_global,
        previous_release=previous_release,
    )
    index = create_status_index(
        [*statements, statement],
        generated_at=recorded_at,
        private_key_path=private_key_path,
        trusted_signers=trusted_signers,
        registry=registry,
    )
    return statement, index
