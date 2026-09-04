"""Safety authorization and atomic release/configuration/catalog selection.

The deployment receiver is the only production caller of this module.  The
module deliberately stops at selector mutation: starting control-plane units,
evaluating health, accepting a candidate, and automatic rollback belong to the
receiver activation engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping, TextIO

from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import OPERATION_ID
from iii_deployment.receiver.state import atomic_document

SAFETY_SCHEMA = "iii.activation-safety/v1"
SELECTOR_SCHEMA = "iii.activation-selector/v1"
TRANSACTION_SCHEMA = "iii.activation-transaction/v1"
OVERRIDE_SCHEMA = "iii.maintenance-override/v1"
HASH = re.compile(r"^(?:sha256:)?[a-f0-9]{64}$")
LOGICAL_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
SAFE_NAV_STATES = frozenset({"manual", "position", "hold"})
MINIMUM_SAFE_OBSERVATION_S = 3.0
STOP_TARGET = "iii.target"


def _identity(value: Mapping[str, Any], field: str) -> str:
    return content_identity({key: item for key, item in value.items() if key != field})


def _require_logical(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not LOGICAL_ID.fullmatch(value):
        raise ContractError(f"invalid {label}")


def _require_hash(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ContractError(f"invalid {label}")


def _require_operation(value: str) -> None:
    if not isinstance(value, str) or not OPERATION_ID.fullmatch(value):
        raise ContractError("invalid activation operation identity")


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_stopped_units(units: tuple[str, ...]) -> None:
    if (
        STOP_TARGET not in units
        or len(units) != len(set(units))
        or any(
            not re.fullmatch(r"iii(?:-[a-z0-9@_.-]+)?\.(?:service|target)", unit)
            for unit in units
        )
    ):
        raise ContractError("all-III-unit stop proof is incomplete or malformed")


def _atomic_symlink(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.parent / f".{path.name}.partial-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise ContractError(
            f"stale selector partial requires reconciliation: {temporary.name}"
        )
    os.symlink(target, temporary)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


@dataclass(frozen=True)
class ActivationSafetySnapshot:
    """One identity-bound observation supplied by the canonical runtime API."""

    logical_target: str
    profile: str
    observation_id: str
    runtime_api_available: bool
    runtime_identity_matches: bool
    runtime_fresh: bool
    px4_available: bool
    px4_fresh: bool
    armed: bool | None
    in_air: bool | None
    nav_state: str | None
    failsafe: bool | None
    mission_fresh: bool
    mission_active: bool | None
    mission_control_owner: bool | None
    operation_fresh: bool
    custom_operation_active: bool | None
    custom_operation_control_owner: bool | None
    direct_operation_active: bool | None
    reference_owner_active: bool | None
    configuration_migration_ready: bool
    configuration_checkpoint_id: str | None
    continuously_safe_for_s: float
    schema: str = SAFETY_SCHEMA

    def as_document(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if self.schema != SAFETY_SCHEMA:
            raise ContractError("activation safety observation schema is unsupported")
        _require_logical(self.logical_target, label="activation logical target")
        _require_logical(self.profile, label="activation profile")
        _require_hash(self.observation_id, label="activation observation identity")
        if self.observation_id != _identity(self.as_document(), "observation_id"):
            raise ContractError("activation safety observation identity mismatch")
        if self.configuration_checkpoint_id is not None:
            _require_hash(
                self.configuration_checkpoint_id,
                label="configuration checkpoint identity",
            )
        if (
            isinstance(self.continuously_safe_for_s, bool)
            or self.continuously_safe_for_s < 0
        ):
            raise ContractError("activation safety observation duration is invalid")


@dataclass(frozen=True)
class MaintenanceOverride:
    schema: str
    override_id: str
    operation_id: str
    actor_id: str
    release_id: str
    logical_target: str
    profile: str
    observation_id: str
    stopped_units: tuple[str, ...]
    confirmation: str

    def validate(self) -> None:
        value = asdict(self)
        value["stopped_units"] = list(self.stopped_units)
        if self.schema != OVERRIDE_SCHEMA or self.override_id != _identity(
            value, "override_id"
        ):
            raise ContractError("maintenance override identity mismatch")
        _require_operation(self.operation_id)
        for field in ("actor_id", "release_id", "observation_id"):
            _require_hash(getattr(self, field), label=f"maintenance override {field}")
        _require_logical(self.logical_target, label="maintenance override target")
        _require_logical(self.profile, label="maintenance override profile")
        if not self.stopped_units or any(not value for value in self.stopped_units):
            raise ContractError("maintenance override has no stopped III units")


@dataclass(frozen=True)
class ActivationTuple:
    release_id: str
    release_path: str
    configuration_checkpoint_id: str
    configuration_checkpoint_path: str
    configuration_schema_version: int
    mission_catalog_hash: str
    profile: str

    def validate(self) -> None:
        _require_hash(self.release_id, label="activation release identity")
        _require_hash(
            self.configuration_checkpoint_id,
            label="activation configuration checkpoint identity",
        )
        _require_hash(
            self.mission_catalog_hash, label="activation mission catalog identity"
        )
        _require_logical(self.profile, label="activation profile")
        if self.configuration_schema_version < 1:
            raise ContractError("activation configuration schema version is invalid")
        for value, label in (
            (self.release_path, "release"),
            (self.configuration_checkpoint_path, "configuration checkpoint"),
        ):
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ContractError(
                    f"activation {label} path is not an absolute fixed path"
                )


class ActivationSafetyGate:
    """Evaluate the settled real-aircraft activation policy without side effects."""

    def __init__(self, *, logical_target: str, profile: str):
        self.logical_target = logical_target
        self.profile = profile

    def rejection_reasons(self, snapshot: ActivationSafetySnapshot) -> list[str]:
        snapshot.validate()
        reasons: list[str] = []
        if snapshot.logical_target != self.logical_target:
            reasons.append("runtime logical target differs from the deployment target")
        if snapshot.profile != self.profile:
            reasons.append("runtime profile differs from the deployment profile")
        if not snapshot.runtime_api_available:
            reasons.append("runtime API is unavailable")
        if not snapshot.runtime_identity_matches:
            reasons.append("runtime target identity is untrusted")
        if not snapshot.runtime_fresh:
            reasons.append("runtime safety state is stale")
        if not snapshot.px4_available:
            reasons.append("PX4 safety telemetry is unavailable")
        if not snapshot.px4_fresh:
            reasons.append("PX4 safety telemetry is stale")
        if snapshot.armed is not False:
            reasons.append("vehicle is not confirmed disarmed")
        if snapshot.in_air is not False:
            reasons.append("vehicle is not confirmed landed")
        if snapshot.failsafe is not False:
            reasons.append("PX4 failsafe state is not confirmed clear")
        if (snapshot.nav_state or "").lower() not in SAFE_NAV_STATES:
            reasons.append("PX4 navigation state is not maintenance-safe")
        if not snapshot.mission_fresh:
            reasons.append("Mission Execution state is stale")
        if snapshot.mission_active is not False:
            reasons.append("Mission Execution is not confirmed inactive")
        if snapshot.mission_control_owner is not False:
            reasons.append("Mission Execution control ownership is not confirmed clear")
        if not snapshot.operation_fresh:
            reasons.append("Custom Operation state is stale")
        if snapshot.custom_operation_active is not False:
            reasons.append("Custom Operation is not confirmed inactive")
        if snapshot.custom_operation_control_owner is not False:
            reasons.append("Custom Operation control ownership is not confirmed clear")
        if snapshot.direct_operation_active is not False:
            reasons.append("Direct Operation is not confirmed inactive")
        if snapshot.reference_owner_active is not False:
            reasons.append("active Reference Owner is not confirmed clear")
        if (
            not snapshot.configuration_migration_ready
            or snapshot.configuration_checkpoint_id is None
        ):
            reasons.append("configuration migration checkpoint is not ready")
        if snapshot.continuously_safe_for_s < MINIMUM_SAFE_OBSERVATION_S:
            reasons.append(
                "maintenance-safe state was not continuous for three seconds"
            )
        return reasons

    def authorize(
        self,
        snapshot: ActivationSafetySnapshot,
        *,
        maintenance_override: MaintenanceOverride | None = None,
        operation_id: str,
        release_id: str,
    ) -> None:
        reasons = self.rejection_reasons(snapshot)
        if not reasons:
            if maintenance_override is not None:
                raise ContractError(
                    "maintenance override is forbidden when normal safety authorization succeeds"
                )
            return
        if maintenance_override is None:
            raise ContractError(
                "activation safety gate rejected: " + "; ".join(reasons)
            )
        maintenance_override.validate()
        expected = {
            "operation_id": operation_id,
            "release_id": release_id,
            "logical_target": self.logical_target,
            "profile": self.profile,
            "observation_id": snapshot.observation_id,
        }
        for field, value in expected.items():
            if getattr(maintenance_override, field) != value:
                raise ContractError(f"maintenance override {field} binding mismatch")
        known_unsafe = {
            "armed": snapshot.armed,
            "in_air": snapshot.in_air,
            "mission_active": snapshot.mission_active,
            "mission_control_owner": snapshot.mission_control_owner,
            "custom_operation_active": snapshot.custom_operation_active,
            "custom_operation_control_owner": snapshot.custom_operation_control_owner,
            "direct_operation_active": snapshot.direct_operation_active,
            "reference_owner_active": snapshot.reference_owner_active,
        }
        active = sorted(name for name, value in known_unsafe.items() if value is True)
        if active:
            raise ContractError(
                "maintenance override cannot waive known active safety evidence: "
                + ", ".join(active)
            )
        if (
            not snapshot.configuration_migration_ready
            or snapshot.configuration_checkpoint_id is None
        ):
            raise ContractError(
                "maintenance override cannot waive configuration migration readiness"
            )


class MaintenanceOverrideAuthorizer:
    """Create a single-operation recovery authorization from an interactive TTY."""

    def __init__(
        self,
        *,
        stop_all_units: Callable[[], tuple[str, ...]],
        audit: Callable[[Mapping[str, Any]], None],
        input_stream: TextIO,
        output_stream: TextIO,
    ) -> None:
        self.stop_all_units = stop_all_units
        self.audit = audit
        self.input_stream = input_stream
        self.output_stream = output_stream

    def authorize(
        self,
        *,
        snapshot: ActivationSafetySnapshot,
        operation_id: str,
        actor_id: str,
        release_id: str,
        unattended: bool = True,
    ) -> MaintenanceOverride:
        if (
            unattended
            or not self.input_stream.isatty()
            or not self.output_stream.isatty()
        ):
            raise ContractError(
                "maintenance override requires an attended interactive terminal"
            )
        _require_operation(operation_id)
        for value, label in ((actor_id, "actor"), (release_id, "release")):
            _require_hash(value, label=f"maintenance override {label} identity")
        stopped_units = self.stop_all_units()
        _validate_stopped_units(stopped_units)
        phrase = f"PHYSICALLY SAFE {snapshot.logical_target}"
        self.output_stream.write(
            "III units are stopped. Confirm the aircraft is physically restrained, propellers "
            f"cannot energize, and the area is clear by typing: {phrase}\n"
        )
        self.output_stream.flush()
        confirmation = self.input_stream.readline().rstrip("\r\n")
        if confirmation != phrase:
            event = {
                "schema": OVERRIDE_SCHEMA,
                "event": "maintenance-override-denied",
                "operation_id": operation_id,
                "actor_id": actor_id,
                "release_id": release_id,
                "logical_target": snapshot.logical_target,
                "profile": snapshot.profile,
                "observation_id": snapshot.observation_id,
                "stopped_units": list(stopped_units),
                "reason": "physical safety confirmation did not match",
            }
            self.audit(event)
            raise ContractError(
                "maintenance override physical safety confirmation did not match"
            )
        value: dict[str, Any] = {
            "schema": OVERRIDE_SCHEMA,
            "override_id": "0" * 64,
            "operation_id": operation_id,
            "actor_id": actor_id,
            "release_id": release_id,
            "logical_target": snapshot.logical_target,
            "profile": snapshot.profile,
            "observation_id": snapshot.observation_id,
            "stopped_units": list(stopped_units),
            "confirmation": confirmation,
        }
        value["override_id"] = _identity(value, "override_id")
        self.audit({**value, "event": "maintenance-override-authorized"})
        authorization = MaintenanceOverride(
            **{**value, "stopped_units": tuple(value["stopped_units"])}
        )
        authorization.validate()
        return authorization


class ActivationTransactionStore:
    """Durably switch a compatible code/configuration/catalog tuple.

    ``active-selector.json`` is the canonical atomic selector.  The two symlinks
    are materialized views for existing launch/configuration consumers.  A crash
    between views is recoverable from the transaction journal and never creates
    a selector document with a mixed tuple.
    """

    def __init__(
        self,
        target_root: Path,
        *,
        enforce_host_contract: bool | None = None,
        selector_owner: tuple[int, int] | None = None,
    ):
        self.target_root = target_root.resolve()
        self.enforce_host_contract = (
            self.target_root == Path("/")
            if enforce_host_contract is None
            else enforce_host_contract
        )
        self.release_root = self.target_root / "opt/iii/releases"
        self.release_selector = self.target_root / "opt/iii/current"
        self.checkpoint_root = (
            self.target_root / "var/lib/iii/configuration/checkpoints"
        )
        self.configuration_selector = (
            self.target_root / "var/lib/iii/configuration/current"
        )
        self.configuration_working_root = (
            self.target_root / "var/lib/iii/configuration/working"
        )
        self.state_root = self.target_root / "var/lib/iii/deployment"
        self.selector_path = self.state_root / "active-selector.json"
        self.transaction_root = self.state_root / "activation-transactions"
        self.selector_owner = selector_owner

    def _working_configuration(self, checkpoint: Path, checkpoint_id: str) -> Path:
        """Return a durable writable copy while keeping its checkpoint immutable."""

        destination = self.configuration_working_root / checkpoint_id
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise ContractError("configuration working tree is unsafe")
            return destination
        self.configuration_working_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{checkpoint_id}.", dir=self.configuration_working_root
            )
        )
        try:
            shutil.copytree(
                checkpoint,
                temporary,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("checkpoint.json"),
            )
            owner = self.selector_owner
            for path in temporary.rglob("*"):
                if path.is_symlink():
                    raise ContractError("configuration checkpoint contains a link")
                path.chmod(0o770 if path.is_dir() else 0o660)
                if owner is not None:
                    os.chown(path, *owner)
            temporary.chmod(0o770)
            if owner is not None:
                os.chown(temporary, *owner)
            try:
                os.replace(temporary, destination)
            except FileExistsError:
                shutil.rmtree(temporary)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _under(self, path_text: str, root: Path, *, label: str) -> Path:
        path = Path(path_text)
        if path.is_symlink():
            raise ContractError(f"activation {label} is linked")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ContractError(f"activation {label} is unavailable: {exc}") from exc
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_dir():
            raise ContractError(f"activation {label} escapes its fixed root")
        return resolved

    def _verify_tuple(self, value: ActivationTuple) -> tuple[Path, Path]:
        value.validate()
        release = self._under(value.release_path, self.release_root, label="release")
        checkpoint = self._under(
            value.configuration_checkpoint_path,
            self.checkpoint_root,
            label="configuration checkpoint",
        )
        if release.name != value.release_id:
            raise ContractError("activation release path/identity mismatch")
        manifest = _read_canonical(
            release / "release-manifest.json", label="release manifest"
        )
        if manifest.get("release_id") != value.release_id:
            raise ContractError("activation release manifest identity mismatch")
        if self.enforce_host_contract:
            report = _read_canonical(
                self.state_root / "host-baseline-report.json",
                label="converged host baseline report",
            )
            target = manifest.get("target", {})
            if (
                report.get("schema") != "iii.host-baseline-report/v1"
                or report.get("state") != "converged"
                or report.get("baseline_id") != target.get("host_baseline")
                or report.get("unit_contract_id") != target.get("host_unit_contract")
                or report.get("target_definition_id") != target.get("definition_id")
            ):
                raise ContractError(
                    "release activation requires Ansible host maintenance"
                )
        profile = next(
            (
                item
                for item in manifest.get("profiles", [])
                if item.get("id") == value.profile
            ),
            None,
        )
        if profile is None or not profile.get("bootable"):
            raise ContractError(
                "activation profile is absent or not bootable in the release"
            )
        configuration = manifest.get("configuration", {})
        if configuration.get("schema_version") != value.configuration_schema_version:
            raise ContractError(
                "activation configuration schema differs from the release"
            )
        if (
            manifest.get("mission_catalog", {}).get("catalog_hash")
            != value.mission_catalog_hash
        ):
            raise ContractError("activation mission catalog differs from the release")
        checkpoint_manifest = _read_canonical(
            checkpoint / "checkpoint.json",
            label="configuration checkpoint manifest",
        )
        checkpoint_identity = checkpoint_manifest.get("checkpoint_id")
        if checkpoint_identity != value.configuration_checkpoint_id:
            raise ContractError("activation configuration checkpoint identity mismatch")
        identity_document = {
            key: item
            for key, item in checkpoint_manifest.items()
            if key != "checkpoint_id"
        }
        if content_identity(identity_document) != checkpoint_identity:
            raise ContractError("configuration checkpoint logical identity mismatch")
        if (
            checkpoint_manifest.get("schema_version")
            != value.configuration_schema_version
        ):
            raise ContractError(
                "configuration checkpoint schema differs from the release"
            )
        if checkpoint_manifest.get("profile") != value.profile:
            raise ContractError(
                "configuration checkpoint profile differs from the release"
            )
        return release, checkpoint

    def current(self) -> ActivationTuple | None:
        if not self.selector_path.exists() and not self.selector_path.is_symlink():
            return None
        value = _read_canonical(self.selector_path, label="active deployment selector")
        if set(value) != {
            "schema",
            "selector_id",
            *ActivationTuple.__dataclass_fields__,
        }:
            raise ContractError("active deployment selector fields are malformed")
        if value["schema"] != SELECTOR_SCHEMA or value["selector_id"] != _identity(
            value, "selector_id"
        ):
            raise ContractError("active deployment selector identity mismatch")
        selected = ActivationTuple(
            **{field: value[field] for field in ActivationTuple.__dataclass_fields__}
        )
        self._verify_tuple(selected)
        return selected

    def _journal(
        self,
        *,
        operation_id: str,
        previous: ActivationTuple | None,
        candidate: ActivationTuple,
        checkpoint: str,
    ) -> dict[str, Any]:
        _require_operation(operation_id)
        value: dict[str, Any] = {
            "schema": TRANSACTION_SCHEMA,
            "transaction_id": "0" * 64,
            "operation_id": operation_id,
            "previous": asdict(previous) if previous is not None else None,
            "candidate": asdict(candidate),
            "checkpoint": checkpoint,
            "autonomy_started": False,
        }
        value["transaction_id"] = _identity(value, "transaction_id")
        atomic_document(self.transaction_root / f"{operation_id}.json", value)
        return value

    def _write_selector(self, selected: ActivationTuple) -> None:
        value = {
            "schema": SELECTOR_SCHEMA,
            "selector_id": "0" * 64,
            **asdict(selected),
        }
        value["selector_id"] = _identity(value, "selector_id")
        atomic_document(
            self.selector_path,
            value,
            mode=0o640,
            owner=self.selector_owner,
        )

    def switch(
        self,
        candidate: ActivationTuple,
        *,
        operation_id: str,
        stop_all_units: Callable[[], tuple[str, ...]],
    ) -> dict[str, Any]:
        release, checkpoint = self._verify_tuple(candidate)
        previous = self.current()
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="prepared",
        )
        stopped = stop_all_units()
        _validate_stopped_units(stopped)
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="units-stopped",
        )
        _atomic_symlink(self.release_selector, release)
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="code-selector-switched",
        )
        working = self._working_configuration(
            checkpoint, candidate.configuration_checkpoint_id
        )
        _atomic_symlink(self.configuration_selector, working)
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="configuration-selector-switched",
        )
        self._write_selector(candidate)
        return self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="selector-committed",
        )

    def rollback(self, *, operation_id: str) -> dict[str, Any]:
        path = self.transaction_root / f"{operation_id}.json"
        transaction = _read_canonical(path, label="activation transaction")
        if transaction.get("schema") != TRANSACTION_SCHEMA or transaction.get(
            "transaction_id"
        ) != _identity(transaction, "transaction_id"):
            raise ContractError("activation transaction identity mismatch")
        if transaction.get("operation_id") != operation_id:
            raise ContractError("activation transaction operation binding mismatch")
        previous_value = transaction.get("previous")
        if previous_value is None:
            raise ContractError("activation transaction has no rollback tuple")
        previous = ActivationTuple(**previous_value)
        candidate = ActivationTuple(**transaction["candidate"])
        release, checkpoint = self._verify_tuple(previous)
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="rollback-prepared",
        )
        _atomic_symlink(self.release_selector, release)
        self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="rollback-code-selector-switched",
        )
        working = self._working_configuration(
            checkpoint, previous.configuration_checkpoint_id
        )
        _atomic_symlink(self.configuration_selector, working)
        self._write_selector(previous)
        return self._journal(
            operation_id=operation_id,
            previous=previous,
            candidate=candidate,
            checkpoint="rollback-selector-committed",
        )
