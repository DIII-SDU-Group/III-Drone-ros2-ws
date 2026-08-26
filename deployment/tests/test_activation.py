from __future__ import annotations

from dataclasses import asdict, replace
import io
from pathlib import Path

import pytest

from iii_deployment.activation import (
    ActivationSafetyGate,
    ActivationSafetySnapshot,
    ActivationTransactionStore,
    ActivationTuple,
    MaintenanceOverrideAuthorizer,
)
from iii_deployment.contracts import ContractError, canonical_json, content_identity

IDENTITY = "a" * 64
RELEASE_ONE = "b" * 64
RELEASE_TWO = "c" * 64
CHECKPOINT_ONE = "d" * 64
CHECKPOINT_TWO = "e" * 64
CATALOG_ONE = "sha256:" + "1" * 64
CATALOG_TWO = "sha256:" + "2" * 64


def _snapshot(**overrides) -> ActivationSafetySnapshot:
    value = {
        "logical_target": "drone",
        "profile": "real",
        "observation_id": "0" * 64,
        "runtime_api_available": True,
        "runtime_identity_matches": True,
        "runtime_fresh": True,
        "px4_available": True,
        "px4_fresh": True,
        "armed": False,
        "in_air": False,
        "nav_state": "hold",
        "failsafe": False,
        "mission_fresh": True,
        "mission_active": False,
        "mission_control_owner": False,
        "operation_fresh": True,
        "custom_operation_active": False,
        "custom_operation_control_owner": False,
        "direct_operation_active": False,
        "reference_owner_active": False,
        "configuration_migration_ready": True,
        "configuration_checkpoint_id": CHECKPOINT_TWO,
        "continuously_safe_for_s": 3.0,
    }
    value.update(overrides)
    snapshot = ActivationSafetySnapshot(**value)
    document = asdict(snapshot)
    document.pop("observation_id")
    return replace(snapshot, observation_id=content_identity(document))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"armed": True}, "not confirmed disarmed"),
        ({"in_air": True}, "not confirmed landed"),
        ({"mission_active": True}, "Mission Execution is not confirmed inactive"),
        ({"mission_control_owner": True}, "Mission Execution control ownership"),
        (
            {"custom_operation_active": True},
            "Custom Operation is not confirmed inactive",
        ),
        (
            {"custom_operation_control_owner": True},
            "Custom Operation control ownership",
        ),
        (
            {"direct_operation_active": True},
            "Direct Operation is not confirmed inactive",
        ),
        ({"reference_owner_active": True}, "active Reference Owner"),
        ({"px4_fresh": False}, "PX4 safety telemetry is stale"),
        ({"runtime_api_available": False}, "runtime API is unavailable"),
        ({"continuously_safe_for_s": 2.99}, "continuous for three seconds"),
    ],
)
def test_activation_safety_state_matrix_fails_closed(overrides, reason):
    gate = ActivationSafetyGate(logical_target="drone", profile="real")
    with pytest.raises(ContractError, match=reason):
        gate.authorize(
            _snapshot(**overrides),
            operation_id=IDENTITY,
            release_id=RELEASE_TWO,
        )


def test_landed_disarmed_continuous_state_is_authorized_without_override():
    ActivationSafetyGate(logical_target="drone", profile="real").authorize(
        _snapshot(),
        operation_id=IDENTITY,
        release_id=RELEASE_TWO,
    )


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_maintenance_override_is_attended_stops_units_and_is_narrowly_bound():
    snapshot = _snapshot(
        runtime_api_available=False,
        runtime_fresh=False,
        px4_available=False,
        px4_fresh=False,
        armed=None,
        in_air=None,
        nav_state=None,
        failsafe=None,
        mission_fresh=False,
        mission_active=None,
        mission_control_owner=None,
        operation_fresh=False,
        custom_operation_active=None,
        custom_operation_control_owner=None,
        direct_operation_active=None,
        reference_owner_active=None,
        continuously_safe_for_s=0,
    )
    events = []
    calls = []
    authorizer = MaintenanceOverrideAuthorizer(
        stop_all_units=lambda: calls.append("stopped")
        or ("iii.target", "iii-runtime.service"),
        audit=events.append,
        input_stream=_Tty("PHYSICALLY SAFE drone\n"),
        output_stream=_Tty(),
    )
    override = authorizer.authorize(
        snapshot=snapshot,
        operation_id=IDENTITY,
        actor_id="f" * 64,
        release_id=RELEASE_TWO,
        unattended=False,
    )
    assert calls == ["stopped"]
    assert events[-1]["event"] == "maintenance-override-authorized"
    ActivationSafetyGate(logical_target="drone", profile="real").authorize(
        snapshot,
        maintenance_override=override,
        operation_id=IDENTITY,
        release_id=RELEASE_TWO,
    )
    with pytest.raises(ContractError, match="release_id binding mismatch"):
        ActivationSafetyGate(logical_target="drone", profile="real").authorize(
            snapshot,
            maintenance_override=override,
            operation_id=IDENTITY,
            release_id=RELEASE_ONE,
        )


