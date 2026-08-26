"""Ed25519 release signer generation, proof, trust, rotation, and revocation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterator, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contracts import ContractError, ContractRegistry, canonical_json

AUTHORITIES = {
    "ci-qualified",
    "workstation-field",
    "release-status",
    "receiver-update",
}
PROOF_DOMAIN = b"iii.release-signer-proof/v1\0"


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str, *, field: str, length: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"invalid base64 in {field}") from exc
    if len(decoded) != length:
        raise ContractError(f"invalid {field} length")
    return decoded


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def signer_id_for_public_key(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(_public_bytes(key)).hexdigest()


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ContractError(f"refusing signer-store symlink: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError(f"signer file is missing or unsafe: {path}")
            value = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load signer file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"signer file must contain an object: {path}")
    return value


def generate_signer(
    private_key_path: Path,
    public_descriptor_path: Path,
    *,
    authority: str,
    registry: ContractRegistry,
    forbidden_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Generate a private key once and a safe public descriptor.

    The caller supplies forbidden repository roots so operational entrypoints can
    guarantee private material never enters Git. Both destinations must be new.
    """
    if authority not in AUTHORITIES:
        raise ContractError(f"unknown signer authority: {authority}")
    private_key_path = private_key_path.absolute()
    public_descriptor_path = public_descriptor_path.absolute()
    for root in forbidden_roots:
        if private_key_path.is_relative_to(root.resolve()):
            raise ContractError(
                "private signer key must be generated outside the repository"
            )
    if private_key_path.exists() or private_key_path.is_symlink():
        raise ContractError(
            f"refusing to replace private signer key: {private_key_path}"
        )
    if public_descriptor_path.exists() or public_descriptor_path.is_symlink():
        raise ContractError(
            f"refusing to replace public signer descriptor: {public_descriptor_path}"
        )

    key = Ed25519PrivateKey.generate()
    public = key.public_key()
    descriptor = {
        "schema_version": "1",
        "descriptor_type": "iii.signer-public",
        "signer_id": signer_id_for_public_key(public),
        "algorithm": "Ed25519",
        "authority": authority,
        "public_key": _b64(_public_bytes(public)),
    }
    registry.validate("signer-public", descriptor)
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _atomic_write(private_key_path, private_bytes, mode=0o600)
    _atomic_write(
        public_descriptor_path, canonical_json(descriptor) + b"\n", mode=0o644
    )
    return descriptor


