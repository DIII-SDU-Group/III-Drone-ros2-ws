from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from iii_deployment.activation import (
    ActivationSafetySnapshot,
    ActivationTransactionStore,
    ActivationTuple,
)
from iii_deployment.activation_health import (
    ActivationCoordinator,
    ActivationDiagnosticStore,
    ActivationHealthGate,
    ActivationHealthPolicy,
    ActivationHealthSnapshot,
    ControlPlaneProof,
)
from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.staging import ActivationAuthorization


OLD_RELEASE = "1" * 64
NEW_RELEASE = "2" * 64
OLD_CHECKPOINT = "3" * 64
NEW_CHECKPOINT = "4" * 64
CATALOG = "sha256:" + "5" * 64
RECEIVER = "6" * 64
SAFETY_ID = "7" * 64
OPERATION = "activation-operation-0001"
REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


class Clock:
    def __init__(self, *, sleep_step: float | None = None) -> None:
        self.value = 100.0
        self.sleep_step = sleep_step

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += self.sleep_step if self.sleep_step is not None else duration

    @staticmethod
    def boot_id() -> str:
        return "boot-a"


class FakeReleaseStore:
    def __init__(self, root: Path, diagnostics: ActivationDiagnosticStore) -> None:
        self.releases_root = root / "opt/iii/releases"
        self._state = {
            "state_id": "8" * 64,
            "generation": 4,
            "active_release_id": OLD_RELEASE,
            "rollback_release_id": None,
            "candidate_release_id": NEW_RELEASE,
            "status_index_id": None,
        }
        self.diagnostics = diagnostics
        self.acceptance_calls = 0
        self.rollback_acceptance_calls = 0

    def state(self) -> dict:
        return dict(self._state)

    def authorize_activation(self, release_id: str, *, status_index):
        assert release_id == NEW_RELEASE
        assert status_index is None
        value = {
            "authorization_id": "0" * 64,
            "release_id": release_id,
            "release_class": "field-development",
            "state_id": self._state["state_id"],
            "state_generation": self._state["generation"],
            "status_index_id": None,
            "status_statement_id": None,
            "recovery_only": False,
            "flight_capable": True,
        }
        value["authorization_id"] = content_identity(
            {key: item for key, item in value.items() if key != "authorization_id"}
        )
        return ActivationAuthorization(**value)

    def record_acceptance(self, authorization, *, explicit_qualified_action):
        assert explicit_qualified_action is False
        assert authorization.release_id == NEW_RELEASE
        retained = self.diagnostics.load_state(OPERATION)
        assert retained is not None
        assert retained["stage"] == "acceptance-evidence-persisted"
        assert retained["evidence_id"] is not None
        self.acceptance_calls += 1
        self._state.update(
            {
                "state_id": "9" * 64,
                "generation": 5,
                "active_release_id": NEW_RELEASE,
                "rollback_release_id": OLD_RELEASE,
                "candidate_release_id": None,
            }
        )
        return dict(self._state)

    def authorize_rollback(self, release_id: str, *, status_index):
        assert release_id == OLD_RELEASE
        assert status_index is None
        assert self._state["rollback_release_id"] == OLD_RELEASE
        value = {
            "authorization_id": "0" * 64,
            "release_id": release_id,
            "release_class": "field-development",
            "state_id": self._state["state_id"],
            "state_generation": self._state["generation"],
            "status_index_id": None,
            "status_statement_id": None,
            "recovery_only": False,
            "flight_capable": True,
        }
        value["authorization_id"] = content_identity(
            {key: item for key, item in value.items() if key != "authorization_id"}
        )
        return ActivationAuthorization(**value)

    def record_rollback_acceptance(self, authorization):
        assert authorization.release_id == OLD_RELEASE
        assert self._state["active_release_id"] == NEW_RELEASE
        assert self._state["rollback_release_id"] == OLD_RELEASE
        self.rollback_acceptance_calls += 1
        self._state.update(
            {
                "state_id": "a" * 64,
                "generation": 6,
                "active_release_id": OLD_RELEASE,
                "rollback_release_id": NEW_RELEASE,
                "candidate_release_id": None,
            }
        )
        return dict(self._state)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _policy() -> ActivationHealthPolicy:
    value = ActivationHealthPolicy(
        required_hardware_roles=("fmu",),
        optional_hardware_roles=("camera",),
        required_services=("micro_ros_agent",),
        optional_services=(),
        required_managed_nodes={
            "configuration_server": "active",
            "mission_executor": "active",
        },
        optional_managed_nodes={},
        required_systemd_units=(
            "iii-runtime-api.service",
            "iii-system-daemon.service",
        ),
    )
    value.validate()
    return value


