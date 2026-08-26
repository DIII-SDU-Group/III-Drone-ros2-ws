"""Receiver-owned add/prove/revoke SSH operator-key sequencing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import IDENTITY, PUBLIC_KEY
from iii_deployment.receiver.state import atomic_bytes, atomic_document


ACCESS_SCHEMA = "iii.receiver-access-state/v1"


def client_id_for_public_key(public_key: str) -> str:
    if not PUBLIC_KEY.fullmatch(public_key):
        raise ContractError("operator key must be canonical ssh-ed25519 public material")
    return hashlib.sha256(public_key.encode("ascii")).hexdigest()


class AccessManager:
    def __init__(
        self,
        *,
        state_path: Path,
        authorized_keys_path: Path,
        client_path: str = "/usr/bin/iii-deploymentctl",
    ) -> None:
        if client_path != "/usr/bin/iii-deploymentctl":
            raise ContractError("receiver access command path is not the fixed deployment client")
        self.state_path = state_path
        self.authorized_keys_path = authorized_keys_path
        self.client_path = client_path

    def _initial(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": ACCESS_SCHEMA,
            "access_id": "",
            "generation": 0,
            "clients": {},
        }
        value["access_id"] = self._identity(value)
        return value

    @staticmethod
    def _identity(value: Mapping[str, Any]) -> str:
        return content_identity(
            {key: item for key, item in value.items() if key != "access_id"}
        )

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return self._initial()
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise ContractError("receiver access state is missing or linked")
        try:
            raw = self.state_path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot read receiver access state: {exc}") from exc
        if (
            not isinstance(value, dict)
            or raw != canonical_json(value) + b"\n"
            or value.get("schema") != ACCESS_SCHEMA
            or value.get("access_id") != self._identity(value)
        ):
            raise ContractError("receiver access-state identity mismatch")
        if set(value) != {"schema", "access_id", "generation", "clients"}:
            raise ContractError("receiver access state fields are malformed")
        if (
            not isinstance(value.get("generation"), int)
            or value["generation"] < 0
            or not isinstance(value.get("clients"), dict)
        ):
            raise ContractError("receiver access state is malformed")
        for client_id, record in value["clients"].items():
            if not IDENTITY.fullmatch(client_id) or set(record) != {
                "public_key",
                "state",
                "added_by",
                "proved_by",
            }:
                raise ContractError("receiver access client record is malformed")
            if client_id_for_public_key(record["public_key"]) != client_id:
                raise ContractError("receiver access key/client identity mismatch")
            if record["state"] not in {"pending", "active", "revoked"}:
                raise ContractError("receiver access client state is invalid")
            if not isinstance(record["added_by"], str) or (
                record["proved_by"] is not None and not isinstance(record["proved_by"], str)
            ):
                raise ContractError("receiver access client provenance is invalid")
        return value

    def bootstrap(self, public_keys: list[str]) -> dict[str, Any]:
        """Seed the first Ansible-proven operator keys exactly once."""

        value = self.load()
        if value["clients"]:
            raise ContractError("receiver access bootstrap is already complete")
        if not public_keys:
            raise ContractError("receiver access bootstrap needs at least one operator key")
        for public_key in public_keys:
            client_id = client_id_for_public_key(public_key)
            if client_id in value["clients"]:
                raise ContractError("receiver access bootstrap repeats an operator key")
            value["clients"][client_id] = {
                "public_key": public_key,
                "state": "active",
                "added_by": "ansible-bootstrap",
                "proved_by": client_id,
            }
        return self._commit(value)

    def list_clients(self) -> list[dict[str, Any]]:
        value = self.load()
        return [
            {
                "client_id": client_id,
                "state": record["state"],
                "key_sha256": hashlib.sha256(record["public_key"].encode("ascii")).hexdigest(),
            }
            for client_id, record in sorted(value["clients"].items())
        ]

    def require_active(self, client_id: str) -> None:
        record = self.load()["clients"].get(client_id)
        if record is None or record["state"] != "active":
            raise ContractError("receiver client is not an active authorized operator")

    def require_pending_proof(
        self,
        *,
        requester: str,
        client_id: str,
        public_key: str,
    ) -> None:
        if requester != client_id:
            raise ContractError("pending operator may only plan its own credential proof")
        record = self.load()["clients"].get(client_id)
        if (
            record is None
            or record["state"] != "pending"
            or record["public_key"] != public_key
        ):
            raise ContractError("pending operator proof does not match enrolled credential")

    def add_pending(
        self,
        *,
        requester: str,
        client_id: str,
        public_key: str,
    ) -> dict[str, Any]:
        self.require_active(requester)
        if client_id_for_public_key(public_key) != client_id:
            raise ContractError("new operator key differs from requested client identity")
        value = self.load()
        existing = value["clients"].get(client_id)
        if existing is not None:
            if existing["public_key"] == public_key and existing["state"] in {"pending", "active"}:
                self._write_authorized_keys(value)
                return value
            raise ContractError("operator client identity is already used")
        value["clients"][client_id] = {
            "public_key": public_key,
            "state": "pending",
            "added_by": requester,
            "proved_by": None,
        }
        return self._commit(value)

    def prove(
        self,
        *,
        requester: str,
        client_id: str,
        public_key: str,
    ) -> dict[str, Any]:
        if requester != client_id:
            raise ContractError("replacement credential must prove itself in a new authenticated session")
        if client_id_for_public_key(public_key) != client_id:
            raise ContractError("replacement credential proof key identity mismatch")
        value = self.load()
        record = value["clients"].get(client_id)
        if record is None or record["public_key"] != public_key:
            raise ContractError("replacement credential was not enrolled")
        if record["state"] == "revoked":
            raise ContractError("revoked operator credential cannot be reproved in-band")
        record["state"] = "active"
        record["proved_by"] = requester
        return self._commit(value)

    def revoke(self, *, requester: str, client_id: str) -> dict[str, Any]:
        self.require_active(requester)
        value = self.load()
        record = value["clients"].get(client_id)
        if record is None or record["state"] != "active":
            raise ContractError("operator credential is not active")
        active = [key for key, item in value["clients"].items() if item["state"] == "active"]
        if len(active) <= 1:
            raise ContractError("cannot revoke the final usable SSH operator key")
        record["state"] = "revoked"
        return self._commit(value)

    def _commit(self, value: dict[str, Any]) -> dict[str, Any]:
        value["generation"] += 1
        value["access_id"] = self._identity(value)
        atomic_document(self.state_path, value)
        self._write_authorized_keys(value)
        return value

    def reconcile_authorized_keys(self) -> None:
        """Recreate the derived forced-command file from authoritative state."""

        self._write_authorized_keys(self.load())

    def _write_authorized_keys(self, value: Mapping[str, Any]) -> None:
        lines = []
        for client_id, record in sorted(value["clients"].items()):
            if record["state"] not in {"pending", "active"}:
                continue
            command = f"{self.client_path} --client-id {client_id}"
            lines.append(
                "restrict,command=\""
                + command
                + "\" "
                + record["public_key"]
            )
        raw = ("\n".join(lines) + ("\n" if lines else "")).encode("ascii")
        atomic_bytes(self.authorized_keys_path, raw, mode=0o600)