def load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ContractError(
                    "private signer key must be a regular user-only file"
                )
            encoded = stream.read()
        key = serialization.load_pem_private_key(encoded, password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ContractError(f"cannot load private signer key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ContractError("private signer key is not Ed25519")
    return key


def load_public_descriptor(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    descriptor = _load_json(path)
    registry.validate("signer-public", descriptor)
    public = Ed25519PublicKey.from_public_bytes(
        _unb64(descriptor["public_key"], field="public_key", length=32)
    )
    if signer_id_for_public_key(public) != descriptor["signer_id"]:
        raise ContractError("public signer descriptor identity mismatch")
    return descriptor


def sign(private_key_path: Path, message: bytes) -> tuple[str, str]:
    key = load_private_key(private_key_path)
    return signer_id_for_public_key(key.public_key()), _b64(key.sign(message))


def signer_proof(private_key_path: Path) -> dict[str, str]:
    key = load_private_key(private_key_path)
    signer_id = signer_id_for_public_key(key.public_key())
    signature = key.sign(PROOF_DOMAIN + signer_id.encode("ascii"))
    return {"signer_id": signer_id, "proof": _b64(signature)}


def _verify_proof(descriptor: Mapping[str, Any], proof: Mapping[str, str]) -> None:
    signer_id = descriptor["signer_id"]
    if proof.get("signer_id") != signer_id:
        raise ContractError("signer proof identity mismatch")
    public = Ed25519PublicKey.from_public_bytes(
        _unb64(descriptor["public_key"], field="public_key", length=32)
    )
    signature = _unb64(proof.get("proof", ""), field="proof", length=64)
    try:
        public.verify(signature, PROOF_DOMAIN + signer_id.encode("ascii"))
    except InvalidSignature as exc:
        raise ContractError("signer proof-of-possession is invalid") from exc


def verify_signer_proof(signer: Mapping[str, Any], proof: Mapping[str, str]) -> None:
    """Verify possession for a trusted-store entry without private material."""

    if signer.get("algorithm") != "Ed25519":
        raise ContractError("signer proof requires an Ed25519 trust entry")
    _verify_proof(signer, proof)


@contextmanager
def _store_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_name(path.name + ".lock")
    if lock.is_symlink():
        raise ContractError(f"refusing signer-store lock symlink: {lock}")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _empty_store() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "store_type": "iii.trusted-signers",
        "signers": [],
    }


def validate_trusted_signers(
    value: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    """Validate a trust store including identities not expressible in JSON Schema."""

    registry.validate("trusted-signers", value)
    identities = [item["signer_id"] for item in value["signers"]]
    if identities != sorted(set(identities)):
        raise ContractError("trusted signer identities must be unique and sorted")
    for item in value["signers"]:
        public = Ed25519PublicKey.from_public_bytes(
            _unb64(item["public_key"], field="public_key", length=32)
        )
        if signer_id_for_public_key(public) != item["signer_id"]:
            raise ContractError("trusted signer public-key identity mismatch")
        boundary = item.get("trusted_through")
        if boundary is not None and (
            item["authority"] != "release-status" or item["state"] != "revoked"
        ):
            raise ContractError(
                "only revoked release-status signers may retain a history boundary"
            )
    return dict(value)


def load_trusted_signers(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    if path.is_symlink():
        raise ContractError(f"refusing signer-store symlink: {path}")
    value = _empty_store() if not path.exists() else _load_json(path)
    return validate_trusted_signers(value, registry)


def _write_store(
    path: Path, value: Mapping[str, Any], registry: ContractRegistry
) -> None:
    registry.validate("trusted-signers", value)
    _atomic_write(path, canonical_json(value) + b"\n", mode=0o600)


def add_trusted_signer(
    store_path: Path,
    descriptor_path: Path,
    proof: Mapping[str, str],
    registry: ContractRegistry,
) -> dict[str, Any]:
    descriptor = load_public_descriptor(descriptor_path, registry)
    _verify_proof(descriptor, proof)
    with _store_lock(store_path):
        store = load_trusted_signers(store_path, registry)
        entry = {
            "signer_id": descriptor["signer_id"],
            "algorithm": "Ed25519",
            "authority": descriptor["authority"],
            "public_key": descriptor["public_key"],
            "state": "active",
        }
        existing = next(
            (
                item
                for item in store["signers"]
                if item["signer_id"] == entry["signer_id"]
            ),
            None,
        )
        if existing is not None:
            if existing != entry:
                raise ContractError(
                    "trusted signer identity already exists with different state or metadata"
                )
            return store
        store["signers"].append(entry)
        store["signers"].sort(key=lambda item: item["signer_id"])
        _write_store(store_path, store, registry)
        return store


def revoke_trusted_signer(
    store_path: Path, signer_id: str, registry: ContractRegistry
) -> dict[str, Any]:
    with _store_lock(store_path):
        store = load_trusted_signers(store_path, registry)
        selected = next(
            (item for item in store["signers"] if item["signer_id"] == signer_id), None
        )
        if selected is None:
            raise ContractError("unknown trusted signer")
        if selected["state"] == "revoked":
            return store
        if selected["authority"] == "release-status":
            raise ContractError(
                "release-status signer revocation requires a commissioned history cutover"
            )
        remaining = [
            item
            for item in store["signers"]
            if item["state"] == "active"
            and item["authority"] == selected["authority"]
            and item["signer_id"] != signer_id
        ]
        if not remaining:
            raise ContractError(
                f"cannot revoke the final active {selected['authority']} signer"
            )
        selected["state"] = "revoked"
        _write_store(store_path, store, registry)
        return store


def trusted_public_key(
    store: Mapping[str, Any],
    signer_id: str,
    authority: str,
    *,
    allow_revoked_history: bool = False,
) -> Ed25519PublicKey:
    selected = next(
        (item for item in store["signers"] if item["signer_id"] == signer_id), None
    )
    if selected is None:
        raise ContractError("bundle signer is unknown")
    if selected["state"] != "active" and not allow_revoked_history:
        raise ContractError("bundle signer is revoked")
    if selected["authority"] != authority:
        raise ContractError("bundle signer authority does not match release class")
    return Ed25519PublicKey.from_public_bytes(
        _unb64(selected["public_key"], field="public_key", length=32)
    )


def verify(public: Ed25519PublicKey, signature: str, message: bytes) -> None:
    try:
        public.verify(_unb64(signature, field="signature", length=64), message)
    except InvalidSignature as exc:
        raise ContractError("bundle signature is invalid") from exc