def _profile() -> dict:
    return {
        "id": "real",
        "bootable": True,
        "health": _policy().as_document(),
    }


def _checkpoint(root: Path, checkpoint_id_seed: str) -> tuple[str, Path]:
    path = root / "var/lib/iii/configuration/checkpoints" / checkpoint_id_seed
    value = {
        "checkpoint_id": "0" * 64,
        "schema": "iii.configuration-checkpoint/v1",
        "schema_version": 1,
        "profile": "real",
        "values_hash": checkpoint_id_seed,
    }
    value["checkpoint_id"] = content_identity(
        {key: item for key, item in value.items() if key != "checkpoint_id"}
    )
    path = path.parent / value["checkpoint_id"]
    path.mkdir(parents=True)
    _write(path / "checkpoint.json", value)
    return value["checkpoint_id"], path


def _release(root: Path, release_id: str, *, health: bool) -> Path:
    path = root / "opt/iii/releases" / release_id
    path.mkdir(parents=True)
    profile = _profile()
    if not health:
        profile.pop("health")
    _write(
        path / "release-manifest.json",
        {
            "release_id": release_id,
            "profiles": [profile],
            "configuration": {"schema_version": 1},
            "mission_catalog": {"catalog_hash": CATALOG},
            "compatibility": {"api_ranges": {"runtime_api": ">=2.0.0,<3.0.0"}},
        },
    )
    return path


def _tuple(
    release_id: str, release: Path, checkpoint_id: str, checkpoint: Path
) -> ActivationTuple:
    return ActivationTuple(
        release_id=release_id,
        release_path=str(release),
        configuration_checkpoint_id=checkpoint_id,
        configuration_checkpoint_path=str(checkpoint),
        configuration_schema_version=1,
        mission_catalog_hash=CATALOG,
        profile="real",
    )


def _safety(checkpoint_id: str) -> ActivationSafetySnapshot:
    snapshot = ActivationSafetySnapshot(
        logical_target="drone",
        profile="real",
        observation_id="0" * 64,
        runtime_api_available=True,
        runtime_identity_matches=True,
        runtime_fresh=True,
        px4_available=True,
        px4_fresh=True,
        armed=False,
        in_air=False,
        nav_state="hold",
        failsafe=False,
        mission_fresh=True,
        mission_active=False,
        mission_control_owner=False,
        operation_fresh=True,
        custom_operation_active=False,
        custom_operation_control_owner=False,
        direct_operation_active=False,
        reference_owner_active=False,
        configuration_migration_ready=True,
        configuration_checkpoint_id=checkpoint_id,
        continuously_safe_for_s=3.0,
    )
    value = asdict(snapshot)
    value.pop("observation_id")
    return replace(snapshot, observation_id=content_identity(value))


def _health(
    candidate: ActivationTuple, clock: Clock, **changes
) -> ActivationHealthSnapshot:
    value = {
        "schema": "iii.activation-health/v1",
        "evidence_id": "0" * 64,
        "release_id": candidate.release_id,
        "profile": candidate.profile,
        "boot_id": clock.boot_id(),
        "observed_monotonic": clock.monotonic(),
        "receiver": {"ready": True, "receiver_id": RECEIVER, "generation": 7},
        "bootstrap": {"ready": True, "protocol_version": "1"},
        "daemon": {
            "available": True,
            "fresh": True,
            "release_id": candidate.release_id,
            "profile": candidate.profile,
        },
        "runtime_api": {
            "available": True,
            "fresh": True,
            "release_id": candidate.release_id,
            "profile": candidate.profile,
            "api_version": ">=2.0.0,<3.0.0",
        },
        "configuration": {
            "reconciled": True,
            "durable": True,
            "schema_valid": True,
            "checkpoint_id": candidate.configuration_checkpoint_id,
            "schema_version": candidate.configuration_schema_version,
        },
        "hardware_roles": {"fmu": {"state": "present", "unambiguous": True}},
        "services": {"micro_ros_agent": {"alive": True, "ready": True}},
        "managed_nodes": {
            "configuration_server": "active",
            "mission_executor": "active",
        },
        "systemd_units": {
            "iii-runtime-api.service": "active",
            "iii-system-daemon.service": "active",
        },
        "px4": {
            "available": True,
            "fresh": True,
            "interface_compatible": True,
            "firmware_compatible": True,
            "parameter_manifest_matches": True,
            "armed": False,
            "in_air": False,
            "failsafe": False,
            "nav_state": "hold",
        },
        "operations": {
            "fresh": True,
            "mission_active": False,
            "mission_control_owner": False,
            "custom_operation_active": False,
            "custom_operation_control_owner": False,
            "direct_operation_active": False,
            "reference_owner_active": False,
        },
    }
    value.update(changes)
    value["evidence_id"] = content_identity(
        {key: item for key, item in value.items() if key != "evidence_id"}
    )
    snapshot = ActivationHealthSnapshot(**value)
    snapshot.validate()
    return snapshot


