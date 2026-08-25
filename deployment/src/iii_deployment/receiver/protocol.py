"""Fixed, schema-validated receiver actions and hostile-input rejection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping

from iii_deployment.contracts import ContractError


PROTOCOL_VERSION = "1"
IDENTITY = re.compile(r"^[a-f0-9]{64}$")
OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class Action(str, Enum):
    STATUS = "status"
    PLAN_STAGE = "plan-stage"
    STAGE = "stage"
    ACTIVATE = "activate"
    ROLLBACK = "rollback"
    CANCEL = "cancel"
    CLOCK_SYNC = "clock-sync"
    ACCESS_LIST = "access-list"
    ACCESS_ADD = "access-add"
    ACCESS_REVOKE = "access-revoke"
    NETWORK_PLAN = "network-plan"
    NETWORK_APPLY = "network-apply"
    BACKUP_SEAL = "backup-seal"


MUTATING_ACTIONS = frozenset(Action) - {Action.STATUS, Action.PLAN_STAGE, Action.ACCESS_LIST, Action.NETWORK_PLAN}


@dataclass(frozen=True)
class Request:
    action: Action
    operation_id: str
    client_id: str
    payload: Mapping[str, Any]
    nonce: str | None = None

    @classmethod
    def parse(cls, raw: bytes, *, maximum_bytes: int = 1024 * 1024) -> "Request":
        if len(raw) > maximum_bytes:
            raise ContractError("receiver request exceeds maximum size")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid receiver request JSON: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"protocol_version", "action", "operation_id", "client_id", "payload", "nonce"}:
            raise ContractError("receiver request fields do not match the fixed protocol")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ContractError("unsupported receiver protocol version")
        try:
            action = Action(value["action"])
        except ValueError as exc:
            raise ContractError("unsupported receiver action") from exc
        if not isinstance(value["operation_id"], str) or not OPERATION_ID.fullmatch(value["operation_id"]):
            raise ContractError("invalid operation ID")
        if not isinstance(value["client_id"], str) or not IDENTITY.fullmatch(value["client_id"]):
            raise ContractError("invalid client identity")
        if not isinstance(value["payload"], dict):
            raise ContractError("receiver payload must be an object")
        serialized = json.dumps(value["payload"])
        if any(token in serialized for token in ("../", "\\u0000")):
            raise ContractError("receiver payload contains a forbidden path token")
        nonce = value["nonce"]
        if action in MUTATING_ACTIONS and (not isinstance(nonce, str) or not IDENTITY.fullmatch(nonce)):
            raise ContractError("mutating receiver request needs a bound nonce")
        if action not in MUTATING_ACTIONS and nonce is not None:
            raise ContractError("read-only receiver request cannot consume a nonce")
        return cls(action, value["operation_id"], value["client_id"], value["payload"], nonce)

