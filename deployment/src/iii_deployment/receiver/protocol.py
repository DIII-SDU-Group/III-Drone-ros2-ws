"""Fixed, schema-validated receiver actions and hostile-input rejection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any, Mapping

from iii_deployment.contracts import ContractError, canonical_json
from iii_deployment.contracts import content_identity


PROTOCOL_VERSION = "1"
IDENTITY = re.compile(r"^[a-f0-9]{64}$")
OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class Action(str, Enum):
    STATUS = "status"
    PLAN_STAGE = "plan-stage"
    PLAN_ACTIVATE = "plan-activate"
    PLAN_ROLLBACK = "plan-rollback"
    PLAN_ACCESS = "plan-access"
    PLAN_CLOCK_SYNC = "plan-clock-sync"
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


READ_ONLY_ACTIONS = frozenset(
    {
        Action.STATUS,
        Action.PLAN_STAGE,
        Action.PLAN_ACTIVATE,
        Action.PLAN_ROLLBACK,
        Action.PLAN_ACCESS,
        Action.PLAN_CLOCK_SYNC,
        Action.ACCESS_LIST,
        Action.NETWORK_PLAN,
        Action.CANCEL,
    }
)
MUTATING_ACTIONS = frozenset(Action) - READ_ONLY_ACTIONS
RELEASE_ID = IDENTITY
UPLOAD_ID = IDENTITY
PROFILE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TARGET_ID = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
PUBLIC_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/]{43}=$")
PLAN_SCHEMA = "iii.receiver-mutation-plan/v1"


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
        if not isinstance(value, dict) or set(value) != {
            "protocol_version",
            "action",
            "operation_id",
            "client_id",
            "payload",
            "nonce",
        }:
            raise ContractError(
                "receiver request fields do not match the fixed protocol"
            )
        if raw != canonical_json(value):
            raise ContractError("receiver request is not canonical JSON")
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ContractError("unsupported receiver protocol version")
        try:
            action = Action(value["action"])
        except ValueError as exc:
            raise ContractError("unsupported receiver action") from exc
        if not isinstance(value["operation_id"], str) or not OPERATION_ID.fullmatch(
            value["operation_id"]
        ):
            raise ContractError("invalid operation ID")
        if not isinstance(value["client_id"], str) or not IDENTITY.fullmatch(
            value["client_id"]
        ):
            raise ContractError("invalid client identity")
        if not isinstance(value["payload"], dict):
            raise ContractError("receiver payload must be an object")
        serialized = json.dumps(value["payload"])
        if any(token in serialized for token in ("../", "\\u0000")):
            raise ContractError("receiver payload contains a forbidden path token")
        nonce = value["nonce"]
        if action in MUTATING_ACTIONS and (
            not isinstance(nonce, str) or not IDENTITY.fullmatch(nonce)
        ):
            raise ContractError("mutating receiver request needs a bound nonce")
        if action not in MUTATING_ACTIONS and nonce is not None:
            raise ContractError("read-only receiver request cannot consume a nonce")
        request = cls(
            action, value["operation_id"], value["client_id"], value["payload"], nonce
        )
        validate_request_payload(request)
        return request


def _exact(value: Mapping[str, Any], fields: set[str], *, label: str) -> None:
    if set(value) != fields:
        raise ContractError(f"{label} fields do not match the fixed receiver contract")


def _identity(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ContractError(f"invalid {label}")
    return value


def _clock_samples(value: Any) -> None:
    fields = {
        "target_boot_id",
        "target_monotonic_ns",
        "target_wall_ns",
        "operator_midpoint_utc_ns",
        "rtt_ns",
        "offset_ns",
    }
    if not isinstance(value, list) or len(value) < 5:
        raise ContractError("clock synchronization requires at least five samples")
    boot_ids = set()
    for sample in value:
        if not isinstance(sample, dict) or set(sample) != fields:
            raise ContractError("clock synchronization sample fields are invalid")
        boot_id = sample["target_boot_id"]
        if not isinstance(boot_id, str) or not boot_id:
            raise ContractError("clock synchronization sample boot ID is invalid")
        boot_ids.add(boot_id)
        for field in fields - {"target_boot_id"}:
            if not isinstance(sample[field], int) or isinstance(sample[field], bool):
                raise ContractError(f"clock synchronization sample {field} is invalid")
        if sample["rtt_ns"] < 0 or sample["rtt_ns"] > 500_000_000:
            raise ContractError("clock synchronization sample RTT exceeds 500 ms")
        if sample["offset_ns"] != (
            sample["target_wall_ns"] - sample["operator_midpoint_utc_ns"]
        ):
            raise ContractError("clock synchronization sample offset is inconsistent")
    if len(boot_ids) != 1:
        raise ContractError("clock synchronization samples span target boots")


def validate_mutation_plan(
    plan: Mapping[str, Any],
    *,
    operation_id: str | None = None,
    client_id: str | None = None,
) -> None:
    _exact(
        plan,
        {
            "schema",
            "plan_id",
            "action",
            "operation_id",
            "client_id",
            "receiver_generation",
            "parameters",
            "target",
            "expected_state",
        },
        label="receiver mutation plan",
    )
    if plan["schema"] != PLAN_SCHEMA or plan["action"] not in {
        Action.STAGE.value,
        Action.ACTIVATE.value,
        Action.ROLLBACK.value,
        Action.ACCESS_ADD.value,
        Action.ACCESS_REVOKE.value,
        Action.CLOCK_SYNC.value,
    }:
        raise ContractError("unsupported receiver mutation plan")
    _identity(plan["plan_id"], label="receiver plan identity")
    if (
        content_identity(
            {key: value for key, value in plan.items() if key != "plan_id"}
        )
        != plan["plan_id"]
    ):
        raise ContractError("receiver mutation plan identity mismatch")
    if not isinstance(plan["operation_id"], str) or not OPERATION_ID.fullmatch(
        plan["operation_id"]
    ):
        raise ContractError("receiver mutation plan has invalid operation ID")
    if operation_id is not None and plan["operation_id"] != operation_id:
        raise ContractError("receiver mutation plan operation ID mismatch")
    _identity(plan["client_id"], label="receiver plan client identity")
    if client_id is not None and plan["client_id"] != client_id:
        raise ContractError("receiver mutation plan client mismatch")
    if (
        not isinstance(plan["receiver_generation"], int)
        or plan["receiver_generation"] < 1
    ):
        raise ContractError("receiver mutation plan has invalid generation")
    parameters = plan["parameters"]
    if not isinstance(parameters, dict):
        raise ContractError("receiver mutation plan parameters are malformed")
    if plan["action"] == Action.STAGE.value:
        _exact(
            parameters,
            {"release_id", "archive_sha256", "upload_id", "status_index_id"},
            label="receiver staging parameters",
        )
        for field in ("release_id", "archive_sha256", "upload_id"):
            _identity(parameters[field], label=f"staging {field}")
        if parameters["status_index_id"] is not None:
            _identity(parameters["status_index_id"], label="staging status index")
    elif plan["action"] in {Action.ACTIVATE.value, Action.ROLLBACK.value}:
        expected = {"release_id", "configuration_checkpoint_id"}
        if plan["action"] == Action.ACTIVATE.value:
            expected.add("explicit_qualified_action")
        _exact(
            parameters,
            expected,
            label=f"receiver {plan['action']} parameters",
        )
        _identity(parameters["release_id"], label="activation release")
        _identity(
            parameters["configuration_checkpoint_id"],
            label="activation configuration checkpoint",
        )
        if plan["action"] == Action.ACTIVATE.value and not isinstance(
            parameters["explicit_qualified_action"], bool
        ):
            raise ContractError("activation qualified authority must be boolean")
    elif plan["action"] == Action.CLOCK_SYNC.value:
        _exact(parameters, {"samples"}, label="receiver clock-sync parameters")
        _clock_samples(parameters["samples"])
    elif plan["action"] == Action.ACCESS_ADD.value:
        _exact(
            parameters,
            {"phase", "client_id", "public_key"},
            label="receiver access-add parameters",
        )
        if parameters["phase"] not in {"add", "prove"}:
            raise ContractError("receiver access-add phase is invalid")
        _identity(parameters["client_id"], label="access client identity")
        if not isinstance(parameters["public_key"], str) or not PUBLIC_KEY.fullmatch(
            parameters["public_key"]
        ):
            raise ContractError("receiver access-add public key is invalid")
    else:
        _exact(parameters, {"client_id"}, label="receiver access-revoke parameters")
        _identity(parameters["client_id"], label="access client identity")
    target = plan["target"]
    if not isinstance(target, dict):
        raise ContractError("receiver mutation plan target is malformed")
    _exact(target, {"logical_id", "profile"}, label="receiver target")
    if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
        target["logical_id"]
    ):
        raise ContractError("receiver mutation plan has invalid logical target")
    if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
        target["profile"]
    ):
        raise ContractError("receiver mutation plan has invalid profile")
    expected = plan["expected_state"]
    if not isinstance(expected, dict):
        raise ContractError("receiver mutation plan expected state is malformed")
    _exact(
        expected,
        {
            "active_release_id",
            "configuration_hash",
            "commissioning_hash",
            "profile",
            "target_state_hash",
            "access_state_id",
        },
        label="receiver expected state",
    )
    for field in (
        "configuration_hash",
        "commissioning_hash",
        "target_state_hash",
        "access_state_id",
    ):
        _identity(expected[field], label=f"expected {field}")
    if expected["active_release_id"] is not None:
        _identity(expected["active_release_id"], label="expected active release")
    if expected["profile"] != target["profile"]:
        raise ContractError("receiver mutation plan profile binding mismatch")


def create_mutation_plan(
    request: Request,
    *,
    receiver_generation: int,
    live_state: Mapping[str, Any],
) -> dict[str, Any]:
    if request.action not in {
        Action.PLAN_STAGE,
        Action.PLAN_ACTIVATE,
        Action.PLAN_ROLLBACK,
        Action.PLAN_ACCESS,
        Action.PLAN_CLOCK_SYNC,
    }:
        raise ContractError(
            "only fixed receiver planning actions can create mutation plans"
        )
    target = request.payload["target"]
    if request.action == Action.PLAN_STAGE:
        action = Action.STAGE.value
        parameters = request.payload["artifact"]
    elif request.action == Action.PLAN_ACTIVATE:
        action = Action.ACTIVATE.value
        parameters = request.payload["activation"]
    elif request.action == Action.PLAN_ROLLBACK:
        action = Action.ROLLBACK.value
        parameters = request.payload["rollback"]
    elif request.action == Action.PLAN_CLOCK_SYNC:
        action = Action.CLOCK_SYNC.value
        parameters = {"samples": request.payload["samples"]}
    else:
        action = request.payload["action"]
        parameters = request.payload["parameters"]
    value: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "plan_id": "0" * 64,
        "action": action,
        "operation_id": request.operation_id,
        "client_id": request.client_id,
        "receiver_generation": receiver_generation,
        "parameters": dict(parameters),
        "target": dict(target),
        "expected_state": {
            "active_release_id": live_state.get("active_release_id"),
            "configuration_hash": live_state["configuration_hash"],
            "commissioning_hash": live_state["commissioning_hash"],
            "profile": live_state["profile"],
            "target_state_hash": live_state["target_state_hash"],
            "access_state_id": live_state["access_state_id"],
        },
    }
    value["plan_id"] = content_identity(
        {key: item for key, item in value.items() if key != "plan_id"}
    )
    validate_mutation_plan(
        value, operation_id=request.operation_id, client_id=request.client_id
    )
    return value


def validate_request_payload(request: Request) -> None:
    payload = request.payload
    if request.action in {Action.STATUS, Action.ACCESS_LIST}:
        _exact(payload, set(), label=f"{request.action.value} payload")
        return
    if request.action == Action.PLAN_STAGE:
        _exact(payload, {"artifact", "target"}, label="plan-stage payload")
        artifact = payload["artifact"]
        target = payload["target"]
        if not isinstance(artifact, dict) or not isinstance(target, dict):
            raise ContractError("plan-stage artifact and target must be objects")
        _exact(
            artifact,
            {"release_id", "archive_sha256", "upload_id", "status_index_id"},
            label="plan-stage artifact",
        )
        for field in ("release_id", "archive_sha256", "upload_id"):
            _identity(artifact[field], label=f"artifact {field}")
        if artifact["status_index_id"] is not None:
            _identity(artifact["status_index_id"], label="artifact status index")
        _exact(target, {"logical_id", "profile"}, label="plan-stage target")
        if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
            target["logical_id"]
        ):
            raise ContractError("invalid plan-stage logical target")
        if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
            target["profile"]
        ):
            raise ContractError("invalid plan-stage profile")
        return
    if request.action == Action.PLAN_ACTIVATE:
        _exact(payload, {"activation", "target"}, label="plan-activate payload")
        activation = payload["activation"]
        target = payload["target"]
        if not isinstance(activation, dict) or not isinstance(target, dict):
            raise ContractError("plan-activate activation and target must be objects")
        _exact(
            activation,
            {
                "release_id",
                "configuration_checkpoint_id",
                "explicit_qualified_action",
            },
            label="plan-activate parameters",
        )
        _identity(activation["release_id"], label="activation release")
        _identity(
            activation["configuration_checkpoint_id"],
            label="activation configuration checkpoint",
        )
        if not isinstance(activation["explicit_qualified_action"], bool):
            raise ContractError("activation qualified authority must be boolean")
        _exact(target, {"logical_id", "profile"}, label="plan-activate target")
        if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
            target["logical_id"]
        ):
            raise ContractError("invalid plan-activate logical target")
        if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
            target["profile"]
        ):
            raise ContractError("invalid plan-activate profile")
        return
    if request.action == Action.PLAN_ROLLBACK:
        _exact(payload, {"rollback", "target"}, label="plan-rollback payload")
        rollback = payload["rollback"]
        target = payload["target"]
        if not isinstance(rollback, dict) or not isinstance(target, dict):
            raise ContractError("plan-rollback rollback and target must be objects")
        _exact(
            rollback,
            {"release_id", "configuration_checkpoint_id"},
            label="plan-rollback parameters",
        )
        _identity(rollback["release_id"], label="rollback release")
        _identity(
            rollback["configuration_checkpoint_id"],
            label="rollback configuration checkpoint",
        )
        _exact(target, {"logical_id", "profile"}, label="plan-rollback target")
        if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
            target["logical_id"]
        ):
            raise ContractError("invalid plan-rollback logical target")
        if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
            target["profile"]
        ):
            raise ContractError("invalid plan-rollback profile")
        return
    if request.action == Action.PLAN_CLOCK_SYNC:
        _exact(payload, {"samples", "target"}, label="plan-clock-sync payload")
        _clock_samples(payload["samples"])
        target = payload["target"]
        if not isinstance(target, dict):
            raise ContractError("plan-clock-sync target must be an object")
        _exact(target, {"logical_id", "profile"}, label="plan-clock-sync target")
        if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
            target["logical_id"]
        ):
            raise ContractError("invalid plan-clock-sync logical target")
        if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
            target["profile"]
        ):
            raise ContractError("invalid plan-clock-sync profile")
        return
    if request.action == Action.PLAN_ACCESS:
        _exact(payload, {"action", "parameters", "target"}, label="plan-access payload")
        if payload["action"] not in {
            Action.ACCESS_ADD.value,
            Action.ACCESS_REVOKE.value,
        }:
            raise ContractError("plan-access action is unsupported")
        parameters = payload["parameters"]
        target = payload["target"]
        if not isinstance(parameters, dict) or not isinstance(target, dict):
            raise ContractError("plan-access parameters and target must be objects")
        _exact(target, {"logical_id", "profile"}, label="plan-access target")
        if not isinstance(target["logical_id"], str) or not TARGET_ID.fullmatch(
            target["logical_id"]
        ):
            raise ContractError("invalid plan-access logical target")
        if not isinstance(target["profile"], str) or not PROFILE.fullmatch(
            target["profile"]
        ):
            raise ContractError("invalid plan-access profile")
        if payload["action"] == Action.ACCESS_ADD.value:
            _exact(
                parameters,
                {"phase", "client_id", "public_key"},
                label="plan access-add",
            )
            if parameters["phase"] not in {"add", "prove"}:
                raise ContractError("plan access-add phase must be add or prove")
            _identity(parameters["client_id"], label="access client identity")
            if not isinstance(
                parameters["public_key"], str
            ) or not PUBLIC_KEY.fullmatch(parameters["public_key"]):
                raise ContractError(
                    "plan access public key must be canonical ssh-ed25519"
                )
        else:
            _exact(parameters, {"client_id"}, label="plan access-revoke")
            _identity(parameters["client_id"], label="access client identity")
        return
    if request.action == Action.STAGE:
        _exact(payload, {"plan"}, label="stage payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("stage plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.STAGE.value:
            raise ContractError("stage request carries a different mutation plan")
        return
    if request.action == Action.ACTIVATE:
        _exact(payload, {"plan"}, label="activate payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("activate plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.ACTIVATE.value:
            raise ContractError("activate request carries a different mutation plan")
        return
    if request.action == Action.ROLLBACK:
        _exact(payload, {"plan"}, label="rollback payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("rollback plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.ROLLBACK.value:
            raise ContractError("rollback request carries a different mutation plan")
        return
    if request.action == Action.CLOCK_SYNC:
        _exact(payload, {"plan"}, label="clock-sync payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("clock-sync plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.CLOCK_SYNC.value:
            raise ContractError("clock-sync request carries a different mutation plan")
        return
    if request.action == Action.CANCEL:
        _exact(payload, {"target_operation_id"}, label="cancel payload")
        if not isinstance(
            payload["target_operation_id"], str
        ) or not OPERATION_ID.fullmatch(payload["target_operation_id"]):
            raise ContractError("invalid cancellation operation ID")
        return
    if request.action == Action.ACCESS_ADD:
        _exact(payload, {"plan"}, label="access-add payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("access-add plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.ACCESS_ADD.value:
            raise ContractError("access-add request carries a different mutation plan")
        return
    if request.action == Action.ACCESS_REVOKE:
        _exact(payload, {"plan"}, label="access-revoke payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("access-revoke plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.ACCESS_REVOKE.value:
            raise ContractError(
                "access-revoke request carries a different mutation plan"
            )
        return
    # Later task owners retain fixed action names but cannot smuggle arbitrary
    # paths, commands, environment, or units through an unimplemented surface.
    _exact(payload, set(), label=f"{request.action.value} payload")