def _proof(candidate: ActivationTuple) -> ControlPlaneProof:
    value = {
        "release_id": candidate.release_id,
        "profile": candidate.profile,
        "started_units": (
            "iii-runtime-api.service",
            "iii-system-daemon.service",
        ),
        "autonomy_started": False,
        "proof_id": "0" * 64,
    }
    document = {
        **value,
        "started_units": list(value["started_units"]),
        "schema": "iii.activation-control-plane-proof/v1",
    }
    value["proof_id"] = content_identity(
        {key: item for key, item in document.items() if key != "proof_id"}
    )
    proof = ControlPlaneProof(**value)
    proof.validate(expected=candidate)
    return proof


def _environment(tmp_path: Path, *, clock: Clock | None = None):
    clock = clock or Clock()
    diagnostics = ActivationDiagnosticStore(
        tmp_path / "var/lib/iii/deployment/activation"
    )
    store = FakeReleaseStore(tmp_path, diagnostics)
    old_release = _release(tmp_path, OLD_RELEASE, health=False)
    new_release = _release(tmp_path, NEW_RELEASE, health=True)
    old_id, old_checkpoint = _checkpoint(tmp_path, OLD_CHECKPOINT)
    new_id, new_checkpoint = _checkpoint(tmp_path, NEW_CHECKPOINT)
    old = _tuple(OLD_RELEASE, old_release, old_id, old_checkpoint)
    new = _tuple(NEW_RELEASE, new_release, new_id, new_checkpoint)
    transactions = ActivationTransactionStore(tmp_path)
    transactions._write_selector(old)
    (tmp_path / "opt/iii/current").symlink_to(old_release)
    (tmp_path / "var/lib/iii/configuration/current").symlink_to(old_checkpoint)
    stop_calls: list[str] = []
    start_calls: list[str] = []

    def stop():
        stop_calls.append("stop")
        return ("iii.target", "iii-runtime-api.service", "iii-system-daemon.service")

    def start(candidate):
        start_calls.append(candidate.release_id)
        return _proof(candidate)

    coordinator = ActivationCoordinator(
        release_store=store,
        transaction_store=transactions,
        diagnostics=diagnostics,
        safety_provider=lambda: _safety(new_id),
        health_provider=lambda candidate, _policy_value: _health(candidate, clock),
        stop_all_units=stop,
        start_control_plane=start,
        receiver_id=RECEIVER,
        receiver_generation=7,
        bootstrap_protocol_version="1",
        logical_target="drone",
        profile="real",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        boot_id=clock.boot_id,
        poll_interval_s=0.5,
    )
    return {
        "clock": clock,
        "diagnostics": diagnostics,
        "store": store,
        "transactions": transactions,
        "old": old,
        "new": new,
        "new_checkpoint_id": new_id,
        "stop_calls": stop_calls,
        "start_calls": start_calls,
        "coordinator": coordinator,
    }