def test_maintenance_override_is_unavailable_to_scripts_and_cannot_waive_known_flight():
    snapshot = _snapshot(runtime_api_available=False, armed=True)
    authorizer = MaintenanceOverrideAuthorizer(
        stop_all_units=lambda: ("iii.target",),
        audit=lambda _event: None,
        input_stream=_Tty("PHYSICALLY SAFE drone\n"),
        output_stream=_Tty(),
    )
    with pytest.raises(ContractError, match="attended interactive terminal"):
        authorizer.authorize(
            snapshot=snapshot,
            operation_id=IDENTITY,
            actor_id="f" * 64,
            release_id=RELEASE_TWO,
        )
    override = authorizer.authorize(
        snapshot=snapshot,
        operation_id=IDENTITY,
        actor_id="f" * 64,
        release_id=RELEASE_TWO,
        unattended=False,
    )
    with pytest.raises(ContractError, match="known active safety evidence: armed"):
        ActivationSafetyGate(logical_target="drone", profile="real").authorize(
            snapshot,
            maintenance_override=override,
            operation_id=IDENTITY,
            release_id=RELEASE_TWO,
        )


def test_maintenance_override_requires_canonical_all_units_stop_proof():
    authorizer = MaintenanceOverrideAuthorizer(
        stop_all_units=lambda: ("iii-runtime.service",),
        audit=lambda _event: None,
        input_stream=_Tty("PHYSICALLY SAFE drone\n"),
        output_stream=_Tty(),
    )
    with pytest.raises(ContractError, match="all-III-unit stop proof"):
        authorizer.authorize(
            snapshot=_snapshot(runtime_api_available=False),
            operation_id=IDENTITY,
            actor_id="f" * 64,
            release_id=RELEASE_TWO,
            unattended=False,
        )


def _write_document(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _install_tuple(
    root: Path,
    *,
    release_id: str,
    checkpoint_id: str,
    catalog_hash: str,
) -> ActivationTuple:
    release = root / "opt/iii/releases" / release_id
    release.mkdir(parents=True)
    _write_document(
        release / "release-manifest.json",
        {
            "release_id": release_id,
            "profiles": [{"id": "real", "bootable": True}],
            "configuration": {"schema_version": 1},
            "mission_catalog": {"catalog_hash": catalog_hash},
        },
    )
    checkpoint = root / "var/lib/iii/configuration/checkpoints" / checkpoint_id
    checkpoint.mkdir(parents=True)
    checkpoint_manifest = {
        "checkpoint_id": "0" * 64,
        "schema": "iii.configuration-checkpoint/v1",
        "schema_version": 1,
        "profile": "real",
        "values_hash": checkpoint_id,
    }
    checkpoint_manifest["checkpoint_id"] = content_identity(
        {
            key: value
            for key, value in checkpoint_manifest.items()
            if key != "checkpoint_id"
        }
    )
    actual_id = checkpoint_manifest["checkpoint_id"]
    if checkpoint_id != actual_id:
        checkpoint.rename(checkpoint.parent / actual_id)
        checkpoint = checkpoint.parent / actual_id
    _write_document(checkpoint / "checkpoint.json", checkpoint_manifest)
    return ActivationTuple(
        release_id=release_id,
        release_path=str(release),
        configuration_checkpoint_id=actual_id,
        configuration_checkpoint_path=str(checkpoint),
        configuration_schema_version=1,
        mission_catalog_hash=catalog_hash,
        profile="real",
    )


def test_activation_switches_and_rolls_back_matching_code_configuration_and_catalog(
    tmp_path,
):
    first = _install_tuple(
        tmp_path,
        release_id=RELEASE_ONE,
        checkpoint_id=CHECKPOINT_ONE,
        catalog_hash=CATALOG_ONE,
    )
    second = _install_tuple(
        tmp_path,
        release_id=RELEASE_TWO,
        checkpoint_id=CHECKPOINT_TWO,
        catalog_hash=CATALOG_TWO,
    )
    store = ActivationTransactionStore(tmp_path)
    first_operation = "1" * 64
    second_operation = "2" * 64
    first_journal = store.switch(
        first,
        operation_id=first_operation,
        stop_all_units=lambda: ("iii.target",),
    )
    assert first_journal["checkpoint"] == "selector-committed"
    assert first_journal["autonomy_started"] is False
    store.switch(
        second,
        operation_id=second_operation,
        stop_all_units=lambda: ("iii.target",),
    )
    assert store.current() == second
    assert (tmp_path / "opt/iii/current").resolve() == Path(second.release_path)
    assert (tmp_path / "var/lib/iii/configuration/current").resolve() == Path(
        second.configuration_checkpoint_path
    )

    rolled_back = store.rollback(operation_id=second_operation)
    assert rolled_back["checkpoint"] == "rollback-selector-committed"
    assert rolled_back["autonomy_started"] is False
    assert store.current() == first
    assert store.current().mission_catalog_hash == CATALOG_ONE


def test_activation_rejects_mixed_release_catalog_or_configuration(tmp_path):
    selected = _install_tuple(
        tmp_path,
        release_id=RELEASE_ONE,
        checkpoint_id=CHECKPOINT_ONE,
        catalog_hash=CATALOG_ONE,
    )
    store = ActivationTransactionStore(tmp_path)
    with pytest.raises(ContractError, match="mission catalog differs"):
        store.switch(
            replace(selected, mission_catalog_hash=CATALOG_TWO),
            operation_id=IDENTITY,
            stop_all_units=lambda: ("iii.target",),
        )
    with pytest.raises(ContractError, match="schema differs"):
        store.switch(
            replace(selected, configuration_schema_version=2),
            operation_id=IDENTITY,
            stop_all_units=lambda: ("iii.target",),
        )
