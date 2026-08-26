"""Passphrase-protected, TTL-bounded signer for validated field digests only."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .contracts import ContractError, ContractRegistry, canonical_json
from .signers import signer_id_for_public_key


ALLOWED_CONTRACTS = frozenset(
    {
        "release-manifest",
        "release-status",
        "release-status-index",
        "promotion-evidence",
        "qualification-evidence",
        "field-readiness",
    }
)
SIGNING_DOMAIN = b"iii.workstation-field-validated-digest/v1\0"
DEFAULT_TTL_HOURS = 8
MAX_TTL_HOURS = 24


def _write_once(path: Path, raw: bytes, *, mode: int) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise ContractError(f"refusing to replace field signer material: {path}")
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


def generate_field_signer(
    *,
    private_key_path: Path,
    public_descriptor_path: Path,
    passphrase: bytes,
    registry: ContractRegistry,
    forbidden_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    if len(passphrase) < 12:
        raise ContractError("field signer passphrase must contain at least 12 bytes")
    private = private_key_path.expanduser().absolute()
    public_descriptor_path = public_descriptor_path.expanduser().absolute()
    for root in forbidden_roots:
        if private.is_relative_to(root.resolve()):
            raise ContractError(
                "field signer key must be generated outside the repository"
            )
    for path in (private, public_descriptor_path):
        if path.exists() or path.is_symlink():
            raise ContractError(f"refusing to replace field signer material: {path}")
    key = Ed25519PrivateKey.generate()
    public = key.public_key()
    descriptor = {
        "schema_version": "1",
        "descriptor_type": "iii.signer-public",
        "signer_id": signer_id_for_public_key(public),
        "algorithm": "Ed25519",
        "authority": "workstation-field",
        "public_key": base64.b64encode(
            public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii"),
    }
    registry.validate("signer-public", descriptor)
    encoded = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
    )
    _write_once(private, encoded, mode=0o600)
    try:
        _write_once(
            public_descriptor_path,
            canonical_json(descriptor) + b"\n",
            mode=0o644,
        )
    except Exception:
        private.unlink(missing_ok=True)
        raise
    return descriptor


def passphrase_from_keyring(account: str) -> bytes:
    if not account or any(character.isspace() for character in account):
        raise ContractError("field signer keyring account is invalid")
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "lookup",
                "service",
                "iii-field-signing",
                "account",
                account,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("OS keyring lookup is unavailable") from exc
    passphrase = result.stdout.rstrip(b"\n")
    if result.returncode != 0 or len(passphrase) < 12:
        raise ContractError(
            "OS keyring did not return a usable field signer passphrase"
        )
    return passphrase


class FieldSigningAgent:
    def __init__(
        self,
        *,
        private_key_path: Path,
        registry: ContractRegistry,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(ttl_hours, (int, float))
            or isinstance(ttl_hours, bool)
            or ttl_hours <= 0
            or ttl_hours > MAX_TTL_HOURS
        ):
            raise ContractError(
                "field signing agent TTL must be greater than 0 and at most 24 hours"
            )
        self.private_key_path = private_key_path.expanduser().absolute()
        self.registry = registry
        self.ttl_seconds = float(ttl_hours) * 3600
        self.monotonic = monotonic
        self._key: Ed25519PrivateKey | None = None
        self._expires_at = 0.0

    def unlock(self, passphrase: bytes) -> dict[str, Any]:
        if self.private_key_path.is_symlink() or not self.private_key_path.is_file():
            raise ContractError("field signer key is missing or linked")
        metadata = self.private_key_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ContractError("field signer key must be a regular owner-only file")
        try:
            loaded = serialization.load_pem_private_key(
                self.private_key_path.read_bytes(), password=passphrase
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ContractError("field signer unlock failed") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ContractError("field signer key is not Ed25519")
        self._key = loaded
        self._expires_at = self.monotonic() + self.ttl_seconds
        return self.status()

    def lock(self) -> dict[str, Any]:
        self._key = None
        self._expires_at = 0.0
        return self.status()

    def status(self) -> dict[str, Any]:
        now = self.monotonic()
        if self._key is not None and now >= self._expires_at:
            self.lock()
        return {
            "schema": "iii.field-signing-agent-status/v1",
            "unlocked": self._key is not None,
            "ttl_seconds": self.ttl_seconds,
            "remaining_seconds": max(0.0, self._expires_at - now),
            "signer_id": (
                signer_id_for_public_key(self._key.public_key())
                if self._key is not None
                else None
            ),
        }

    def sign_document(
        self, *, contract: str, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        if contract not in ALLOWED_CONTRACTS:
            raise ContractError("field signing agent refuses arbitrary input")
        self.registry.validate(contract, document)
        if not self.status()["unlocked"] or self._key is None:
            raise ContractError("field signing agent is locked or expired")
        digest = hashlib.sha256(canonical_json(document)).hexdigest()
        message = (
            SIGNING_DOMAIN + contract.encode("ascii") + b"\0" + bytes.fromhex(digest)
        )
        value = {
            "schema": "iii.field-digest-signature/v1",
            "contract": contract,
            "digest_sha256": digest,
            "signer_id": signer_id_for_public_key(self._key.public_key()),
            "authority": "workstation-field",
            "algorithm": "Ed25519",
            "signature": base64.b64encode(self._key.sign(message)).decode("ascii"),
        }
        self.registry.validate("field-digest-signature", value)
        return value


__all__ = [
    "ALLOWED_CONTRACTS",
    "DEFAULT_TTL_HOURS",
    "FieldSigningAgent",
    "MAX_TTL_HOURS",
    "generate_field_signer",
    "passphrase_from_keyring",
]