def test_activation_accepts_only_after_stable_health_evidence_is_durable(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    result = env["coordinator"].activate(
        operation_id=OPERATION,
        release_id=NEW_RELEASE,
        configuration_checkpoint_id=env["new_checkpoint_id"],
        explicit_qualified_action=False,
    )
    assert result["stable_window_s"] == 10.0
    assert result["automatic_rollback_permitted"] is False
    assert result["autonomy_started"] is False
    assert env["store"].acceptance_calls == 1
    assert env["transactions"].current() == env["new"]
    state = env["diagnostics"].load_state(OPERATION)
    assert state["stage"] == "accepted"
    assert state["accepted_state_id"] == "9" * 64
    assert state["automatic_rollback_permitted"] is False
    evidence_path = env["diagnostics"].evidence_path(OPERATION, state["evidence_id"])
    assert evidence_path.is_file()
    REGISTRY.validate("activation-health-transaction", state)
    REGISTRY.validate(
        "activation-health",
        __import__("json").loads(evidence_path.read_bytes()),
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"receiver": {"ready": False, "receiver_id": RECEIVER, "generation": 7}},
            "receiver identity",
        ),
        (
            {"bootstrap": {"ready": False, "protocol_version": "1"}},
            "bootstrap readiness",
        ),
        (
            {
                "daemon": {
                    "available": False,
                    "fresh": True,
                    "release_id": NEW_RELEASE,
                    "profile": "real",
                }
            },
            "daemon is unavailable",
        ),
        (
            {
                "runtime_api": {
                    "available": True,
                    "fresh": False,
                    "release_id": NEW_RELEASE,
                    "profile": "real",
                    "api_version": ">=2.0.0,<3.0.0",
                }
            },
            "runtime API evidence is stale",
        ),
        (
            {
                "configuration": {
                    "reconciled": False,
                    "durable": True,
                    "schema_valid": True,
                    "checkpoint_id": "4" * 64,
                    "schema_version": 1,
                }
            },
            "configuration is not reconciled",
        ),
        ({"hardware_roles": {}}, "required hardware role is absent"),
        (
            {"hardware_roles": {"fmu": {"state": "ambiguous", "unambiguous": False}}},
            "required hardware role is unhealthy",
        ),
        ({"services": {}}, "required service is absent"),
        (
            {"managed_nodes": {"configuration_server": "active"}},
            "required managed node is absent",
        ),
        (
            {
                "systemd_units": {
                    "iii-runtime-api.service": "active",
                    "iii-system-daemon.service": "failed",
                }
            },
            "expected active",
        ),
        (
            {
                "px4": {
                    "available": True,
                    "fresh": True,
                    "interface_compatible": False,
                    "firmware_compatible": True,
                    "parameter_manifest_matches": True,
                    "armed": False,
                    "in_air": False,
                    "failsafe": False,
                    "nav_state": "hold",
                }
            },
            "PX4 interface compatible",
        ),
        (
            {
                "px4": {
                    "available": True,
                    "fresh": True,
                    "interface_compatible": True,
                    "firmware_compatible": True,
                    "parameter_manifest_matches": True,
                    "armed": True,
                    "in_air": False,
                    "failsafe": False,
                    "nav_state": "hold",
                }
            },
            "not confirmed disarmed",
        ),
        (
            {
                "operations": {
                    "fresh": True,
                    "mission_active": True,
                    "mission_control_owner": False,
                    "custom_operation_active": False,
                    "custom_operation_control_owner": False,
                    "direct_operation_active": False,
                    "reference_owner_active": False,
                }
            },
            "Mission Execution",
        ),
        (
            {
                "operations": {
                    "fresh": True,
                    "mission_active": False,
                    "mission_control_owner": False,
                    "custom_operation_active": False,
                    "custom_operation_control_owner": False,
                    "direct_operation_active": False,
                    "reference_owner_active": True,
                }
            },
            "Reference Owner",
        ),
    ],
)
def test_every_health_domain_fails_closed(tmp_path: Path, changes: dict, reason: str):
    env = _environment(tmp_path)
    snapshot = _health(env["new"], env["clock"], **changes)
    gate = ActivationHealthGate(
        candidate=env["new"],
        policy=_policy(),
        receiver_id=RECEIVER,
        receiver_generation=7,
        bootstrap_protocol_version="1",
        runtime_api_version_range=">=2.0.0,<3.0.0",
    )
    assert any(reason in item for item in gate.rejection_reasons(snapshot))


