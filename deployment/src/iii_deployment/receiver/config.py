"""Fixed production receiver paths and minimal root-owned host configuration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import PROFILE, TARGET_ID


CONFIG_SCHEMA = "iii.receiver-config/v1"
LIVE_STATE_SCHEMA = "iii.receiver-live-state/v1"
CONFIG_PATH = Path("/etc/iii/deployment-receiver.json")
SOCKET_PATH = Path("/run/iii/deployment-receiver.sock")
LOCK_PATH = Path("/run/iii/deployment-receiver.lock")
STATE_ROOT = Path("/var/lib/iii/deployment")
INCOMING_ROOT = Path("/var/lib/iii/incoming")
RECEIVER_ROOT = Path("/opt/iii/receiver")
RELEASE_ROOT = Path("/opt/iii/releases")
AUTHORIZED_KEYS_PATH = Path("/home/iii/.ssh/authorized_keys")
BUNDLE_TRUST_PATH = Path("/etc/iii/trust/bundle-signers.json")
STATUS_TRUST_PATH = Path("/etc/iii/trust/release-status-signers.json")
RECEIVER_UPDATE_TRUST_PATH = Path("/etc/iii/trust/receiver-update-signers.json")
OPERATIONAL_POLICY_PATH = Path("/etc/iii/operational-policy.json")
SCHEMA_ROOT = Path("/opt/iii/receiver/selectors/current/share/iii-deployment/schemas/v1")
LIVE_STATE_PATH = STATE_ROOT / "live-state.json"
AUDIT_PATH = Path("/var/log/iii/deployment/receiver-audit.jsonl")
READINESS_PATH = Path("/run/iii/receiver-readiness.json")


def under(root: Path, absolute: Path) -> Path:
    if not absolute.is_absolute():
        raise ContractError("receiver host path must be absolute")
    return root.joinpath(*absolute.parts[1:])


def _read_canonical(path: Path, *, label: str) -> dict[str, Any]:
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


@dataclass(frozen=True)
class ReceiverConfig:
    receiver_generation: int
    logical_target: str
    profile: str
    runtime_uid: int
    runtime_gid: int

    @classmethod
    def load(cls, path: Path, *, production: bool = True) -> "ReceiverConfig":
        if production and path != CONFIG_PATH:
            raise ContractError("receiver configuration path differs from fixed host policy")
        if production:
            try:
                stat_result = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ContractError(f"cannot inspect receiver configuration: {exc}") from exc
            if stat_result.st_uid != 0 or stat_result.st_mode & 0o022:
                raise ContractError("receiver configuration is not root-owned and write-protected")
        value = _read_canonical(path, label="receiver configuration")
        if set(value) != {
            "schema",
            "receiver_generation",
            "logical_target",
            "profile",
            "runtime_uid",
            "runtime_gid",
        } or value["schema"] != CONFIG_SCHEMA:
            raise ContractError("receiver configuration fields are invalid")
        if not isinstance(value["receiver_generation"], int) or value["receiver_generation"] < 1:
            raise ContractError("receiver generation is invalid")
        if not isinstance(value["logical_target"], str) or not TARGET_ID.fullmatch(
            value["logical_target"]
        ):
            raise ContractError("receiver logical target is invalid")
        if not isinstance(value["profile"], str) or not PROFILE.fullmatch(value["profile"]):
            raise ContractError("receiver profile is invalid")
        for field in ("runtime_uid", "runtime_gid"):
            if not isinstance(value[field], int) or value[field] <= 0:
                raise ContractError(f"receiver {field} is invalid")
        return cls(
            receiver_generation=value["receiver_generation"],
            logical_target=value["logical_target"],
            profile=value["profile"],
            runtime_uid=value["runtime_uid"],
            runtime_gid=value["runtime_gid"],
        )


def load_live_state(path: Path, *, profile: str) -> Mapping[str, Any]:
    value = _read_canonical(path, label="receiver live state")
    required = {
        "schema",
        "target_state_hash",
        "active_release_id",
        "configuration_hash",
        "commissioning_hash",
        "profile",
    }
    if set(value) != required or value["schema"] != LIVE_STATE_SCHEMA:
        raise ContractError("receiver live-state fields are invalid")
    expected = content_identity(
        {key: item for key, item in value.items() if key != "target_state_hash"}
    )
    if value["target_state_hash"] != expected:
        raise ContractError("receiver live-state identity mismatch")
    if value["profile"] != profile:
        raise ContractError("receiver live-state profile mismatch")
    return {key: item for key, item in value.items() if key != "schema"}


def assert_production_root() -> None:
    if os.geteuid() != 0:
        raise ContractError("deployment receiver must run with root authority")
