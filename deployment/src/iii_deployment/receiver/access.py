"""Receiver-owned per-machine SSH, Runtime API, and signer verification state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import base64
from pathlib import Path
from typing import Any, Mapping, Sequence

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.identity import (
    client_id_for_public_key,
    validate_machine_enrollment,
)
from iii_deployment.signers import signer_id_for_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from iii_deployment.receiver.protocol import IDENTITY
from iii_deployment.receiver.state import atomic_bytes, atomic_document


ACCESS_SCHEMA_V1 = "iii.receiver-access-state/v1"
ACCESS_SCHEMA = "iii.receiver-access-state/v2"
RUNTIME_VERIFIER_SCHEMA = "iii.runtime-api-client-verifiers/v1"


class AccessManager:
    def __init__(
        self,
        *,
        state_path: Path,
        authorized_keys_path: Path,
        registry: ContractRegistry,
        runtime_verifiers_path: Path | None = None,
        field_signers_path: Path | None = None,
        runtime_gid: int | None = None,
        client_path: str = "/usr/bin/iii-deployment-ssh-gateway",
    ) -> None:
        if client_path != "/usr/bin/iii-deployment-ssh-gateway":
            raise ContractError(
                "receiver access command path is not the fixed SSH gateway"
            )
        self.state_path = state_path
        self.authorized_keys_path = authorized_keys_path
        self.runtime_verifiers_path = runtime_verifiers_path
        self.field_signers_path = field_signers_path
        self.runtime_gid = runtime_gid
        self.registry = registry
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

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ContractError("receiver access state is missing or linked")
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot read receiver access state: {exc}") from exc
        if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
            raise ContractError("receiver access state is not canonical JSON")
        return value

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return self._initial()
        value = self._read(self.state_path)
        if value.get("access_id") != self._identity(value):
            raise ContractError("receiver access-state identity mismatch")
        if set(value) != {"schema", "access_id", "generation", "clients"}:
            raise ContractError("receiver access state fields are malformed")
        if (
            not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or value["generation"] < 0
            or not isinstance(value.get("clients"), dict)
        ):
            raise ContractError("receiver access state is malformed")
        if value.get("schema") == ACCESS_SCHEMA_V1:
            self._validate_v1(value)
        elif value.get("schema") == ACCESS_SCHEMA:
            self._validate_v2(value)
        else:
            raise ContractError("receiver access-state schema is unsupported")
        return value

    @staticmethod
    def _validate_v1(value: Mapping[str, Any]) -> None:
        for client_id, record in value["clients"].items():
            if not IDENTITY.fullmatch(client_id) or set(record) != {
                "public_key",
                "state",
                "added_by",
                "proved_by",
            }:
                raise ContractError("legacy receiver access client is malformed")
            if client_id_for_public_key(record["public_key"]) != client_id:
                raise ContractError("legacy receiver access identity mismatch")
            AccessManager._validate_state_provenance(record)

    @staticmethod
    def _validate_state_provenance(record: Mapping[str, Any]) -> None:
        if record.get("state") not in {"pending", "active", "revoked"}:
            raise ContractError("receiver access client state is invalid")
        if not isinstance(record.get("added_by"), str) or (
            record.get("proved_by") is not None
            and not isinstance(record.get("proved_by"), str)
        ):
            raise ContractError("receiver access client provenance is invalid")

    @staticmethod
    def _validate_v2(value: Mapping[str, Any]) -> None:
        machine_ids: set[str] = set()
        runtime_verifiers: set[str] = set()
        signer_ids: set[str] = set()
        expected_fields = {
            "machine_id",
            "label",
            "public_key",
            "runtime_token_sha256",
            "field_signer_id",
            "field_signer_public_key",
            "state",
            "field_signer_state",
            "added_by",
            "proved_by",
        }
        for client_id, record in value["clients"].items():
            if (
                not IDENTITY.fullmatch(client_id)
                or not isinstance(record, dict)
                or set(record) != expected_fields
            ):
                raise ContractError("receiver machine credential record is malformed")
            if client_id_for_public_key(record["public_key"]) != client_id:
                raise ContractError("receiver SSH key/client identity mismatch")
            for field in (
                "machine_id",
                "runtime_token_sha256",
                "field_signer_id",
            ):
                if not isinstance(record.get(field), str) or not IDENTITY.fullmatch(
                    record[field]
                ):
                    raise ContractError(f"receiver machine {field} is invalid")
            if (
                not isinstance(record.get("label"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", record["label"])
                is None
            ):
                raise ContractError("receiver machine label is invalid")
            try:
                signer_public = Ed25519PublicKey.from_public_bytes(
                    base64.b64decode(
                        str(record.get("field_signer_public_key", "")), validate=True
                    )
                )
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    "receiver field signer verifier is invalid"
                ) from exc
            if signer_id_for_public_key(signer_public) != record["field_signer_id"]:
                raise ContractError("receiver field signer verifier is invalid")
            if record["machine_id"] != content_identity(
                {
                    "ssh_client_id": client_id,
                    "runtime_token_sha256": record["runtime_token_sha256"],
                    "field_signer_id": record["field_signer_id"],
                }
            ):
                raise ContractError("receiver machine identity is invalid")
            AccessManager._validate_state_provenance(record)
            if record["field_signer_state"] not in {"pending", "active", "revoked"}:
                raise ContractError("receiver field signer state is invalid")
            if (
                record["state"] == "pending"
                and record["field_signer_state"] != "pending"
            ):
                raise ContractError("pending machine has inconsistent signer state")
            if record["machine_id"] in machine_ids:
                raise ContractError("receiver machine identity is duplicated")
            if record["runtime_token_sha256"] in runtime_verifiers:
                raise ContractError("receiver Runtime API verifier is duplicated")
            if record["field_signer_id"] in signer_ids:
                raise ContractError("receiver field signer identity is duplicated")
            machine_ids.add(record["machine_id"])
            runtime_verifiers.add(record["runtime_token_sha256"])
            signer_ids.add(record["field_signer_id"])

    @staticmethod
    def _record(
        enrollment: Mapping[str, Any], *, added_by: str, state: str
    ) -> dict[str, Any]:
        return {
            "machine_id": enrollment["machine_id"],
            "label": enrollment["label"],
            "public_key": enrollment["ssh"]["public_key"],
            "runtime_token_sha256": enrollment["runtime_api"]["token_sha256"],
            "field_signer_id": enrollment["field_signing"]["signer_id"],
            "field_signer_public_key": enrollment["field_signing"]["public_key"],
            "state": state,
            "field_signer_state": state,
            "added_by": added_by,
            "proved_by": enrollment["ssh"]["client_id"] if state == "active" else None,
        }

    @staticmethod
    def _matches_enrollment(
        record: Mapping[str, Any], enrollment: Mapping[str, Any]
    ) -> bool:
        return all(
            (
                record["machine_id"] == enrollment["machine_id"],
                record["label"] == enrollment["label"],
                record["public_key"] == enrollment["ssh"]["public_key"],
                record["runtime_token_sha256"]
                == enrollment["runtime_api"]["token_sha256"],
                record["field_signer_id"] == enrollment["field_signing"]["signer_id"],
                record["field_signer_public_key"]
                == enrollment["field_signing"]["public_key"],
            )
        )

    def bootstrap(self, enrollments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Seed or migrate the Ansible-proven initial machine credentials."""

        validated = [
            validate_machine_enrollment(item, self.registry) for item in enrollments
        ]
        if not validated:
            raise ContractError(
                "receiver access bootstrap needs at least one machine enrollment"
            )
        client_ids = [item["ssh"]["client_id"] for item in validated]
        if len(set(client_ids)) != len(client_ids):
            raise ContractError("receiver access bootstrap repeats an SSH identity")
        value = self.load()
        if value["schema"] == ACCESS_SCHEMA_V1:
            active = {
                client_id: record["public_key"]
                for client_id, record in value["clients"].items()
                if record["state"] == "active"
            }
            expected = {
                item["ssh"]["client_id"]: item["ssh"]["public_key"]
                for item in validated
            }
            if active != expected or any(
                record["state"] == "pending" for record in value["clients"].values()
            ):
                raise ContractError(
                    "legacy access state differs from Ansible machine enrollments"
                )
            value = {
                "schema": ACCESS_SCHEMA,
                "access_id": value["access_id"],
                "generation": value["generation"],
                "clients": {
                    item["ssh"]["client_id"]: self._record(
                        item, added_by="ansible-bootstrap", state="active"
                    )
                    for item in validated
                },
            }
            return self._commit(value)
        if not value["clients"]:
            value["clients"] = {
                item["ssh"]["client_id"]: self._record(
                    item, added_by="ansible-bootstrap", state="active"
                )
                for item in validated
            }
            return self._commit(value)
        for item in validated:
            record = value["clients"].get(item["ssh"]["client_id"])
            if record is None or not self._matches_enrollment(record, item):
                raise ContractError(
                    "existing receiver access state differs from an Ansible "
                    "bootstrap enrollment"
                )
        self._reconcile_state_permissions()
        self._write_authorized_keys(value)
        self._write_derived(value)
        return value

    def list_clients(self) -> list[dict[str, Any]]:
        value = self.load()
        rows = []
        for client_id, record in sorted(value["clients"].items()):
            row = {
                "client_id": client_id,
                "state": record["state"],
                "ssh_key_sha256": hashlib.sha256(
                    record["public_key"].encode("ascii")
                ).hexdigest(),
                "credential_complete": value["schema"] == ACCESS_SCHEMA,
            }
            if value["schema"] == ACCESS_SCHEMA:
                row.update(
                    machine_id=record["machine_id"],
                    label=record["label"],
                    field_signer_id=record["field_signer_id"],
                    ssh_runtime_state=record["state"],
                    field_signing_state=record["field_signer_state"],
                )
            rows.append(row)
        return rows

    def require_active(self, client_id: str) -> None:
        record = self.load()["clients"].get(client_id)
        if record is None or record["state"] != "active":
            raise ContractError("receiver client is not an active authorized operator")

    def require_pending(self, client_id: str) -> None:
        record = self.load()["clients"].get(client_id)
        if record is None or record["state"] != "pending":
            raise ContractError("receiver client is not a pending machine")

    def require_pending_proof(
        self, *, requester: str, enrollment: Mapping[str, Any]
    ) -> None:
        item = validate_machine_enrollment(enrollment, self.registry)
        client_id = item["ssh"]["client_id"]
        if requester != client_id:
            raise ContractError("pending machine may prove only its own SSH credential")
        value = self.load()
        if value["schema"] != ACCESS_SCHEMA:
            raise ContractError("machine credential state requires Ansible migration")
        record = value["clients"].get(client_id)
        if (
            record is None
            or record["state"] != "pending"
            or not self._matches_enrollment(record, item)
        ):
            raise ContractError(
                "pending machine proof does not match enrolled public verifiers"
            )

    def add_pending(
        self, *, requester: str, enrollment: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.require_active(requester)
        item = validate_machine_enrollment(enrollment, self.registry)
        value = self.load()
        if value["schema"] != ACCESS_SCHEMA:
            raise ContractError("machine credential state requires Ansible migration")
        client_id = item["ssh"]["client_id"]
        candidate = self._record(item, added_by=requester, state="pending")
        existing = value["clients"].get(client_id)
        if existing is not None:
            if existing["state"] in {"pending", "active"} and self._matches_enrollment(
                existing, item
            ):
                self._write_derived(value)
                return value
            raise ContractError("machine SSH identity is already used")
        for record in value["clients"].values():
            if record["machine_id"] == item["machine_id"]:
                raise ContractError("machine identity is already enrolled")
            if record["runtime_token_sha256"] == item["runtime_api"]["token_sha256"]:
                raise ContractError("Runtime API verifier is already enrolled")
            if record["field_signer_id"] == item["field_signing"]["signer_id"]:
                raise ContractError("field signer is already enrolled")
        value["clients"][client_id] = candidate
        return self._commit(value)

    def prove(self, *, requester: str, enrollment: Mapping[str, Any]) -> dict[str, Any]:
        item = validate_machine_enrollment(enrollment, self.registry)
        client_id = item["ssh"]["client_id"]
        if requester != client_id:
            raise ContractError(
                "replacement machine must prove itself in a new SSH session"
            )
        value = self.load()
        if value["schema"] != ACCESS_SCHEMA:
            raise ContractError("machine credential state requires Ansible migration")
        record = value["clients"].get(client_id)
        if record is None:
            raise ContractError("replacement machine was not enrolled")
        if record["state"] != "pending" or not self._matches_enrollment(record, item):
            raise ContractError(
                "replacement proof differs from enrolled public verifiers"
            )
        record["state"] = "active"
        record["field_signer_state"] = "active"
        record["proved_by"] = requester
        return self._commit(value)

    def revoke(self, *, requester: str, machine_id: str) -> dict[str, Any]:
        self.require_active(requester)
        value = self.load()
        if value["schema"] != ACCESS_SCHEMA:
            raise ContractError("machine credential state requires Ansible migration")
        selected = [
            (client_id, record)
            for client_id, record in value["clients"].items()
            if record["machine_id"] == machine_id
        ]
        if len(selected) != 1 or selected[0][1]["state"] != "active":
            raise ContractError("machine credential is not active")
        active = [
            record
            for record in value["clients"].values()
            if record["state"] == "active"
        ]
        if len(active) <= 1:
            raise ContractError("cannot revoke the final usable SSH operator machine")
        selected[0][1]["state"] = "revoked"
        selected[0][1]["field_signer_state"] = "revoked"
        return self._commit(value)

    def revoke_field_signer(
        self, *, requester: str, field_signer_id: str
    ) -> dict[str, Any]:
        """Revoke signing authority without changing SSH or Runtime access."""

        self.require_active(requester)
        value = self.load()
        if value["schema"] != ACCESS_SCHEMA:
            raise ContractError("machine credential state requires Ansible migration")
        selected = [
            record
            for record in value["clients"].values()
            if record["field_signer_id"] == field_signer_id
        ]
        if len(selected) != 1 or selected[0]["field_signer_state"] != "active":
            raise ContractError("field signer is not active")
        selected[0]["field_signer_state"] = "revoked"
        return self._commit(value)

    def _commit(self, value: dict[str, Any]) -> dict[str, Any]:
        value["generation"] += 1
        value["access_id"] = self._identity(value)
        atomic_document(self.state_path, value, mode=0o640)
        self._reconcile_state_permissions()
        self._write_authorized_keys(value)
        self._write_derived(value)
        return value

    def reconcile_derived_access(self) -> None:
        """Recreate every derived public verifier file from authoritative state."""

        state_exists = self.state_path.exists() or self.state_path.is_symlink()
        value = self.load()
        if state_exists:
            self._reconcile_state_permissions()
        self._write_authorized_keys(value)
        if value["schema"] == ACCESS_SCHEMA:
            self._write_derived(value)

    def _write_authorized_keys(self, value: Mapping[str, Any]) -> None:
        lines = []
        for client_id, record in sorted(value["clients"].items()):
            if record["state"] not in {"pending", "active"}:
                continue
            command = f"{self.client_path} --client-id {client_id}"
            lines.append('restrict,command="' + command + '" ' + record["public_key"])
        raw = ("\n".join(lines) + ("\n" if lines else "")).encode("ascii")
        atomic_bytes(self.authorized_keys_path, raw, mode=0o600)

    def _write_derived(self, value: Mapping[str, Any]) -> None:
        active = [
            record
            for record in value["clients"].values()
            if record["state"] == "active"
        ]
        if self.runtime_verifiers_path is not None:
            runtime: dict[str, Any] = {
                "schema": RUNTIME_VERIFIER_SCHEMA,
                "verifier_id": "0" * 64,
                "access_id": value["access_id"],
                "generation": value["generation"],
                "clients": sorted(
                    (
                        {
                            "machine_id": record["machine_id"],
                            "label": record["label"],
                            "token_sha256": record["runtime_token_sha256"],
                        }
                        for record in active
                    ),
                    key=lambda item: item["machine_id"],
                ),
            }
            runtime["verifier_id"] = content_identity(
                {key: item for key, item in runtime.items() if key != "verifier_id"}
            )
            self.registry.validate("runtime-api-client-verifiers", runtime)
            atomic_document(self.runtime_verifiers_path, runtime, mode=0o640)
            self._set_runtime_group(self.runtime_verifiers_path)
        if self.field_signers_path is not None:
            signers = {
                "schema_version": "1",
                "store_type": "iii.trusted-signers",
                "signers": sorted(
                    (
                        {
                            "signer_id": record["field_signer_id"],
                            "algorithm": "Ed25519",
                            "authority": "workstation-field",
                            "public_key": record["field_signer_public_key"],
                            "state": record["field_signer_state"],
                        }
                        for record in value["clients"].values()
                        if record["field_signer_state"] in {"active", "revoked"}
                    ),
                    key=lambda item: item["signer_id"],
                ),
            }
            self.registry.validate("trusted-signers", signers)
            atomic_document(self.field_signers_path, signers, mode=0o640)
            self._set_runtime_group(self.field_signers_path)

    def _set_runtime_group(self, path: Path) -> None:
        if self.runtime_gid is not None:
            os.chown(path, 0, self.runtime_gid, follow_symlinks=False)

    def _reconcile_state_permissions(self) -> None:
        os.chmod(self.state_path, 0o640, follow_symlinks=False)
        self._set_runtime_group(self.state_path)


__all__ = ["AccessManager", "client_id_for_public_key"]