def test_optional_absence_is_allowed_but_undeclared_or_unhealthy_optional_is_not(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    gate = ActivationHealthGate(
        candidate=env["new"],
        policy=_policy(),
        receiver_id=RECEIVER,
        receiver_generation=7,
        bootstrap_protocol_version="1",
        runtime_api_version_range=">=2.0.0,<3.0.0",
    )
    assert gate.rejection_reasons(_health(env["new"], env["clock"])) == []
    unhealthy = _health(
        env["new"],
        env["clock"],
        hardware_roles={
            "camera": {"state": "ambiguous", "unambiguous": False},
            "fmu": {"state": "present", "unambiguous": True},
        },
    )
    assert (
        "present optional hardware role is unhealthy: camera"
        in gate.rejection_reasons(unhealthy)
    )
    undeclared = _health(
        env["new"],
        env["clock"],
        hardware_roles={
            "fmu": {"state": "present", "unambiguous": True},
            "mystery": {"state": "present", "unambiguous": True},
        },
    )
    assert "observed undeclared hardware roles: mystery" in gate.rejection_reasons(
        undeclared
    )


def test_failed_health_times_out_onboard_and_restores_previous_without_autonomy(
    tmp_path: Path,
):
    env = _environment(tmp_path, clock=Clock(sleep_step=120.0))
    env["coordinator"].health_provider = lambda candidate, _policy_value: _health(
        candidate,
        env["clock"],
        px4={
            "available": True,
            "fresh": True,
            "interface_compatible": True,
            "firmware_compatible": True,
            "parameter_manifest_matches": True,
            "armed": True,
            "in_air": False,
            "failsafe": False,
            "nav_state": "hold",
        },
    )
    with pytest.raises(ContractError, match="failed and restored"):
        env["coordinator"].activate(
            operation_id=OPERATION,
            release_id=NEW_RELEASE,
            configuration_checkpoint_id=env["new_checkpoint_id"],
            explicit_qualified_action=False,
        )
    assert env["transactions"].current() == env["old"]
    state = env["diagnostics"].load_state(OPERATION)
    assert state["stage"] == "rolled-back"
    assert state["rollback"]["autonomy_started"] is False
    assert env["store"].acceptance_calls == 0


@pytest.mark.parametrize(
    "stage",
    [
        "prepared",
        "selector-switched",
        "control-plane-started",
        "health-observing",
        "acceptance-evidence-persisted",
        "rollback-prepared",
    ],
)
def test_power_loss_at_every_preacceptance_stage_reconciles_to_previous(
    tmp_path: Path, stage: str
):
    env = _environment(tmp_path)
    authorization = env["store"].authorize_activation(NEW_RELEASE, status_index=None)
    safety = _safety(env["new_checkpoint_id"])
    if stage != "prepared":
        env["transactions"].switch(
            env["new"], operation_id=OPERATION, stop_all_units=lambda: ("iii.target",)
        )
    env["diagnostics"].write_state(
        operation_id=OPERATION,
        previous=env["old"],
        candidate=env["new"],
        authorization=authorization,
        safety_observation_id=safety.observation_id,
        safety_snapshot=safety.as_document(),
        stage=stage,
        boot_id="boot-before-loss",
        monotonic=5.0,
        evidence_id=None,
    )
    result = env["coordinator"].reconcile()
    assert result["restored_operations"] == [OPERATION]
    assert result["autonomy_started"] is False
    assert env["transactions"].current() == env["old"]
    assert env["diagnostics"].load_state(OPERATION)["stage"] == "rolled-back"


def test_reconcile_recognizes_durable_acceptance_window_without_rollback(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    authorization = env["store"].authorize_activation(NEW_RELEASE, status_index=None)
    safety = _safety(env["new_checkpoint_id"])
    env["transactions"].switch(
        env["new"], operation_id=OPERATION, stop_all_units=lambda: ("iii.target",)
    )
    evidence = _health(env["new"], env["clock"])
    env["diagnostics"].retain_snapshot(OPERATION, evidence)
    env["diagnostics"].write_state(
        operation_id=OPERATION,
        previous=env["old"],
        candidate=env["new"],
        authorization=authorization,
        safety_observation_id=safety.observation_id,
        safety_snapshot=safety.as_document(),
        stage="acceptance-evidence-persisted",
        boot_id="boot-before-loss",
        monotonic=5.0,
        evidence_id=evidence.evidence_id,
    )
    env["store"]._state.update(
        {
            "active_release_id": NEW_RELEASE,
            "candidate_release_id": None,
            "state_id": "9" * 64,
        }
    )
    result = env["coordinator"].reconcile()
    assert result["accepted_operations"] == [OPERATION]
    assert env["transactions"].current() == env["new"]
    state = env["diagnostics"].load_state(OPERATION)
    assert state["stage"] == "accepted"
    assert state["automatic_rollback_permitted"] is False


def test_post_acceptance_failure_exhausts_bounded_restart_and_never_rolls_back(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    env["coordinator"].activate(
        operation_id=OPERATION,
        release_id=NEW_RELEASE,
        configuration_checkpoint_id=env["new_checkpoint_id"],
        explicit_qualified_action=False,
    )

    def fail(_candidate):
        raise RuntimeError("runtime process remains failed")

    env["coordinator"].start_control_plane = fail
    result = env["coordinator"].handle_post_acceptance_failure(
        operation_id=OPERATION, maximum_restart_attempts=2
    )
    assert result == {
        "outcome": "faulted",
        "attempts": 2,
        "selector_changed": False,
        "automatic_rollback_permitted": False,
        "state_id": result["state_id"],
    }
    assert env["transactions"].current() == env["new"]
    assert env["diagnostics"].load_state(OPERATION)["stage"] == "faulted"


def test_explicit_operator_rollback_rechecks_safety_health_and_swaps_retained_roles(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    env["coordinator"].activate(
        operation_id=OPERATION,
        release_id=NEW_RELEASE,
        configuration_checkpoint_id=env["new_checkpoint_id"],
        explicit_qualified_action=False,
    )
    old_manifest = _profile()
    _write(
        Path(env["old"].release_path) / "release-manifest.json",
        {
            "release_id": OLD_RELEASE,
            "profiles": [old_manifest],
            "configuration": {"schema_version": 1},
            "mission_catalog": {"catalog_hash": CATALOG},
            "compatibility": {"api_ranges": {"runtime_api": ">=2.0.0,<3.0.0"}},
        },
    )
    env["coordinator"].safety_provider = lambda: _safety(
        env["old"].configuration_checkpoint_id
    )
    preflight = env["coordinator"].preflight(
        release_id=OLD_RELEASE,
        configuration_checkpoint_id=env["old"].configuration_checkpoint_id,
        operator_rollback=True,
    )
    assert preflight["ready"] is True
    result = env["coordinator"].operator_rollback(
        operation_id="operator-rollback-0001",
        release_id=OLD_RELEASE,
        configuration_checkpoint_id=env["old"].configuration_checkpoint_id,
    )
    assert result["kind"] == "rollback"
    assert result["automatic_rollback_permitted"] is False
    assert result["autonomy_started"] is False
    assert env["transactions"].current() == env["old"]
    assert env["store"].state()["active_release_id"] == OLD_RELEASE
    assert env["store"].state()["rollback_release_id"] == NEW_RELEASE
    assert env["store"].rollback_acceptance_calls == 1


def test_explicit_operator_rollback_is_denied_while_armed_without_selector_change(
    tmp_path: Path,
):
    env = _environment(tmp_path)
    env["coordinator"].activate(
        operation_id=OPERATION,
        release_id=NEW_RELEASE,
        configuration_checkpoint_id=env["new_checkpoint_id"],
        explicit_qualified_action=False,
    )
    _write(
        Path(env["old"].release_path) / "release-manifest.json",
        {
            "release_id": OLD_RELEASE,
            "profiles": [_profile()],
            "configuration": {"schema_version": 1},
            "mission_catalog": {"catalog_hash": CATALOG},
            "compatibility": {"api_ranges": {"runtime_api": ">=2.0.0,<3.0.0"}},
        },
    )
    safe = _safety(env["old"].configuration_checkpoint_id)
    identity_fields = asdict(safe)
    identity_fields.pop("observation_id")
    identity_fields["armed"] = True
    armed = replace(
        safe,
        armed=True,
        observation_id=content_identity(identity_fields),
    )
    env["coordinator"].safety_provider = lambda: armed
    preflight = env["coordinator"].preflight(
        release_id=OLD_RELEASE,
        configuration_checkpoint_id=env["old"].configuration_checkpoint_id,
        operator_rollback=True,
    )
    assert preflight["ready"] is False
    assert any("armed" in reason for reason in preflight["rejection_reasons"])
    with pytest.raises(ContractError, match="armed"):
        env["coordinator"].operator_rollback(
            operation_id="operator-rollback-0002",
            release_id=OLD_RELEASE,
            configuration_checkpoint_id=env["old"].configuration_checkpoint_id,
        )
    assert env["transactions"].current() == env["new"]
    assert env["store"].state()["active_release_id"] == NEW_RELEASE
    assert env["store"].rollback_acceptance_calls == 0
