"""Receiver-owned candidate health, durable acceptance, and rollback.

The receiver accepts an activation request and then owns it to a terminal state.
Nothing in this module polls an offboard client.  Every pre-acceptance failure,
including reboot reconciliation, restores the previous composite selector and
starts only the previous control plane.  Once release acceptance is durable,
automatic selector rollback is permanently disabled for that transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol

from iii_deployment.activation import (
    ActivationSafetyGate,
    ActivationSafetySnapshot,
    ActivationTransactionStore,
    ActivationTuple,
    MaintenanceOverride,
)
from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import (
    OPERATION_ID,
    validate_px4_activation_evidence,
)
from iii_deployment.receiver.state import atomic_document
from iii_deployment.staging import ActivationAuthorization, ReleaseStore

HEALTH_SCHEMA = "iii.activation-health/v1"
POLICY_SCHEMA = "iii.activation-health-policy/v1"
STATE_SCHEMA = "iii.activation-health-transaction/v1"
DIAGNOSTIC_SCHEMA = "iii.activation-diagnostic/v1"
CONTROL_PROOF_SCHEMA = "iii.activation-control-plane-proof/v1"
STABLE_WINDOW_S = 10.0
HARD_DEADLINE_S = 120.0
ROLLBACK_TARGET_S = 60.0
HASH = re.compile(r"^(?:sha256:)?[a-f0-9]{64}$")
ENTITY = re.compile(r"^[a-z][a-z0-9_.@-]{0,127}$")
TERMINAL_STAGES = frozenset({"accepted", "rolled-back", "faulted"})
PRE_ACCEPTANCE_STAGES = frozenset(
    {
        "prepared",
        "selector-switched",
        "control-plane-started",
        "health-observing",
        "acceptance-evidence-persisted",
        "rollback-prepared",
    }
)


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def _require_hash(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ContractError(f"invalid {label}")


def _require_operation(value: str) -> None:
    if not OPERATION_ID.fullmatch(value):
        raise ContractError("invalid activation operation identity")


def _require_entity(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not ENTITY.fullmatch(value):
        raise ContractError(f"invalid {label}")


def _exact(value: Mapping[str, Any], fields: set[str], *, label: str) -> None:
    if set(value) != fields:
        raise ContractError(f"{label} fields do not match the fixed contract")


def _canonical_document(path: Path, *, label: str) -> dict[str, Any]:
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
class ActivationHealthPolicy:
    required_hardware_roles: tuple[str, ...]
    optional_hardware_roles: tuple[str, ...]
    required_services: tuple[str, ...]
    optional_services: tuple[str, ...]
    required_managed_nodes: Mapping[str, str]
    optional_managed_nodes: Mapping[str, str]
    required_systemd_units: tuple[str, ...]
    schema: str = POLICY_SCHEMA

    @classmethod
    def from_profile(cls, profile: Mapping[str, Any]) -> "ActivationHealthPolicy":
        health = profile.get("health")
        if not isinstance(health, dict):
            raise ContractError(
                "release profile lacks a signed activation health policy"
            )
        _exact(
            health,
            {
                "schema",
                "required_hardware_roles",
                "optional_hardware_roles",
                "required_services",
                "optional_services",
                "required_managed_nodes",
                "optional_managed_nodes",
                "required_systemd_units",
            },
            label="activation health policy",
        )
        policy = cls(
            required_hardware_roles=tuple(health["required_hardware_roles"]),
            optional_hardware_roles=tuple(health["optional_hardware_roles"]),
            required_services=tuple(health["required_services"]),
            optional_services=tuple(health["optional_services"]),
            required_managed_nodes=dict(health["required_managed_nodes"]),
            optional_managed_nodes=dict(health["optional_managed_nodes"]),
            required_systemd_units=tuple(health["required_systemd_units"]),
            schema=health["schema"],
        )
        policy.validate()
        return policy

    def as_document(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "required_hardware_roles",
            "optional_hardware_roles",
            "required_services",
            "optional_services",
            "required_systemd_units",
        ):
            value[field] = list(value[field])
        return value

    def validate(self) -> None:
        if self.schema != POLICY_SCHEMA:
            raise ContractError("activation health policy schema is unsupported")
        list_fields = (
            (self.required_hardware_roles, "required hardware role"),
            (self.optional_hardware_roles, "optional hardware role"),
            (self.required_services, "required service"),
            (self.optional_services, "optional service"),
            (self.required_systemd_units, "required systemd unit"),
        )
        for values, label in list_fields:
            if tuple(sorted(set(values))) != values:
                raise ContractError(f"{label} declarations must be sorted and unique")
            for value in values:
                _require_entity(value, label=label)
        for required, optional, label in (
            (
                set(self.required_hardware_roles),
                set(self.optional_hardware_roles),
                "hardware role",
            ),
            (set(self.required_services), set(self.optional_services), "service"),
            (
                set(self.required_managed_nodes),
                set(self.optional_managed_nodes),
                "managed node",
            ),
        ):
            overlap = sorted(required & optional)
            if overlap:
                raise ContractError(
                    f"activation health {label} is both required and optional: "
                    + ", ".join(overlap)
                )
        for values, label in (
            (self.required_managed_nodes, "required managed node"),
            (self.optional_managed_nodes, "optional managed node"),
        ):
            if list(values) != sorted(values):
                raise ContractError(f"{label} declarations must be sorted")
            for entity, state in values.items():
                _require_entity(entity, label=label)
                if state not in {"unconfigured", "inactive", "active"}:
                    raise ContractError(f"{label} has an unsupported lifecycle state")
        if not self.required_systemd_units:
            raise ContractError(
                "activation health policy has no required control-plane units"
            )


@dataclass(frozen=True)
class ActivationHealthSnapshot:
    evidence_id: str
    release_id: str
    profile: str
    boot_id: str
    observed_monotonic: float
    receiver: Mapping[str, Any]
    bootstrap: Mapping[str, Any]
    daemon: Mapping[str, Any]
    runtime_api: Mapping[str, Any]
    configuration: Mapping[str, Any]
    hardware_roles: Mapping[str, Mapping[str, Any]]
    services: Mapping[str, Mapping[str, Any]]
    managed_nodes: Mapping[str, str]
    systemd_units: Mapping[str, str]
    px4: Mapping[str, Any]
    operations: Mapping[str, Any]
    schema: str = HEALTH_SCHEMA

    def as_document(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        value = self.as_document()
        if self.schema != HEALTH_SCHEMA or self.evidence_id != _identity(
            value, "evidence_id"
        ):
            raise ContractError("activation health evidence identity mismatch")
        _require_hash(self.release_id, label="activation health release identity")
        _require_entity(self.profile, label="activation health profile")
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ContractError("activation health boot identity is missing")
        if (
            isinstance(self.observed_monotonic, bool)
            or not isinstance(self.observed_monotonic, (int, float))
            or self.observed_monotonic < 0
        ):
            raise ContractError("activation health monotonic observation is invalid")
        fixed = {
            "receiver": (
                self.receiver,
                {"ready", "receiver_id", "generation"},
            ),
            "bootstrap": (
                self.bootstrap,
                {"ready", "protocol_version"},
            ),
            "daemon": (
                self.daemon,
                {"available", "fresh", "release_id", "profile"},
            ),
            "runtime API": (
                self.runtime_api,
                {"available", "fresh", "release_id", "profile", "api_version"},
            ),
            "configuration": (
                self.configuration,
                {
                    "reconciled",
                    "durable",
                    "schema_valid",
                    "checkpoint_id",
                    "schema_version",
                },
            ),
            "PX4": (
                self.px4,
                {
                    "available",
                    "fresh",
                    "interface_compatible",
                    "firmware_compatible",
                    "parameter_manifest_matches",
                    "armed",
                    "in_air",
                    "failsafe",
                    "nav_state",
                },
            ),
            "operation ownership": (
                self.operations,
                {
                    "fresh",
                    "mission_active",
                    "mission_control_owner",
                    "custom_operation_active",
                    "custom_operation_control_owner",
                    "direct_operation_active",
                    "reference_owner_active",
                },
            ),
        }
        for label, (document, fields) in fixed.items():
            if not isinstance(document, dict):
                raise ContractError(f"activation health {label} is malformed")
            _exact(document, fields, label=f"activation health {label}")
        for section, label in (
            (self.hardware_roles, "hardware role"),
            (self.services, "service"),
        ):
            if not isinstance(section, dict) or list(section) != sorted(section):
                raise ContractError(f"activation health {label} evidence is not sorted")
            for entity, evidence in section.items():
                _require_entity(entity, label=label)
                if not isinstance(evidence, dict):
                    raise ContractError(
                        f"activation health {label} evidence is malformed"
                    )
        for section, label in (
            (self.managed_nodes, "managed node"),
            (self.systemd_units, "systemd unit"),
        ):
            if not isinstance(section, dict) or list(section) != sorted(section):
                raise ContractError(f"activation health {label} evidence is not sorted")
            for entity, state in section.items():
                _require_entity(entity, label=label)
                if not isinstance(state, str) or not state:
                    raise ContractError(f"activation health {label} state is invalid")


class ActivationHealthGate:
    """Evaluate one observation against the exact signed profile declaration."""

    def __init__(
        self,
        *,
        candidate: ActivationTuple,
        policy: ActivationHealthPolicy,
        receiver_id: str,
        receiver_generation: int,
        bootstrap_protocol_version: str,
        runtime_api_version_range: str,
    ) -> None:
        policy.validate()
        candidate.validate()
        _require_hash(receiver_id, label="receiver identity")
        if receiver_generation < 1:
            raise ContractError("receiver generation is invalid")
        self.candidate = candidate
        self.policy = policy
        self.receiver_id = receiver_id
        self.receiver_generation = receiver_generation
        self.bootstrap_protocol_version = bootstrap_protocol_version
        self.runtime_api_version_range = runtime_api_version_range

    def rejection_reasons(self, snapshot: ActivationHealthSnapshot) -> list[str]:
        snapshot.validate()
        reasons: list[str] = []
        if snapshot.release_id != self.candidate.release_id:
            reasons.append("health release identity differs from the candidate")
        if snapshot.profile != self.candidate.profile:
            reasons.append("health profile differs from the candidate")
        if snapshot.receiver != {
            "ready": True,
            "receiver_id": self.receiver_id,
            "generation": self.receiver_generation,
        }:
            reasons.append("receiver identity or readiness does not match")
        if snapshot.bootstrap != {
            "ready": True,
            "protocol_version": self.bootstrap_protocol_version,
        }:
            reasons.append("stable bootstrap readiness or protocol does not match")
        for component, evidence in (
            ("daemon", snapshot.daemon),
            ("runtime API", snapshot.runtime_api),
        ):
            if evidence["available"] is not True:
                reasons.append(f"{component} is unavailable")
            if evidence["fresh"] is not True:
                reasons.append(f"{component} evidence is stale")
            if evidence["release_id"] != self.candidate.release_id:
                reasons.append(f"{component} release identity does not match")
            if evidence["profile"] != self.candidate.profile:
                reasons.append(f"{component} profile does not match")
        if snapshot.runtime_api["api_version"] != self.runtime_api_version_range:
            reasons.append("runtime API compatibility identity does not match")
        expected_configuration = {
            "checkpoint_id": self.candidate.configuration_checkpoint_id,
            "schema_version": self.candidate.configuration_schema_version,
        }
        for field, expected in expected_configuration.items():
            if snapshot.configuration[field] != expected:
                reasons.append(
                    f"configuration {field.replace('_', ' ')} does not match"
                )
        for field in ("reconciled", "durable", "schema_valid"):
            if snapshot.configuration[field] is not True:
                reasons.append(f"configuration is not {field.replace('_', ' ')}")
        self._entity_reasons(
            reasons,
            kind="hardware role",
            evidence=snapshot.hardware_roles,
            required=self.policy.required_hardware_roles,
            optional=self.policy.optional_hardware_roles,
            healthy=lambda item: item == {"state": "present", "unambiguous": True},
        )
        self._entity_reasons(
            reasons,
            kind="service",
            evidence=snapshot.services,
            required=self.policy.required_services,
            optional=self.policy.optional_services,
            healthy=lambda item: item == {"alive": True, "ready": True},
        )
        self._state_reasons(
            reasons,
            kind="managed node",
            evidence=snapshot.managed_nodes,
            required=self.policy.required_managed_nodes,
            optional=self.policy.optional_managed_nodes,
        )
        self._state_reasons(
            reasons,
            kind="systemd unit",
            evidence=snapshot.systemd_units,
            required={unit: "active" for unit in self.policy.required_systemd_units},
            optional={},
        )
        px4 = snapshot.px4
        for field in (
            "available",
            "fresh",
            "interface_compatible",
            "firmware_compatible",
            "parameter_manifest_matches",
        ):
            if px4[field] is not True:
                reasons.append(f"PX4 {field.replace('_', ' ')} is not confirmed")
        if px4["armed"] is not False:
            reasons.append("PX4 is not confirmed disarmed")
        if px4["in_air"] is not False:
            reasons.append("PX4 is not confirmed landed")
        if px4["failsafe"] is not False:
            reasons.append("PX4 failsafe is not confirmed clear")
        if str(px4["nav_state"] or "").lower() not in {"manual", "position", "hold"}:
            reasons.append("PX4 navigation state is not maintenance-safe")
        operations = snapshot.operations
        if operations["fresh"] is not True:
            reasons.append("operation ownership evidence is stale")
        for field, label in (
            ("mission_active", "Mission Execution"),
            ("mission_control_owner", "Mission Control Owner"),
            ("custom_operation_active", "Custom Operation"),
            ("custom_operation_control_owner", "Custom Operation Control Owner"),
            ("direct_operation_active", "Direct Operation"),
            ("reference_owner_active", "Reference Owner"),
        ):
            if operations[field] is not False:
                reasons.append(f"{label} is not confirmed inactive")
        return reasons

    @staticmethod
    def _entity_reasons(
        reasons: list[str],
        *,
        kind: str,
        evidence: Mapping[str, Mapping[str, Any]],
        required: tuple[str, ...],
        optional: tuple[str, ...],
        healthy: Callable[[Mapping[str, Any]], bool],
    ) -> None:
        declared = set(required) | set(optional)
        undeclared = sorted(set(evidence) - declared)
        if undeclared:
            reasons.append(f"observed undeclared {kind}s: " + ", ".join(undeclared))
        for entity in required:
            if entity not in evidence:
                reasons.append(f"required {kind} is absent: {entity}")
            elif not healthy(evidence[entity]):
                reasons.append(f"required {kind} is unhealthy: {entity}")
        for entity in optional:
            if entity in evidence and not healthy(evidence[entity]):
                reasons.append(f"present optional {kind} is unhealthy: {entity}")

    @staticmethod
    def _state_reasons(
        reasons: list[str],
        *,
        kind: str,
        evidence: Mapping[str, str],
        required: Mapping[str, str],
        optional: Mapping[str, str],
    ) -> None:
        declared = set(required) | set(optional)
        undeclared = sorted(set(evidence) - declared)
        if undeclared:
            reasons.append(f"observed undeclared {kind}s: " + ", ".join(undeclared))
        for entity, desired in required.items():
            observed = evidence.get(entity)
            if observed is None:
                reasons.append(f"required {kind} is absent: {entity}")
            elif observed != desired:
                reasons.append(
                    f"required {kind} {entity} is {observed}, expected {desired}"
                )
        for entity, desired in optional.items():
            observed = evidence.get(entity)
            if observed is not None and observed != desired:
                reasons.append(
                    f"present optional {kind} {entity} is {observed}, expected {desired}"
                )


@dataclass(frozen=True)
class ControlPlaneProof:
    release_id: str
    profile: str
    started_units: tuple[str, ...]
    autonomy_started: bool
    proof_id: str
    schema: str = CONTROL_PROOF_SCHEMA

    def as_document(self) -> dict[str, Any]:
        value = asdict(self)
        value["started_units"] = list(self.started_units)
        return value

    def validate(self, *, expected: ActivationTuple) -> None:
        value = self.as_document()
        if self.schema != CONTROL_PROOF_SCHEMA or self.proof_id != _identity(
            value, "proof_id"
        ):
            raise ContractError("activation control-plane proof identity mismatch")
        if self.release_id != expected.release_id or self.profile != expected.profile:
            raise ContractError(
                "activation control-plane proof targets another selector"
            )
        if self.autonomy_started:
            raise ContractError(
                "activation control-plane start attempted to start autonomy"
            )
        if (
            not self.started_units
            or tuple(sorted(set(self.started_units))) != self.started_units
        ):
            raise ContractError("activation control-plane unit proof is incomplete")


class HealthProvider(Protocol):
    def __call__(
        self, candidate: ActivationTuple, policy: ActivationHealthPolicy
    ) -> ActivationHealthSnapshot: ...


class ActivationDiagnosticStore:
    """Retain immutable observations plus one durable transaction summary."""

    def __init__(self, root: Path):
        self.root = root

    def state_path(self, operation_id: str) -> Path:
        _require_operation(operation_id)
        return self.root / "transactions" / f"{operation_id}.json"

    def evidence_path(self, operation_id: str, evidence_id: str) -> Path:
        _require_operation(operation_id)
        _require_hash(evidence_id, label="activation evidence identity")
        return self.root / "evidence" / operation_id / f"{evidence_id}.json"

    def load_state(self, operation_id: str) -> dict[str, Any] | None:
        path = self.state_path(operation_id)
        if not path.exists() and not path.is_symlink():
            return None
        value = _canonical_document(path, label="activation health transaction")
        self._validate_state(value)
        return value

    def list_states(self) -> list[dict[str, Any]]:
        directory = self.root / "transactions"
        if not directory.exists() and not directory.is_symlink():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ContractError("activation transaction directory is linked or invalid")
        values: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ContractError(
                    "activation transaction directory has an unsafe entry"
                )
            value = self.load_state(path.stem)
            assert value is not None
            values.append(value)
        return values

    def write_state(
        self,
        *,
        operation_id: str,
        previous: ActivationTuple,
        candidate: ActivationTuple,
        authorization: ActivationAuthorization,
        safety_observation_id: str,
        safety_snapshot: Mapping[str, Any],
        stage: str,
        boot_id: str,
        monotonic: float,
        evidence_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        rollback: Mapping[str, Any] | None = None,
        accepted_state_id: str | None = None,
    ) -> dict[str, Any]:
        _require_operation(operation_id)
        sequence = 1
        history: list[dict[str, Any]] = []
        existing = self.load_state(operation_id)
        if existing is not None:
            bindings = (
                existing["previous"] == asdict(previous)
                and existing["candidate"] == asdict(candidate)
                and existing["authorization"] == asdict(authorization)
                and existing["safety_observation_id"] == safety_observation_id
                and existing["safety_snapshot"] == dict(safety_snapshot)
            )
            if not bindings:
                raise ContractError("activation health transaction binding changed")
            sequence = int(existing["sequence"]) + 1
            history = list(existing["history"])
        history.append(
            {
                "sequence": sequence,
                "stage": stage,
                "boot_id": boot_id,
                "monotonic": monotonic,
                "evidence_id": evidence_id,
            }
        )
        value: dict[str, Any] = {
            "schema": STATE_SCHEMA,
            "state_id": "0" * 64,
            "operation_id": operation_id,
            "previous": asdict(previous),
            "candidate": asdict(candidate),
            "authorization": asdict(authorization),
            "safety_observation_id": safety_observation_id,
            "safety_snapshot": dict(safety_snapshot),
            "stage": stage,
            "sequence": sequence,
            "history": history,
            "evidence_id": evidence_id,
            "failure": None if failure is None else dict(failure),
            "rollback": None if rollback is None else dict(rollback),
            "accepted_state_id": accepted_state_id,
            "autonomy_started": False,
            "automatic_rollback_permitted": stage in PRE_ACCEPTANCE_STAGES,
        }
        value["state_id"] = _identity(value, "state_id")
        self._validate_state(value)
        atomic_document(self.state_path(operation_id), value, mode=0o640)
        return value

    def retain_snapshot(
        self, operation_id: str, snapshot: ActivationHealthSnapshot
    ) -> str:
        snapshot.validate()
        path = self.evidence_path(operation_id, snapshot.evidence_id)
        if path.exists() or path.is_symlink():
            observed = _canonical_document(path, label="activation health evidence")
            if observed != snapshot.as_document():
                raise ContractError("activation health evidence identity collision")
            return snapshot.evidence_id
        atomic_document(path, snapshot.as_document(), mode=0o440)
        return snapshot.evidence_id

    @staticmethod
    def _validate_state(value: Mapping[str, Any]) -> None:
        _exact(
            value,
            {
                "schema",
                "state_id",
                "operation_id",
                "previous",
                "candidate",
                "authorization",
                "safety_observation_id",
                "safety_snapshot",
                "stage",
                "sequence",
                "history",
                "evidence_id",
                "failure",
                "rollback",
                "accepted_state_id",
                "autonomy_started",
                "automatic_rollback_permitted",
            },
            label="activation health transaction",
        )
        if value["schema"] != STATE_SCHEMA or value["state_id"] != _identity(
            value, "state_id"
        ):
            raise ContractError("activation health transaction identity mismatch")
        _require_operation(value["operation_id"])
        ActivationTuple(**value["previous"]).validate()
        ActivationTuple(**value["candidate"]).validate()
        authorization = ActivationAuthorization(**value["authorization"])
        authorization_value = asdict(authorization)
        if authorization.authorization_id != content_identity(
            {
                key: item
                for key, item in authorization_value.items()
                if key != "authorization_id"
            }
        ):
            raise ContractError("activation authorization identity mismatch")
        _require_hash(value["safety_observation_id"], label="safety observation")
        safety = ActivationSafetySnapshot(**value["safety_snapshot"])
        safety.validate()
        if safety.observation_id != value["safety_observation_id"]:
            raise ContractError("activation safety snapshot binding mismatch")
        if value["stage"] not in PRE_ACCEPTANCE_STAGES | TERMINAL_STAGES:
            raise ContractError("activation health transaction stage is invalid")
        if value["autonomy_started"] is not False:
            raise ContractError("activation health transaction claims autonomy startup")
        if value["automatic_rollback_permitted"] != (
            value["stage"] in PRE_ACCEPTANCE_STAGES
        ):
            raise ContractError("activation automatic rollback authority is malformed")
        if value["stage"] == "accepted" and value["accepted_state_id"] is None:
            raise ContractError("accepted activation lacks release-state identity")
        if value["evidence_id"] is not None:
            _require_hash(value["evidence_id"], label="activation evidence")
        if not isinstance(value["sequence"], int) or value["sequence"] < 1:
            raise ContractError("activation transaction sequence is invalid")
        if len(value["history"]) != value["sequence"]:
            raise ContractError("activation transaction history is incomplete")


class ActivationCoordinator:
    """Execute one activation to durable acceptance or local rollback."""

    def __init__(
        self,
        *,
        release_store: ReleaseStore,
        transaction_store: ActivationTransactionStore,
        diagnostics: ActivationDiagnosticStore,
        safety_provider: Callable[[], ActivationSafetySnapshot],
        health_provider: HealthProvider,
        stop_all_units: Callable[[], tuple[str, ...]],
        start_control_plane: Callable[[ActivationTuple], ControlPlaneProof],
        receiver_id: str,
        receiver_generation: int,
        bootstrap_protocol_version: str,
        logical_target: str,
        profile: str,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        boot_id: Callable[[], str],
        poll_interval_s: float = 0.25,
    ) -> None:
        if poll_interval_s <= 0 or poll_interval_s > STABLE_WINDOW_S:
            raise ContractError("activation health polling interval is invalid")
        self.release_store = release_store
        self.transaction_store = transaction_store
        self.diagnostics = diagnostics
        self.safety_provider = safety_provider
        self.health_provider = health_provider
        self.stop_all_units = stop_all_units
        self.start_control_plane = start_control_plane
        self.receiver_id = receiver_id
        self.receiver_generation = receiver_generation
        self.bootstrap_protocol_version = bootstrap_protocol_version
        self.logical_target = logical_target
        self.profile = profile
        self.monotonic = monotonic
        self.sleep = sleep
        self.boot_id = boot_id
        self.poll_interval_s = poll_interval_s

    def _manifest(self, release_id: str) -> dict[str, Any]:
        _require_hash(release_id, label="activation release identity")
        return _canonical_document(
            self.release_store.releases_root / release_id / "release-manifest.json",
            label="staged release manifest",
        )

    def _candidate(
        self, *, release_id: str, configuration_checkpoint_id: str
    ) -> tuple[ActivationTuple, ActivationHealthPolicy, str]:
        manifest = self._manifest(release_id)
        profile = next(
            (
                item
                for item in manifest.get("profiles", [])
                if item.get("id") == self.profile
            ),
            None,
        )
        if profile is None or not profile.get("bootable"):
            raise ContractError("activation profile is absent or not bootable")
        candidate = ActivationTuple(
            release_id=release_id,
            release_path=str(self.release_store.releases_root / release_id),
            configuration_checkpoint_id=configuration_checkpoint_id,
            configuration_checkpoint_path=str(
                self.transaction_store.checkpoint_root / configuration_checkpoint_id
            ),
            configuration_schema_version=int(
                manifest["configuration"]["schema_version"]
            ),
            mission_catalog_hash=manifest["mission_catalog"]["catalog_hash"],
            profile=self.profile,
        )
        candidate.validate()
        policy = ActivationHealthPolicy.from_profile(profile)
        runtime_range = manifest["compatibility"]["api_ranges"]["runtime_api"]
        if not isinstance(runtime_range, str) or not runtime_range:
            raise ContractError("release lacks a runtime API compatibility range")
        return candidate, policy, runtime_range

    def validate_px4_evidence(
        self, *, release_id: str, evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        validate_px4_activation_evidence(evidence)
        manifest = self._manifest(release_id)
        profile = next(
            (
                item
                for item in manifest.get("profiles", [])
                if item.get("id") == self.profile
            ),
            None,
        )
        parameter_profile = profile and profile.get("parameter_profile")
        expected_manifest = (
            manifest.get("px4", {}).get("manifest_ids", {}).get(parameter_profile)
        )
        comparison = evidence["comparison"]
        snapshot = evidence["snapshot"]
        if (
            evidence["release_id"] != release_id
            or parameter_profile not in {"real", "sim"}
            or evidence["profile"] != parameter_profile
            or evidence["manifest_id"] != expected_manifest
            or comparison["inventory_complete"] is not True
            or comparison["required_match"] is not True
            or comparison["missing"]
            or comparison["unexpected"]
            or comparison["drift"]["release-required"]
            or evidence["healthy"] is not True
            or evidence["writes_performed"] != 0
            or snapshot["target"]["armed"] is not False
        ):
            raise ContractError(
                "PX4 activation evidence does not satisfy the staged release"
            )
        return dict(evidence)

    @staticmethod
    def _with_px4_evidence(
        snapshot: ActivationHealthSnapshot, evidence: Mapping[str, Any]
    ) -> ActivationHealthSnapshot:
        px4 = dict(snapshot.px4)
        px4["interface_compatible"] = (
            px4.get("available") is True and px4.get("fresh") is True
        )
        px4["firmware_compatible"] = True
        px4["parameter_manifest_matches"] = True
        value = asdict(replace(snapshot, px4=px4, evidence_id="0" * 64))
        value["evidence_id"] = _identity(value, "evidence_id")
        result = ActivationHealthSnapshot(**value)
        result.validate()
        return result

    def activate(
        self,
        *,
        operation_id: str,
        release_id: str,
        configuration_checkpoint_id: str,
        explicit_qualified_action: bool,
        px4_activation_evidence: Mapping[str, Any],
        status_index: Mapping[str, Any] | None = None,
        maintenance_override: MaintenanceOverride | None = None,
        operator_rollback: bool = False,
    ) -> dict[str, Any]:
        _require_operation(operation_id)
        candidate, policy, runtime_range = self._candidate(
            release_id=release_id,
            configuration_checkpoint_id=configuration_checkpoint_id,
        )
        px4_evidence = self.validate_px4_evidence(
            release_id=release_id, evidence=px4_activation_evidence
        )
        previous = self.transaction_store.current()
        if previous is None:
            raise ContractError(
                "field activation requires a known previous composite selector"
            )
        safety = self.safety_provider()
        ActivationSafetyGate(
            logical_target=self.logical_target, profile=self.profile
        ).authorize(
            safety,
            maintenance_override=maintenance_override,
            operation_id=operation_id,
            release_id=release_id,
        )
        authorization = (
            self.release_store.authorize_rollback(release_id, status_index=status_index)
            if operator_rollback
            else self.release_store.authorize_activation(
                release_id, status_index=status_index
            )
        )
        started = self.monotonic()
        self._state(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            authorization=authorization,
            safety=safety,
            stage="prepared",
        )
        accepted_state: dict[str, Any] | None = None
        last_snapshot: ActivationHealthSnapshot | None = None
        last_reasons: list[str] = []
        try:
            self.transaction_store.switch(
                candidate,
                operation_id=operation_id,
                stop_all_units=self.stop_all_units,
            )
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="selector-switched",
            )
            proof = self.start_control_plane(candidate)
            proof.validate(expected=candidate)
            missing_units = sorted(
                set(policy.required_systemd_units) - set(proof.started_units)
            )
            if missing_units:
                raise ContractError(
                    "activation did not start required control-plane units: "
                    + ", ".join(missing_units)
                )
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="control-plane-started",
            )
            gate = ActivationHealthGate(
                candidate=candidate,
                policy=policy,
                receiver_id=self.receiver_id,
                receiver_generation=self.receiver_generation,
                bootstrap_protocol_version=self.bootstrap_protocol_version,
                runtime_api_version_range=runtime_range,
            )
            stable_since: float | None = None
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="health-observing",
            )
            while True:
                now = self.monotonic()
                if now - started >= HARD_DEADLINE_S:
                    detail = "; ".join(last_reasons) or "no valid health observation"
                    raise ContractError(
                        "activation health deadline expired before a ten-second stable window: "
                        + detail
                    )
                snapshot = self._with_px4_evidence(
                    self.health_provider(candidate, policy), px4_evidence
                )
                last_snapshot = snapshot
                last_reasons = gate.rejection_reasons(snapshot)
                if last_reasons:
                    stable_since = None
                elif stable_since is None:
                    stable_since = now
                elif now - stable_since >= STABLE_WINDOW_S:
                    break
                self.sleep(min(self.poll_interval_s, HARD_DEADLINE_S - (now - started)))
            assert last_snapshot is not None
            evidence_id = self.diagnostics.retain_snapshot(operation_id, last_snapshot)
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="acceptance-evidence-persisted",
                evidence_id=evidence_id,
            )
            accepted_state = (
                self.release_store.record_rollback_acceptance(authorization)
                if operator_rollback
                else self.release_store.record_acceptance(
                    authorization,
                    explicit_qualified_action=explicit_qualified_action,
                )
            )
            transaction = self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="accepted",
                evidence_id=evidence_id,
                accepted_state_id=accepted_state["state_id"],
            )
            return {
                "kind": "rollback" if operator_rollback else "activation",
                "release_id": release_id,
                "previous_release_id": previous.release_id,
                "accepted_state_id": accepted_state["state_id"],
                "acceptance_evidence_id": evidence_id,
                "activation_state_id": transaction["state_id"],
                "stable_window_s": STABLE_WINDOW_S,
                "elapsed_s": self.monotonic() - started,
                "automatic_rollback_permitted": False,
                "autonomy_started": False,
                "px4_activation_evidence_id": px4_evidence["evidence_id"],
                "px4_snapshot_id": px4_evidence["snapshot"]["snapshot_id"],
                "px4_manifest_id": px4_evidence["manifest_id"],
            }
        except Exception as exc:
            state = self.release_store.state()
            if state.get("active_release_id") == candidate.release_id:
                evidence_id = (
                    self.diagnostics.retain_snapshot(operation_id, last_snapshot)
                    if last_snapshot is not None
                    else None
                )
                self._state(
                    operation_id=operation_id,
                    previous=previous,
                    candidate=candidate,
                    authorization=authorization,
                    safety=safety,
                    stage="faulted",
                    evidence_id=evidence_id,
                    failure={"code": "post-acceptance-fault", "message": str(exc)},
                    accepted_state_id=state["state_id"],
                )
                raise ContractError(
                    "activation fault occurred after durable acceptance; automatic rollback is forbidden: "
                    + str(exc)
                ) from exc
            rollback = self._rollback(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                failure=exc,
                snapshot=last_snapshot,
                started=self.monotonic(),
            )
            raise ContractError(
                f"activation failed and restored {previous.release_id}: {exc}; "
                f"rollback={rollback['outcome']}"
            ) from exc

    def preflight(
        self,
        *,
        release_id: str,
        configuration_checkpoint_id: str,
        px4_activation_evidence: Mapping[str, Any],
        operator_rollback: bool = False,
    ) -> dict[str, Any]:
        """Read-only activation inspection used before durable request acceptance."""

        candidate, policy, runtime_range = self._candidate(
            release_id=release_id,
            configuration_checkpoint_id=configuration_checkpoint_id,
        )
        px4_evidence = self.validate_px4_evidence(
            release_id=release_id, evidence=px4_activation_evidence
        )
        previous = self.transaction_store.current()
        release_state = self.release_store.state()
        reasons: list[str] = []
        if previous is None:
            reasons.append("no previous composite selector is available for rollback")
        expected_role = (
            "rollback_release_id" if operator_rollback else "candidate_release_id"
        )
        if release_state.get(expected_role) != release_id:
            reasons.append(
                "release is not the retained rollback target"
                if operator_rollback
                else "release is not the staged candidate"
            )
        safety = self.safety_provider()
        reasons.extend(
            ActivationSafetyGate(
                logical_target=self.logical_target, profile=self.profile
            ).rejection_reasons(safety)
        )
        return {
            "schema": "iii.activation-preflight/v1",
            "release_id": release_id,
            "configuration_checkpoint_id": configuration_checkpoint_id,
            "previous_release_id": (
                previous.release_id if previous is not None else None
            ),
            "profile": candidate.profile,
            "runtime_api_version_range": runtime_range,
            "health_policy_id": content_identity(policy.as_document()),
            "safety_observation_id": safety.observation_id,
            "ready": not reasons,
            "rejection_reasons": reasons,
            "autonomy_started": False,
            "px4_activation_evidence_id": px4_evidence["evidence_id"],
        }

    def operator_rollback(
        self,
        *,
        operation_id: str,
        release_id: str,
        configuration_checkpoint_id: str,
        px4_activation_evidence: Mapping[str, Any],
        status_index: Mapping[str, Any] | None = None,
        maintenance_override: MaintenanceOverride | None = None,
    ) -> dict[str, Any]:
        """Run explicit rollback through the same safety and health transaction."""

        return self.activate(
            operation_id=operation_id,
            release_id=release_id,
            configuration_checkpoint_id=configuration_checkpoint_id,
            explicit_qualified_action=False,
            px4_activation_evidence=px4_activation_evidence,
            status_index=status_index,
            maintenance_override=maintenance_override,
            operator_rollback=True,
        )

    def _rollback(
        self,
        *,
        operation_id: str,
        previous: ActivationTuple,
        candidate: ActivationTuple,
        authorization: ActivationAuthorization,
        safety: ActivationSafetySnapshot,
        failure: Exception,
        snapshot: ActivationHealthSnapshot | None,
        started: float,
    ) -> dict[str, Any]:
        evidence_id = (
            self.diagnostics.retain_snapshot(operation_id, snapshot)
            if snapshot is not None
            else None
        )
        self._state(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            authorization=authorization,
            safety=safety,
            stage="rollback-prepared",
            evidence_id=evidence_id,
            failure={"code": "health-rejected", "message": str(failure)},
        )
        rollback_started = self.monotonic()
        try:
            stopped = self.stop_all_units()
            if "iii.target" not in stopped:
                raise ContractError("rollback did not prove all III units stopped")
            transaction_path = (
                self.transaction_store.transaction_root / f"{operation_id}.json"
            )
            if transaction_path.exists() or transaction_path.is_symlink():
                self.transaction_store.rollback(operation_id=operation_id)
            elif self.transaction_store.current() != previous:
                raise ContractError(
                    "activation transaction vanished while the selector differs from previous"
                )
            proof = self.start_control_plane(previous)
            proof.validate(expected=previous)
            elapsed = self.monotonic() - rollback_started
            rollback = {
                "outcome": "restored",
                "release_id": previous.release_id,
                "elapsed_s": elapsed,
                "target_s": ROLLBACK_TARGET_S,
                "target_met": elapsed <= ROLLBACK_TARGET_S,
                "proof_id": proof.proof_id,
                "autonomy_started": False,
            }
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="rolled-back",
                evidence_id=evidence_id,
                failure={"code": "health-rejected", "message": str(failure)},
                rollback=rollback,
            )
            return rollback
        except Exception as rollback_error:
            rollback = {
                "outcome": "faulted",
                "release_id": previous.release_id,
                "elapsed_s": self.monotonic() - rollback_started,
                "target_s": ROLLBACK_TARGET_S,
                "target_met": False,
                "error": str(rollback_error),
                "autonomy_started": False,
            }
            self._state(
                operation_id=operation_id,
                previous=previous,
                candidate=candidate,
                authorization=authorization,
                safety=safety,
                stage="faulted",
                evidence_id=evidence_id,
                failure={"code": "rollback-failed", "message": str(failure)},
                rollback=rollback,
            )
            raise ContractError(
                "activation rollback entered a visible non-operational fault: "
                + str(rollback_error)
            ) from rollback_error

    def reconcile(self) -> dict[str, Any]:
        """Fail back interrupted activations; never resume candidate startup."""

        restored: list[str] = []
        accepted: list[str] = []
        faulted: list[str] = []
        for state in self.diagnostics.list_states():
            if state["stage"] in TERMINAL_STAGES:
                continue
            operation_id = state["operation_id"]
            previous = ActivationTuple(**state["previous"])
            candidate = ActivationTuple(**state["candidate"])
            authorization = ActivationAuthorization(**state["authorization"])
            safety = ActivationSafetySnapshot(**state["safety_snapshot"])
            release_state = self.release_store.state()
            if release_state.get("active_release_id") == candidate.release_id:
                self._state(
                    operation_id=operation_id,
                    previous=previous,
                    candidate=candidate,
                    authorization=authorization,
                    safety=safety,
                    stage="accepted",
                    evidence_id=state["evidence_id"],
                    accepted_state_id=release_state["state_id"],
                )
                accepted.append(operation_id)
                continue
            try:
                self._rollback(
                    operation_id=operation_id,
                    previous=previous,
                    candidate=candidate,
                    authorization=authorization,
                    safety=safety,
                    failure=ContractError(
                        "power-loss reconciliation before acceptance"
                    ),
                    snapshot=None,
                    started=self.monotonic(),
                )
                restored.append(operation_id)
            except ContractError:
                faulted.append(operation_id)
        return {
            "schema": "iii.activation-reconciliation/v1",
            "restored_operations": restored,
            "accepted_operations": accepted,
            "faulted_operations": faulted,
            "autonomy_started": False,
        }

    def handle_post_acceptance_failure(
        self,
        *,
        operation_id: str,
        maximum_restart_attempts: int = 2,
    ) -> dict[str, Any]:
        """Bound restart after acceptance without ever changing selectors."""

        if maximum_restart_attempts < 1 or maximum_restart_attempts > 3:
            raise ContractError("post-acceptance restart bound is invalid")
        state = self.diagnostics.load_state(operation_id)
        if state is None or state["stage"] != "accepted":
            raise ContractError(
                "post-acceptance recovery requires an accepted activation"
            )
        candidate = ActivationTuple(**state["candidate"])
        previous = ActivationTuple(**state["previous"])
        authorization = ActivationAuthorization(**state["authorization"])
        safety = ActivationSafetySnapshot(**state["safety_snapshot"])
        observed_selector = self.transaction_store.current()
        if observed_selector != candidate:
            raise ContractError(
                "accepted selector changed outside the activation transaction"
            )
        errors: list[str] = []
        for attempt in range(1, maximum_restart_attempts + 1):
            try:
                proof = self.start_control_plane(candidate)
                proof.validate(expected=candidate)
                retained = self._state(
                    operation_id=operation_id,
                    previous=previous,
                    candidate=candidate,
                    authorization=authorization,
                    safety=safety,
                    stage="accepted",
                    evidence_id=state["evidence_id"],
                    failure={
                        "code": "bounded-restart-recovered",
                        "message": f"control plane recovered on attempt {attempt}",
                    },
                    accepted_state_id=state["accepted_state_id"],
                )
                return {
                    "outcome": "recovered",
                    "attempts": attempt,
                    "proof_id": proof.proof_id,
                    "selector_changed": False,
                    "automatic_rollback_permitted": False,
                    "state_id": retained["state_id"],
                }
            except Exception as exc:
                errors.append(str(exc))
        retained = self._state(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            authorization=authorization,
            safety=safety,
            stage="faulted",
            evidence_id=state["evidence_id"],
            failure={
                "code": "bounded-restart-exhausted",
                "message": "; ".join(errors),
            },
            accepted_state_id=state["accepted_state_id"],
        )
        return {
            "outcome": "faulted",
            "attempts": maximum_restart_attempts,
            "selector_changed": False,
            "automatic_rollback_permitted": False,
            "state_id": retained["state_id"],
        }

    def _state(
        self,
        *,
        operation_id: str,
        previous: ActivationTuple,
        candidate: ActivationTuple,
        authorization: ActivationAuthorization,
        safety: ActivationSafetySnapshot,
        stage: str,
        evidence_id: str | None = None,
        failure: Mapping[str, Any] | None = None,
        rollback: Mapping[str, Any] | None = None,
        accepted_state_id: str | None = None,
    ) -> dict[str, Any]:
        return self.diagnostics.write_state(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            authorization=authorization,
            safety_observation_id=safety.observation_id,
            safety_snapshot=safety.as_document(),
            stage=stage,
            boot_id=self.boot_id(),
            monotonic=self.monotonic(),
            evidence_id=evidence_id,
            failure=failure,
            rollback=rollback,
            accepted_state_id=accepted_state_id,
        )
