"""Fixed, schema-validated receiver actions and hostile-input rejection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
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
    PLAN_HOST_MAINTENANCE = "plan-host-maintenance"
    PLAN_HOST_REBOOT = "plan-host-reboot"
    LOG_EXPORT = "log-export"
    LOG_CHUNK = "log-chunk"
    PLAN_LOG_RECEIPT = "plan-log-receipt"
    PLAN_LOG_PRUNE = "plan-log-prune"
    STAGE = "stage"
    ACTIVATE = "activate"
    ROLLBACK = "rollback"
    CANCEL = "cancel"
    CLOCK_SYNC = "clock-sync"
    LOG_RECEIPT = "log-receipt"
    LOG_PRUNE = "log-prune"
    ACCESS_LIST = "access-list"
    ACCESS_ADD = "access-add"
    ACCESS_REVOKE = "access-revoke"
    HOST_MAINTENANCE = "host-maintenance"
    HOST_REBOOT = "host-reboot"
    HOST_MAINTENANCE_STATUS = "host-maintenance-status"
    HARDWARE_INSPECT = "hardware-inspect"
    HOST_INSPECT = "host-inspect"
    NETWORK_PLAN = "network-plan"
    NETWORK_APPLY = "network-apply"
    NETWORK_CONFIRM_PLAN = "network-confirm-plan"
    NETWORK_CONFIRM = "network-confirm"
    NETWORK_STATUS = "network-status"
    PLAN_BACKUP_SEAL = "plan-backup-seal"
    BACKUP_SEAL = "backup-seal"
    BACKUP_LIST = "backup-list"
    BACKUP_SHOW = "backup-show"
    BACKUP_CHUNK = "backup-chunk"
    BACKUP_STATUS = "backup-status"
    PLAN_BACKUP_RESTORE = "plan-backup-restore"
    BACKUP_RESTORE = "backup-restore"


READ_ONLY_ACTIONS = frozenset(
    {
        Action.STATUS,
        Action.PLAN_STAGE,
        Action.PLAN_ACTIVATE,
        Action.PLAN_ROLLBACK,
        Action.PLAN_ACCESS,
        Action.PLAN_CLOCK_SYNC,
        Action.PLAN_HOST_MAINTENANCE,
        Action.PLAN_HOST_REBOOT,
        Action.LOG_EXPORT,
        Action.LOG_CHUNK,
        Action.PLAN_LOG_RECEIPT,
        Action.PLAN_LOG_PRUNE,
        Action.ACCESS_LIST,
        Action.HOST_MAINTENANCE_STATUS,
        Action.HARDWARE_INSPECT,
        Action.HOST_INSPECT,
        Action.NETWORK_PLAN,
        Action.NETWORK_CONFIRM_PLAN,
        Action.NETWORK_STATUS,
        Action.PLAN_BACKUP_SEAL,
        Action.BACKUP_LIST,
        Action.BACKUP_SHOW,
        Action.BACKUP_CHUNK,
        Action.BACKUP_STATUS,
        Action.PLAN_BACKUP_RESTORE,
        Action.CANCEL,
    }
)
MUTATING_ACTIONS = frozenset(Action) - READ_ONLY_ACTIONS
RELEASE_ID = IDENTITY
UPLOAD_ID = IDENTITY
PROFILE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TARGET_ID = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
REINTRODUCTION_KEY = re.compile(
    r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*:/[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*$"
)
# OpenSSH wire encoding: uint32(11), ``ssh-ed25519``, uint32(32), raw key.
# Comments are deliberately excluded so a key has one stable client identity.
PUBLIC_KEY = re.compile(r"^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}$")
PLAN_SCHEMA = "iii.receiver-mutation-plan/v1"
MACHINE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ED25519_PUBLIC = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def _machine_enrollment(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("machine enrollment must be an object")
    _exact(
        value,
        {
            "schema",
            "enrollment_id",
            "machine_id",
            "label",
            "ssh",
            "runtime_api",
            "field_signing",
        },
        label="machine enrollment",
    )
    if value["schema"] != "iii.machine-enrollment/v1":
        raise ContractError("machine enrollment schema is unsupported")
    for field in ("enrollment_id", "machine_id"):
        _identity(value[field], label=f"machine enrollment {field}")
    if not isinstance(value["label"], str) or not MACHINE_LABEL.fullmatch(
        value["label"]
    ):
        raise ContractError("machine enrollment label is invalid")
    ssh = value["ssh"]
    runtime = value["runtime_api"]
    signer = value["field_signing"]
    if not isinstance(ssh, dict):
        raise ContractError("machine enrollment SSH descriptor is malformed")
    _exact(ssh, {"client_id", "public_key"}, label="machine enrollment SSH")
    _identity(ssh["client_id"], label="machine enrollment SSH identity")
    if not isinstance(ssh["public_key"], str) or not PUBLIC_KEY.fullmatch(
        ssh["public_key"]
    ):
        raise ContractError("machine enrollment SSH key is invalid")
    if not isinstance(runtime, dict):
        raise ContractError("machine enrollment Runtime descriptor is malformed")
    _exact(runtime, {"token_sha256"}, label="machine enrollment Runtime API")
    _identity(runtime["token_sha256"], label="Runtime API verifier")
    if not isinstance(signer, dict):
        raise ContractError("machine enrollment signer descriptor is malformed")
    _exact(
        signer,
        {"signer_id", "algorithm", "authority", "public_key"},
        label="machine enrollment field signer",
    )
    _identity(signer["signer_id"], label="field signer identity")
    if signer["algorithm"] != "Ed25519" or signer["authority"] != "workstation-field":
        raise ContractError("machine enrollment field signer authority is invalid")
    if not isinstance(signer["public_key"], str) or not ED25519_PUBLIC.fullmatch(
        signer["public_key"]
    ):
        raise ContractError("machine enrollment field signer key is invalid")


def _access_revoke_parameters(value: Mapping[str, Any], *, label: str) -> None:
    if value.get("authority") == "machine":
        _exact(value, {"authority", "machine_id"}, label=label)
        _identity(value["machine_id"], label="access machine identity")
        return
    if value.get("authority") == "field-signing":
        _exact(value, {"authority", "field_signer_id"}, label=label)
        _identity(value["field_signer_id"], label="field signer identity")
        return
    raise ContractError("receiver access-revoke authority is invalid")


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


def validate_px4_activation_evidence(value: Any) -> None:
    """Validate hostile PX4 evidence before retaining it in a receiver plan."""

    if not isinstance(value, dict):
        raise ContractError("PX4 activation evidence must be an object")
    _exact(
        value,
        {
            "schema",
            "evidence_id",
            "captured_at",
            "release_id",
            "profile",
            "manifest_id",
            "snapshot",
            "comparison",
            "healthy",
            "writes_performed",
        },
        label="PX4 activation evidence",
    )
    if value["schema"] != "iii.px4-activation-evidence/v1":
        raise ContractError("PX4 activation evidence schema is unsupported")
    for field in ("evidence_id", "release_id", "manifest_id"):
        _identity(value[field], label=f"PX4 activation {field}")
    if value["evidence_id"] != content_identity(
        {key: item for key, item in value.items() if key != "evidence_id"}
    ):
        raise ContractError("PX4 activation evidence identity mismatch")
    if (
        not isinstance(value["captured_at"], str)
        or not value["captured_at"]
        or value["profile"] not in {"real", "sim"}
        or not isinstance(value["healthy"], bool)
        or value["writes_performed"] != 0
    ):
        raise ContractError("PX4 activation evidence metadata is invalid")
    snapshot = value["snapshot"]
    if not isinstance(snapshot, dict):
        raise ContractError("PX4 activation snapshot must be an object")
    _exact(
        snapshot,
        {
            "schema",
            "snapshot_id",
            "captured_at",
            "profile",
            "provenance",
            "target",
            "complete",
            "parameter_count",
            "parameters",
        },
        label="PX4 activation snapshot",
    )
    if (
        snapshot["schema"] != "iii.px4-parameter-snapshot/v1"
        or snapshot["profile"] != value["profile"]
        or snapshot["provenance"] != "qgc-forwarded-mavlink-observation"
        or snapshot["complete"] is not True
        or not isinstance(snapshot["parameter_count"], int)
        or isinstance(snapshot["parameter_count"], bool)
        or not 1 <= snapshot["parameter_count"] <= 4096
        or not isinstance(snapshot["parameters"], list)
        or len(snapshot["parameters"]) != snapshot["parameter_count"]
    ):
        raise ContractError("PX4 activation snapshot completeness is invalid")
    _identity(snapshot["snapshot_id"], label="PX4 activation snapshot")
    if snapshot["snapshot_id"] != content_identity(
        {
            "profile": snapshot["profile"],
            "target": snapshot["target"],
            "parameter_count": snapshot["parameter_count"],
            "parameters": snapshot["parameters"],
        }
    ):
        raise ContractError("PX4 activation snapshot identity mismatch")
    target = snapshot["target"]
    if not isinstance(target, dict):
        raise ContractError("PX4 activation target is malformed")
    _exact(
        target,
        {
            "system_id",
            "component_id",
            "armed",
            "firmware_version",
            "firmware_commit",
        },
        label="PX4 activation target",
    )
    if (
        target["armed"] is not False
        or not isinstance(target["system_id"], int)
        or isinstance(target["system_id"], bool)
        or not 1 <= target["system_id"] <= 255
        or not isinstance(target["component_id"], int)
        or isinstance(target["component_id"], bool)
        or not 0 <= target["component_id"] <= 255
        or not isinstance(target["firmware_version"], str)
        or not isinstance(target["firmware_commit"], str)
        or re.fullmatch(r"[a-f0-9]{10}", target["firmware_commit"]) is None
    ):
        raise ContractError("PX4 activation target identity is invalid")
    names: set[str] = set()
    indexes: set[int] = set()
    for parameter in snapshot["parameters"]:
        if not isinstance(parameter, dict):
            raise ContractError("PX4 activation parameter is malformed")
        _exact(
            parameter,
            {"name", "mav_type", "value", "index"},
            label="PX4 activation parameter",
        )
        name = parameter["name"]
        index = parameter["index"]
        numeric = parameter["value"]
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", name) is None
            or name in names
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index in indexes
            or parameter["mav_type"] not in {"UINT32", "INT32", "REAL32"}
            or isinstance(numeric, bool)
            or not isinstance(numeric, (int, float))
            or not math.isfinite(float(numeric))
        ):
            raise ContractError("PX4 activation parameter inventory is invalid")
        names.add(name)
        indexes.add(index)
    if indexes != set(range(snapshot["parameter_count"])):
        raise ContractError("PX4 activation parameter indices are incomplete")
    comparison = value["comparison"]
    if not isinstance(comparison, dict):
        raise ContractError("PX4 activation comparison must be an object")
    _exact(
        comparison,
        {
            "schema",
            "profile",
            "manifest_id",
            "snapshot_id",
            "inventory_complete",
            "missing",
            "unexpected",
            "drift",
            "preserved_calibration_identity",
            "required_match",
        },
        label="PX4 activation comparison",
    )
    if (
        comparison["schema"] != "iii.px4-parameter-comparison/v1"
        or comparison["profile"] != value["profile"]
        or comparison["manifest_id"] != value["manifest_id"]
        or comparison["snapshot_id"] != snapshot["snapshot_id"]
        or not isinstance(comparison["inventory_complete"], bool)
        or not isinstance(comparison["required_match"], bool)
        or any(
            not isinstance(comparison[field], list)
            for field in ("missing", "unexpected", "preserved_calibration_identity")
        )
        or not isinstance(comparison["drift"], dict)
        or set(comparison["drift"]) != {"release-required", "operator-tunable"}
        or any(not isinstance(items, list) for items in comparison["drift"].values())
    ):
        raise ContractError("PX4 activation comparison is invalid")


def _network_plan(value: Any, *, operation_id: str, client_id: str) -> None:
    if not isinstance(value, dict):
        raise ContractError("network plan must be an object")
    fields = {
        "schema",
        "network_id",
        "operation_id",
        "client_id",
        "candidate_sha256",
        "desired_netplan_sha256",
        "previous_netplan_sha256",
        "profile",
        "connectivity_impacting",
        "confirmation_deadline_s",
        "no_change",
        "declared_permissions",
        "required_checks",
    }
    _exact(value, fields, label="network plan")
    if value["schema"] != "iii.network-plan/v1":
        raise ContractError("unsupported network plan")
    for field in ("network_id", "candidate_sha256", "desired_netplan_sha256"):
        _identity(value[field], label=f"network plan {field}")
    if value["previous_netplan_sha256"] is not None:
        _identity(value["previous_netplan_sha256"], label="previous Netplan")
    if value["operation_id"] != operation_id or value["client_id"] != client_id:
        raise ContractError("network plan binding mismatch")
    if value["network_id"] != content_identity(
        {key: item for key, item in value.items() if key != "network_id"}
    ):
        raise ContractError("network plan identity mismatch")
    profile = value["profile"]
    if not isinstance(profile, dict):
        raise ContractError("redacted network profile is malformed")
    _exact(
        profile,
        {
            "ethernet_dhcp4",
            "wifi_profile_ids",
            "wifi_profile_count",
            "onboard_access_point",
        },
        label="redacted network profile",
    )
    if (
        profile["ethernet_dhcp4"] is not True
        or profile["onboard_access_point"] is not False
    ):
        raise ContractError("network plan weakens the Ethernet/no-access-point policy")
    if (
        not isinstance(profile["wifi_profile_ids"], list)
        or len(profile["wifi_profile_ids"]) != profile["wifi_profile_count"]
        or len(set(profile["wifi_profile_ids"])) != len(profile["wifi_profile_ids"])
    ):
        raise ContractError("redacted Wi-Fi profile identities are malformed")
    for profile_id in profile["wifi_profile_ids"]:
        _identity(profile_id, label="Wi-Fi profile")
    if value["confirmation_deadline_s"] != 90:
        raise ContractError("network plan changes the fixed confirmation deadline")
    for field in ("connectivity_impacting", "no_change"):
        if not isinstance(value[field], bool):
            raise ContractError(f"network plan {field} must be boolean")
    if value["no_change"] == value["connectivity_impacting"]:
        raise ContractError("network plan impact and no-change flags disagree")
    for field in ("declared_permissions", "required_checks"):
        if not isinstance(value[field], list) or not all(
            isinstance(item, str) for item in value[field]
        ):
            raise ContractError(f"network plan {field} is malformed")


def _network_input(value: Any) -> None:
    from iii_deployment.networking import validate_network_input

    if not isinstance(value, dict):
        raise ContractError("network input must be an object")
    validate_network_input(value)


def _network_confirmation(value: Any, *, operation_id: str, client_id: str) -> None:
    if not isinstance(value, dict):
        raise ContractError("network confirmation must be an object")
    _exact(
        value,
        {
            "schema",
            "confirmation_id",
            "operation_id",
            "client_id",
            "target_operation_id",
            "network_id",
        },
        label="network confirmation",
    )
    if value["schema"] != "iii.network-confirmation-plan/v1":
        raise ContractError("unsupported network confirmation")
    if value["operation_id"] != operation_id or value["client_id"] != client_id:
        raise ContractError("network confirmation binding mismatch")
    _identity(value["network_id"], label="network confirmation profile")
    if not isinstance(value["target_operation_id"], str) or not OPERATION_ID.fullmatch(
        value["target_operation_id"]
    ):
        raise ContractError("network confirmation target operation is invalid")
    if value["confirmation_id"] != content_identity(
        {key: item for key, item in value.items() if key != "confirmation_id"}
    ):
        raise ContractError("network confirmation identity mismatch")


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


def _locator(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ContractError("invalid log content locator")
    return value


def _target(value: Any, *, label: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    _exact(value, {"logical_id", "profile"}, label=label)
    if not isinstance(value["logical_id"], str) or not TARGET_ID.fullmatch(
        value["logical_id"]
    ):
        raise ContractError(f"invalid {label} logical target")
    if not isinstance(value["profile"], str) or not PROFILE.fullmatch(value["profile"]):
        raise ContractError(f"invalid {label} profile")


def _configuration_reconciliation_decisions(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 20_000:
        raise ContractError("configuration reconciliation decisions are malformed")
    for key, decision in value.items():
        if not isinstance(key, str) or not REINTRODUCTION_KEY.fullmatch(key):
            raise ContractError("configuration reconciliation decision key is invalid")
        if decision not in {"use_old", "use_new_default"}:
            raise ContractError("configuration reconciliation decision is invalid")


def _verified_files(value: Any) -> None:
    if not isinstance(value, list):
        raise ContractError("verified log files must be an array")
    observed: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ContractError("verified log file must be an object")
        _exact(item, {"locator", "content_id", "size"}, label="verified log file")
        locator = _locator(item["locator"])
        _identity(item["content_id"], label="verified log content")
        if locator in observed:
            raise ContractError("verified log locator is duplicated")
        observed.add(locator)
        if (
            not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise ContractError("verified log content size is invalid")


def _log_file(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("log prune file must be an object")
    _exact(
        value,
        {"locator", "content_id", "size", "protected"},
        label="log prune file",
    )
    _locator(value["locator"])
    _identity(value["content_id"], label="log prune content")
    if (
        not isinstance(value["size"], int)
        or isinstance(value["size"], bool)
        or value["size"] < 0
        or not isinstance(value["protected"], bool)
    ):
        raise ContractError("log prune file metadata is invalid")


def _log_prune_plan(value: Any) -> None:
    if not isinstance(value, dict):
        raise ContractError("log prune plan must be an object")
    _exact(
        value,
        {"schema", "plan_id", "receipt_id", "manifest_id", "remove", "protected"},
        label="log prune plan",
    )
    if value["schema"] != "iii.log-prune-plan/v1":
        raise ContractError("unsupported log prune plan")
    _identity(value["plan_id"], label="log prune plan")
    _identity(value["receipt_id"], label="log pull receipt")
    _identity(value["manifest_id"], label="log export manifest")
    if value["plan_id"] != content_identity(
        {key: item for key, item in value.items() if key != "plan_id"}
    ):
        raise ContractError("log prune plan identity mismatch")
    if not isinstance(value["remove"], list) or not isinstance(
        value["protected"], list
    ):
        raise ContractError("log prune plan file inventory is invalid")
    locators: set[str] = set()
    for item in [*value["remove"], *value["protected"]]:
        _log_file(item)
        if item["locator"] in locators:
            raise ContractError("log prune locator is duplicated")
        locators.add(item["locator"])
    if any(item["protected"] for item in value["remove"]):
        raise ContractError("log prune removal includes protected content")


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
        Action.LOG_RECEIPT.value,
        Action.LOG_PRUNE.value,
        Action.HOST_MAINTENANCE.value,
        Action.HOST_REBOOT.value,
        Action.NETWORK_APPLY.value,
        Action.BACKUP_SEAL.value,
        Action.BACKUP_RESTORE.value,
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
        expected = {
            "release_id",
            "configuration_checkpoint_id",
            "px4_activation_evidence",
        }
        if plan["action"] == Action.ACTIVATE.value:
            expected.add("explicit_qualified_action")
            if "configuration_reconciliation_decisions" in parameters:
                expected.add("configuration_reconciliation_decisions")
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
        validate_px4_activation_evidence(parameters["px4_activation_evidence"])
        if plan["action"] == Action.ACTIVATE.value and not isinstance(
            parameters["explicit_qualified_action"], bool
        ):
            raise ContractError("activation qualified authority must be boolean")
        if "configuration_reconciliation_decisions" in parameters:
            _configuration_reconciliation_decisions(
                parameters["configuration_reconciliation_decisions"]
            )
    elif plan["action"] == Action.CLOCK_SYNC.value:
        _exact(parameters, {"samples"}, label="receiver clock-sync parameters")
        _clock_samples(parameters["samples"])
    elif plan["action"] == Action.LOG_RECEIPT.value:
        _exact(
            parameters,
            {"manifest_id", "receipt_id", "verified_files"},
            label="log receipt parameters",
        )
        _identity(parameters["manifest_id"], label="log export manifest")
        _identity(parameters["receipt_id"], label="log pull receipt")
        _verified_files(parameters["verified_files"])
    elif plan["action"] == Action.LOG_PRUNE.value:
        _exact(parameters, {"prune_plan"}, label="log prune parameters")
        _log_prune_plan(parameters["prune_plan"])
    elif plan["action"] == Action.ACCESS_ADD.value:
        _exact(
            parameters,
            {"phase", "enrollment"},
            label="receiver access-add parameters",
        )
        if parameters["phase"] not in {"add", "prove"}:
            raise ContractError("receiver access-add phase is invalid")
        _machine_enrollment(parameters["enrollment"])
    elif plan["action"] == Action.ACCESS_REVOKE.value:
        _access_revoke_parameters(parameters, label="receiver access-revoke parameters")
    elif plan["action"] == Action.NETWORK_APPLY.value:
        _network_plan(
            parameters,
            operation_id=plan["operation_id"],
            client_id=plan["client_id"],
        )
    elif plan["action"] == Action.HOST_MAINTENANCE.value:
        required = {
            "schema",
            "maintenance_id",
            "operation_id",
            "client_id",
            "request",
            "before",
            "installed_policy_id",
            "playbook_sha256",
            "executor_sha256",
            "expected_package_changes",
            "trust_change",
            "boot_change",
            "mutations",
            "required_checks",
            "declared_permissions",
            "reboot_expected",
            "no_change",
        }
        _exact(parameters, required, label="host-maintenance parameters")
        if parameters["schema"] != "iii.host-maintenance-plan/v1":
            raise ContractError("unsupported host-maintenance plan")
        _identity(parameters["maintenance_id"], label="host maintenance")
        if parameters["maintenance_id"] != content_identity(
            {key: item for key, item in parameters.items() if key != "maintenance_id"}
        ):
            raise ContractError("host-maintenance plan identity mismatch")
        if (
            parameters["operation_id"] != plan["operation_id"]
            or parameters["client_id"] != plan["client_id"]
        ):
            raise ContractError("host-maintenance plan binding mismatch")
    elif plan["action"] == Action.HOST_REBOOT.value:
        _exact(parameters, {"maintenance_id"}, label="host-reboot parameters")
        _identity(parameters["maintenance_id"], label="host maintenance")
    elif plan["action"] == Action.BACKUP_SEAL.value:
        _exact(parameters, {"source"}, label="backup-seal parameters")
        if parameters["source"] != "receiver":
            raise ContractError("receiver backup source is invalid")
    else:
        required = {
            "schema",
            "plan_id",
            "operation_id",
            "backup_id",
            "archive_path",
            "archive_sha256",
            "policy_id",
            "portable_schema_version",
            "active_release_id",
            "clean_converged_host",
            "mutations",
        }
        _exact(parameters, required, label="backup-restore parameters")
        if parameters["schema"] != "iii.portable-restore-plan/v1":
            raise ContractError("unsupported portable restore plan")
        for field in ("plan_id", "backup_id", "archive_sha256", "policy_id"):
            _identity(parameters[field], label=f"portable restore {field}")
        if parameters["plan_id"] != content_identity(
            {key: value for key, value in parameters.items() if key != "plan_id"}
        ):
            raise ContractError("portable restore plan identity mismatch")
        if parameters["operation_id"] != plan["operation_id"]:
            raise ContractError("portable restore operation binding mismatch")
        archive_path = parameters["archive_path"]
        if (
            not isinstance(archive_path, str)
            or not archive_path.startswith("/")
            or ".." in archive_path.split("/")
            or not archive_path.endswith("/portable-state.tar")
        ):
            raise ContractError("portable restore archive path is invalid")
        if parameters["active_release_id"] is not None:
            _identity(parameters["active_release_id"], label="portable restore release")
        if (
            parameters["portable_schema_version"] != 1
            or parameters["clean_converged_host"] is not True
            or not isinstance(parameters["mutations"], list)
            or not parameters["mutations"]
        ):
            raise ContractError("portable restore compatibility/mutations are invalid")
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
    parameter_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if request.action not in {
        Action.PLAN_STAGE,
        Action.PLAN_ACTIVATE,
        Action.PLAN_ROLLBACK,
        Action.PLAN_ACCESS,
        Action.PLAN_CLOCK_SYNC,
        Action.PLAN_LOG_RECEIPT,
        Action.PLAN_LOG_PRUNE,
        Action.PLAN_HOST_MAINTENANCE,
        Action.PLAN_HOST_REBOOT,
        Action.NETWORK_PLAN,
        Action.PLAN_BACKUP_SEAL,
        Action.PLAN_BACKUP_RESTORE,
    }:
        raise ContractError(
            "only fixed receiver planning actions can create mutation plans"
        )
    target = request.payload["target"]
    if parameter_override is not None:
        parameters = parameter_override
        if request.action == Action.PLAN_LOG_RECEIPT:
            action = Action.LOG_RECEIPT.value
        elif request.action == Action.PLAN_LOG_PRUNE:
            action = Action.LOG_PRUNE.value
        elif request.action == Action.PLAN_HOST_MAINTENANCE:
            action = Action.HOST_MAINTENANCE.value
        elif request.action == Action.PLAN_HOST_REBOOT:
            action = Action.HOST_REBOOT.value
        elif request.action == Action.NETWORK_PLAN:
            action = Action.NETWORK_APPLY.value
        elif request.action == Action.PLAN_BACKUP_SEAL:
            action = Action.BACKUP_SEAL.value
        elif request.action == Action.PLAN_BACKUP_RESTORE:
            action = Action.BACKUP_RESTORE.value
        else:
            raise ContractError(
                "receiver parameter override is not allowed for this plan"
            )
    elif request.action == Action.PLAN_STAGE:
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
    if request.action in {
        Action.STATUS,
        Action.ACCESS_LIST,
        Action.HOST_MAINTENANCE_STATUS,
        Action.HARDWARE_INSPECT,
        Action.HOST_INSPECT,
        Action.BACKUP_LIST,
        Action.BACKUP_STATUS,
    }:
        _exact(payload, set(), label=f"{request.action.value} payload")
        return
    if request.action == Action.BACKUP_SHOW:
        _exact(payload, {"backup_id"}, label="backup-show payload")
        _identity(payload["backup_id"], label="portable backup")
        return
    if request.action == Action.BACKUP_CHUNK:
        _exact(payload, {"backup_id", "offset", "length"}, label="backup-chunk payload")
        _identity(payload["backup_id"], label="portable backup")
        if (
            not isinstance(payload["offset"], int)
            or isinstance(payload["offset"], bool)
            or payload["offset"] < 0
            or not isinstance(payload["length"], int)
            or isinstance(payload["length"], bool)
            or not 1 <= payload["length"] <= 4 * 1024 * 1024
        ):
            raise ContractError("portable backup chunk bounds are invalid")
        return
    if request.action == Action.PLAN_BACKUP_SEAL:
        _exact(payload, {"target"}, label="plan-backup-seal payload")
        _target(payload["target"], label="plan-backup-seal target")
        return
    if request.action == Action.PLAN_BACKUP_RESTORE:
        _exact(payload, {"backup_id", "target"}, label="plan-backup-restore payload")
        _identity(payload["backup_id"], label="portable backup")
        _target(payload["target"], label="plan-backup-restore target")
        return
    if request.action == Action.NETWORK_STATUS:
        _exact(payload, {"target_operation_id"}, label="network-status payload")
        if not isinstance(
            payload["target_operation_id"], str
        ) or not OPERATION_ID.fullmatch(payload["target_operation_id"]):
            raise ContractError("network status operation ID is invalid")
        return
    if request.action == Action.NETWORK_PLAN:
        _exact(payload, {"profile", "target"}, label="network-plan payload")
        _network_input(payload["profile"])
        _target(payload["target"], label="network-plan target")
        return
    if request.action == Action.NETWORK_CONFIRM_PLAN:
        _exact(
            payload,
            {"target_operation_id", "target"},
            label="network-confirm-plan payload",
        )
        if not isinstance(
            payload["target_operation_id"], str
        ) or not OPERATION_ID.fullmatch(payload["target_operation_id"]):
            raise ContractError("network confirmation target operation is invalid")
        _target(payload["target"], label="network-confirm-plan target")
        return
    if request.action == Action.LOG_EXPORT:
        _exact(payload, {"domain"}, label="log-export payload")
        if payload["domain"] not in {"logs", "diagnostics"}:
            raise ContractError("log export domain is invalid")
        return
    if request.action == Action.LOG_CHUNK:
        _exact(
            payload,
            {"manifest_id", "content_id", "offset", "length"},
            label="log-chunk payload",
        )
        _identity(payload["manifest_id"], label="log export manifest")
        _identity(payload["content_id"], label="log content")
        for field in ("offset", "length"):
            if not isinstance(payload[field], int) or isinstance(payload[field], bool):
                raise ContractError(f"log chunk {field} is invalid")
        if payload["offset"] < 0 or not 1 <= payload["length"] <= 512 * 1024:
            raise ContractError("log chunk bounds are invalid")
        return
    if request.action == Action.PLAN_LOG_RECEIPT:
        _exact(
            payload,
            {"manifest_id", "verified_files", "target"},
            label="plan-log-receipt payload",
        )
        _identity(payload["manifest_id"], label="log export manifest")
        _verified_files(payload["verified_files"])
        _target(payload["target"], label="plan-log-receipt target")
        return
    if request.action == Action.PLAN_LOG_PRUNE:
        _exact(
            payload,
            {"receipt_id", "target"},
            label="plan-log-prune payload",
        )
        _identity(payload["receipt_id"], label="log pull receipt")
        _target(payload["target"], label="plan-log-prune target")
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
        expected = {
            "release_id",
            "configuration_checkpoint_id",
            "explicit_qualified_action",
            "px4_activation_evidence",
        }
        if "configuration_reconciliation_decisions" in activation:
            expected.add("configuration_reconciliation_decisions")
        _exact(
            activation,
            expected,
            label="plan-activate parameters",
        )
        _identity(activation["release_id"], label="activation release")
        _identity(
            activation["configuration_checkpoint_id"],
            label="activation configuration checkpoint",
        )
        if not isinstance(activation["explicit_qualified_action"], bool):
            raise ContractError("activation qualified authority must be boolean")
        if "configuration_reconciliation_decisions" in activation:
            _configuration_reconciliation_decisions(
                activation["configuration_reconciliation_decisions"]
            )
        validate_px4_activation_evidence(activation["px4_activation_evidence"])
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
            {
                "release_id",
                "configuration_checkpoint_id",
                "px4_activation_evidence",
            },
            label="plan-rollback parameters",
        )
        _identity(rollback["release_id"], label="rollback release")
        _identity(
            rollback["configuration_checkpoint_id"],
            label="rollback configuration checkpoint",
        )
        validate_px4_activation_evidence(rollback["px4_activation_evidence"])
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
                {"phase", "enrollment"},
                label="plan access-add",
            )
            if parameters["phase"] not in {"add", "prove"}:
                raise ContractError("plan access-add phase must be add or prove")
            _machine_enrollment(parameters["enrollment"])
        else:
            _access_revoke_parameters(parameters, label="plan access-revoke")
        return
    if request.action == Action.PLAN_HOST_MAINTENANCE:
        _exact(
            payload,
            {"request", "target"},
            label="plan-host-maintenance payload",
        )
        if not isinstance(payload["request"], dict):
            raise ContractError("host-maintenance request must be an object")
        _target(payload["target"], label="plan-host-maintenance target")
        return
    if request.action == Action.PLAN_HOST_REBOOT:
        _exact(
            payload,
            {"maintenance_id", "target"},
            label="plan-host-reboot payload",
        )
        _identity(payload["maintenance_id"], label="host maintenance")
        _target(payload["target"], label="plan-host-reboot target")
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
    if request.action in {Action.LOG_RECEIPT, Action.LOG_PRUNE}:
        _exact(payload, {"plan"}, label=f"{request.action.value} payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError(f"{request.action.value} plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != request.action.value:
            raise ContractError(
                f"{request.action.value} request carries a different mutation plan"
            )
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
    if request.action in {
        Action.HOST_MAINTENANCE,
        Action.HOST_REBOOT,
        Action.BACKUP_SEAL,
        Action.BACKUP_RESTORE,
    }:
        _exact(payload, {"plan"}, label=f"{request.action.value} payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError(f"{request.action.value} plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != request.action.value:
            raise ContractError(
                f"{request.action.value} request carries a different mutation plan"
            )
        return
    if request.action == Action.NETWORK_APPLY:
        _exact(payload, {"plan", "profile"}, label="network-apply payload")
        if not isinstance(payload["plan"], dict):
            raise ContractError("network-apply plan must be an object")
        validate_mutation_plan(
            payload["plan"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        if payload["plan"]["action"] != Action.NETWORK_APPLY.value:
            raise ContractError(
                "network-apply request carries a different mutation plan"
            )
        _network_input(payload["profile"])
        return
    if request.action == Action.NETWORK_CONFIRM:
        _exact(payload, {"confirmation"}, label="network-confirm payload")
        _network_confirmation(
            payload["confirmation"],
            operation_id=request.operation_id,
            client_id=request.client_id,
        )
        return
    # Later task owners retain fixed action names but cannot smuggle arbitrary
    # paths, commands, environment, or units through an unimplemented surface.
    _exact(payload, set(), label=f"{request.action.value} payload")
