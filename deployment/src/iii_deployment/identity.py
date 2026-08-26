"""Shared target identity and public-only per-computer enrollment contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .signers import signer_id_for_public_key


ENROLLMENT_SCHEMA = "iii.machine-enrollment/v1"
SHARED_PROFILE_SCHEMA = "iii.shared-target-profile/v1"
SSH_PUBLIC_KEY = re.compile(r"^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}$")
RUNTIME_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


def client_id_for_public_key(public_key: str) -> str:
    if not isinstance(public_key, str) or SSH_PUBLIC_KEY.fullmatch(public_key) is None:
        raise ContractError(
            "operator key must be canonical ssh-ed25519 public material"
        )
    return hashlib.sha256(public_key.encode("ascii")).hexdigest()


def _canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def load_shared_target_profile(
    path: Path, registry: ContractRegistry
) -> dict[str, Any]:
    value = _canonical_object(path, label="shared target profile")
    registry.validate("shared-target-profile", value)
    if value.get("schema") != SHARED_PROFILE_SCHEMA or value.get(
        "profile_id"
    ) != content_identity(
        {key: item for key, item in value.items() if key != "profile_id"}
    ):
        raise ContractError("shared target profile identity mismatch")
    return value


def _field_signer(value: Mapping[str, Any]) -> dict[str, str]:
    if (
        value.get("descriptor_type") != "iii.signer-public"
        or value.get("schema_version") != "1"
        or value.get("algorithm") != "Ed25519"
        or value.get("authority") != "workstation-field"
    ):
        raise ContractError(
            "machine enrollment requires a workstation-field public descriptor"
        )
    try:
        raw = base64.b64decode(str(value.get("public_key", "")), validate=True)
        public = Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ContractError("field signer public key is invalid") from exc
    signer_id = signer_id_for_public_key(public)
    if value.get("signer_id") != signer_id:
        raise ContractError("field signer public identity mismatch")
    return {
        "signer_id": signer_id,
        "algorithm": "Ed25519",
        "authority": "workstation-field",
        "public_key": str(value["public_key"]),
    }


def create_machine_enrollment(
    *,
    label: str,
    ssh_public_key: str,
    runtime_token: str,
    field_signer_descriptor: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label) is None:
        raise ContractError("machine label is invalid")
    if RUNTIME_TOKEN.fullmatch(runtime_token) is None:
        raise ContractError("Runtime API token is not a high-entropy URL-safe value")
    client_id = client_id_for_public_key(ssh_public_key)
    signer = _field_signer(field_signer_descriptor)
    token_sha256 = hashlib.sha256(runtime_token.encode("ascii")).hexdigest()
    machine_id = content_identity(
        {
            "ssh_client_id": client_id,
            "runtime_token_sha256": token_sha256,
            "field_signer_id": signer["signer_id"],
        }
    )
    value: dict[str, Any] = {
        "schema": ENROLLMENT_SCHEMA,
        "enrollment_id": "0" * 64,
        "machine_id": machine_id,
        "label": label,
        "ssh": {"client_id": client_id, "public_key": ssh_public_key},
        "runtime_api": {"token_sha256": token_sha256},
        "field_signing": signer,
    }
    value["enrollment_id"] = content_identity(
        {key: item for key, item in value.items() if key != "enrollment_id"}
    )
    registry.validate("machine-enrollment", value)
    return value


def validate_machine_enrollment(
    value: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    enrollment = dict(value)
    registry.validate("machine-enrollment", enrollment)
    if enrollment.get("schema") != ENROLLMENT_SCHEMA:
        raise ContractError("machine enrollment schema is unsupported")
    if enrollment["ssh"]["client_id"] != client_id_for_public_key(
        enrollment["ssh"]["public_key"]
    ):
        raise ContractError("machine enrollment SSH identity mismatch")
    signer = _field_signer(
        {
            "schema_version": "1",
            "descriptor_type": "iii.signer-public",
            **enrollment["field_signing"],
        }
    )
    machine_id = content_identity(
        {
            "ssh_client_id": enrollment["ssh"]["client_id"],
            "runtime_token_sha256": enrollment["runtime_api"]["token_sha256"],
            "field_signer_id": signer["signer_id"],
        }
    )
    if enrollment["machine_id"] != machine_id:
        raise ContractError("machine enrollment identity mismatch")
    expected = content_identity(
        {key: item for key, item in enrollment.items() if key != "enrollment_id"}
    )
    if enrollment["enrollment_id"] != expected:
        raise ContractError("machine enrollment content identity mismatch")
    return enrollment


def load_machine_enrollment(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    return validate_machine_enrollment(
        _canonical_object(path, label="machine enrollment"), registry
    )


def _atomic_bytes(path: Path, raw: bytes, *, mode: int) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ContractError(f"refusing linked credential output: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_path.exists() and not temporary_path.is_symlink():
            temporary_path.unlink()


def prepare_machine_enrollment(
    *,
    directory: Path,
    label: str,
    ssh_public_key_path: Path,
    field_signer_descriptor_path: Path,
    registry: ContractRegistry,
    forbidden_roots: tuple[Path, ...] = (),
) -> tuple[dict[str, Any], Path]:
    root = directory.expanduser().absolute().resolve(strict=False)
    for forbidden in forbidden_roots:
        if root.is_relative_to(forbidden.resolve()):
            raise ContractError(
                "machine private credentials must be prepared outside the repository"
            )
    if root.exists() and (
        root.is_symlink()
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) & 0o077
    ):
        raise ContractError("machine credential directory must be owner-only")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path = root / "runtime-api-token"
    enrollment_path = root / "enrollment.json"
    if token_path.exists() or enrollment_path.exists():
        raise ContractError("refusing to replace existing machine credentials")
    if ssh_public_key_path.is_symlink() or not ssh_public_key_path.is_file():
        raise ContractError("SSH public-key input is missing or linked")
    ssh_key = ssh_public_key_path.read_text(encoding="ascii").strip().split()
    if len(ssh_key) < 2:
        raise ContractError("SSH public key is malformed")
    ssh_public_key = f"{ssh_key[0]} {ssh_key[1]}"
    descriptor = _canonical_object(
        field_signer_descriptor_path, label="field signer descriptor"
    )
    registry.validate("signer-public", descriptor)
    token = secrets.token_urlsafe(32)
    enrollment = create_machine_enrollment(
        label=label,
        ssh_public_key=ssh_public_key,
        runtime_token=token,
        field_signer_descriptor=descriptor,
        registry=registry,
    )
    _atomic_bytes(token_path, (token + "\n").encode("ascii"), mode=0o600)
    _atomic_bytes(enrollment_path, canonical_json(enrollment) + b"\n", mode=0o600)
    return enrollment, token_path
