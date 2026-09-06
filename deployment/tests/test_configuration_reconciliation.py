from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import yaml

from iii_deployment.configuration_reconciliation import ReceiverConfigurationReconciler

ROOT = Path(__file__).resolve().parents[2]
CONFIGURATION_SOURCE = ROOT / "src/III-Drone-Configuration/config"


def _materialize_contract(destination: Path) -> Path:
    shutil.copytree(CONFIGURATION_SOURCE / "configuration_contract", destination)
    schema = destination / "schema/parameter_manifest.yaml"
    real = destination / "tracked_defaults/real/default.yaml"
    sim = destination / "tracked_defaults/sim/default.yaml"
    schema.parent.mkdir()
    real.parent.mkdir(parents=True)
    sim.parent.mkdir(parents=True)
    shutil.copyfile(CONFIGURATION_SOURCE / "parameters/parameter_manifest.yaml", schema)
    shutil.copyfile(
        CONFIGURATION_SOURCE / "parameter_sets/real/tracked/default.yaml", real
    )
    shutil.copyfile(
        CONFIGURATION_SOURCE / "parameter_sets/sim/tracked/default.yaml", sim
    )
    return destination


def _canonical_identity(value: dict) -> str:
    unsigned = {key: item for key, item in value.items() if key != "manifest_id"}
    return hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _contract_variant(
    tmp_path: Path, name: str, *, extra_default: float | None
) -> Path:
    root = _materialize_contract(tmp_path / name)
    schema_path = root / "schema/parameter_manifest.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    if extra_default is None:
        schema.pop("receiver_reconciliation_fixture", None)
    else:
        schema["receiver_reconciliation_fixture"] = {
            "retired_gain": {
                "type": "float",
                "value": extra_default,
                "min": 0.0,
                "max": 10.0,
            }
        }
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    for profile in ("real", "sim"):
        default_path = root / f"tracked_defaults/{profile}/default.yaml"
        document = yaml.safe_load(default_path.read_text())
        values = document["/**"]["ros__parameters"]
        if extra_default is None:
            values.pop("/receiver_reconciliation_fixture/retired_gain", None)
        else:
            values["/receiver_reconciliation_fixture/retired_gain"] = extra_default
        default_path.write_text(yaml.safe_dump(document, sort_keys=False))
    manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for row in manifest["artifacts"]:
        row["sha256"] = hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
    for row in manifest["tracked_sets"]:
        row["sha256"] = hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
    manifest["manifest_id"] = _canonical_identity(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return root


def _release(root: Path, release_id: str, contract: Path) -> Path:
    destination = (
        root
        / release_id
        / "install/iii_drone_configuration/share/iii_drone_configuration/configuration_contract"
    )
    shutil.copytree(contract, destination, symlinks=False)
    return destination


def _source_checkpoint(
    tmp_path: Path, contract: Path, *, release_id: str, target_id: str
) -> dict:
    from iii_drone_configuration import (
        execute_reconciliation,
        load_installed_contract,
        plan_reconciliation,
        seal_configuration_checkpoint,
    )

    state = tmp_path / "source-state"
    operation_id = "initial-aircraft-config-0001"
    marker = state / ".iii-reconciliation-stage.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema": "iii.configuration-reconciliation-stage/v1",
                "operation_id": operation_id,
                "target_id": target_id,
            }
        )
    )
    plan = plan_reconciliation(
        old_immutable_root=contract,
        new_immutable_root=contract,
        writable_state_root=state,
        operations_root=tmp_path / "initial-operations",
        operation_id=operation_id,
        runtime_profile="real",
        target_id=target_id,
        old_release_id=release_id,
        new_release_id=release_id,
        mode="receiver-staged",
        purpose="activation",
    )
    assert execute_reconciliation(plan).status == "complete"
    marker.unlink()
    tracked = state / "parameter_sets/real/tracked/default.yaml"
    document = yaml.safe_load(tracked.read_text())
    document["/**"]["ros__parameters"]["/control/dt"] = 0.3
    tracked.write_text(yaml.safe_dump(document, sort_keys=False))
    installed = load_installed_contract(contract).contract
    return seal_configuration_checkpoint(
        writable_state_root=state,
        checkpoint_root=tmp_path / "checkpoints",
        target_id=target_id,
        runtime_profile="real",
        schema_version=installed.schema_version,
        release_id=release_id,
        manifest_id=installed.manifest_id,
    )


def test_receiver_plans_read_only_then_reconciles_only_a_private_stage(tmp_path: Path):
    from iii_drone_configuration import verify_configuration_checkpoint

    contract = _materialize_contract(tmp_path / "contract-current")
    releases = tmp_path / "releases"
    checkpoints = tmp_path / "checkpoints"
    old_release = "a" * 64
    new_release = "b" * 64
    target = "aircraft-01"
    _release(releases, old_release, contract)
    new_contract = _release(releases, new_release, contract)
    source = _source_checkpoint(
        tmp_path, contract, release_id=old_release, target_id=target
    )
    source_root = Path(source["path"])
    working = tmp_path / "working"
    shutil.copytree(source_root, working, ignore=shutil.ignore_patterns("checkpoint.json"))
    for path in working.rglob("*"):
        path.chmod(0o770 if path.is_dir() else 0o660)
    working.chmod(0o770)
    tracked_working = working / "parameter_sets/real/tracked/default.yaml"
    working_document = yaml.safe_load(tracked_working.read_text())
    working_document["/**"]["ros__parameters"]["/control/dt"] = 0.4
    tracked_working.write_text(yaml.safe_dump(working_document, sort_keys=False))
    source_inventory = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    release_inventory = {
        path.relative_to(new_contract).as_posix(): path.read_bytes()
        for path in new_contract.rglob("*")
        if path.is_file()
    }
    reconciler = ReceiverConfigurationReconciler(
        releases_root=releases,
        checkpoints_root=checkpoints,
        staging_root=tmp_path / "staging",
        operations_root=tmp_path / "operations",
        active_state_root=working,
        target_id=target,
        runtime_profile="real",
    )

    preflight = reconciler.preflight(
        operation_id="receiver-reconcile-0001",
        release_id=new_release,
        source_checkpoint_id=source["checkpoint_id"],
    )
    assert preflight["ready"] is True
    assert preflight["writes_performed"] == 0
    assert not (tmp_path / "staging").exists()
    assert not (checkpoints / preflight["result_checkpoint_id"]).exists()

    result = reconciler.apply(
        operation_id="receiver-reconcile-0001",
        release_id=new_release,
        source_checkpoint_id=source["checkpoint_id"],
    )
    assert result["result_checkpoint_id"] == preflight["result_checkpoint_id"]
    assert result["only_staged_copy_mutated"] is True
    assert not (tmp_path / "staging/receiver-reconcile-0001").exists()
    verified = verify_configuration_checkpoint(
        checkpoints / result["result_checkpoint_id"]
    )
    assert verified["release_id"] == new_release
    tracked = (
        checkpoints
        / result["result_checkpoint_id"]
        / "parameter_sets/real/tracked/default.yaml"
    )
    assert (
        yaml.safe_load(tracked.read_text())["/**"]["ros__parameters"]["/control/dt"]
        == 0.4
    )
    assert source_inventory == {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert release_inventory == {
        path.relative_to(new_contract).as_posix(): path.read_bytes()
        for path in new_contract.rglob("*")
        if path.is_file()
    }


def test_receiver_preserves_wrapped_runtime_configuration_layout(tmp_path: Path):
    from iii_drone_configuration import verify_configuration_checkpoint

    contract = _materialize_contract(tmp_path / "contract-current")
    releases = tmp_path / "releases"
    checkpoints = tmp_path / "checkpoints"
    old_release = "4" * 64
    new_release = "5" * 64
    target = "aircraft-wrapped"
    _release(releases, old_release, contract)
    _release(releases, new_release, contract)
    flat = _source_checkpoint(
        tmp_path, contract, release_id=old_release, target_id=target
    )
    flat_root = Path(flat["path"])

    wrapped_state = tmp_path / "wrapped-state"
    shutil.copytree(
        flat_root,
        wrapped_state / "iii_drone",
        ignore=shutil.ignore_patterns("checkpoint.json"),
    )
    installed = __import__(
        "iii_drone_configuration", fromlist=["load_installed_contract"]
    ).load_installed_contract(contract).contract
    wrapped = __import__(
        "iii_drone_configuration", fromlist=["seal_configuration_checkpoint"]
    ).seal_configuration_checkpoint(
        writable_state_root=wrapped_state,
        checkpoint_root=checkpoints,
        target_id=target,
        runtime_profile="real",
        schema_version=installed.schema_version,
        release_id=old_release,
        manifest_id=installed.manifest_id,
    )
    working = tmp_path / "working-wrapped"
    shutil.copytree(
        Path(wrapped["path"]),
        working,
        ignore=shutil.ignore_patterns("checkpoint.json"),
    )
    reconciler = ReceiverConfigurationReconciler(
        releases_root=releases,
        checkpoints_root=checkpoints,
        staging_root=tmp_path / "staging",
        operations_root=tmp_path / "operations",
        active_state_root=working,
        target_id=target,
        runtime_profile="real",
    )

    preflight = reconciler.preflight(
        operation_id="receiver-wrapped-0001",
        release_id=new_release,
        source_checkpoint_id=wrapped["checkpoint_id"],
    )
    result = reconciler.apply(
        operation_id="receiver-wrapped-0001",
        release_id=new_release,
        source_checkpoint_id=wrapped["checkpoint_id"],
    )

    assert result["result_checkpoint_id"] == preflight["result_checkpoint_id"]
    checkpoint = checkpoints / result["result_checkpoint_id"]
    verified = verify_configuration_checkpoint(checkpoint)
    assert verified["release_id"] == new_release
    assert (checkpoint / "iii_drone/state/real/contract.json").is_file()
    assert not (checkpoint / "state").exists()


def test_receiver_retries_discard_only_its_authenticated_private_stage(tmp_path: Path):
    contract = _materialize_contract(tmp_path / "contract-current")
    releases = tmp_path / "releases"
    checkpoints = tmp_path / "checkpoints"
    old_release = "c" * 64
    new_release = "d" * 64
    target = "aircraft-02"
    _release(releases, old_release, contract)
    _release(releases, new_release, contract)
    source = _source_checkpoint(
        tmp_path, contract, release_id=old_release, target_id=target
    )
    reconciler = ReceiverConfigurationReconciler(
        releases_root=releases,
        checkpoints_root=checkpoints,
        staging_root=tmp_path / "staging",
        operations_root=tmp_path / "operations",
        target_id=target,
        runtime_profile="real",
    )
    stage = tmp_path / "staging/receiver-retry-0001/state"
    stage.mkdir(parents=True)
    (stage / ".iii-reconciliation-stage.json").write_text(
        json.dumps(
            {
                "schema": "iii.configuration-reconciliation-stage/v1",
                "operation_id": "receiver-retry-0001",
                "target_id": target,
            }
        )
    )
    (stage / "partial").write_text("interrupted")
    result = reconciler.apply(
        operation_id="receiver-retry-0001",
        release_id=new_release,
        source_checkpoint_id=source["checkpoint_id"],
    )
    assert result["result_checkpoint_id"]
    assert not stage.parent.exists()


def test_receiver_review_is_read_only_then_decisions_are_sealed_only_on_apply(
    tmp_path: Path,
):
    from iii_drone_configuration import verify_configuration_checkpoint

    old = _contract_variant(tmp_path, "contract-old", extra_default=2.0)
    removed = _contract_variant(tmp_path, "contract-removed", extra_default=None)
    reintroduced = _contract_variant(
        tmp_path, "contract-reintroduced", extra_default=3.0
    )
    releases = tmp_path / "releases"
    checkpoints = tmp_path / "checkpoints"
    old_release = "1" * 64
    removed_release = "2" * 64
    reintroduced_release = "3" * 64
    target = "aircraft-review"
    _release(releases, old_release, old)
    _release(releases, removed_release, removed)
    _release(releases, reintroduced_release, reintroduced)
    source = _source_checkpoint(tmp_path, old, release_id=old_release, target_id=target)
    reconciler = ReceiverConfigurationReconciler(
        releases_root=releases,
        checkpoints_root=checkpoints,
        staging_root=tmp_path / "staging",
        operations_root=tmp_path / "operations",
        target_id=target,
        runtime_profile="real",
    )
    retired = reconciler.apply(
        operation_id="receiver-retire-review-0001",
        release_id=removed_release,
        source_checkpoint_id=source["checkpoint_id"],
    )
    operation = "receiver-reintroduce-review-0001"
    blocked = reconciler.preflight(
        operation_id=operation,
        release_id=reintroduced_release,
        source_checkpoint_id=retired["result_checkpoint_id"],
    )
    assert blocked["ready"] is False
    assert blocked["writes_performed"] == 0
    assert not (tmp_path / "operations" / operation).exists()
    key = "tracked/default.yaml:/receiver_reconciliation_fixture/retired_gain"
    decisions = {key: "use_old"}
    ready = reconciler.preflight(
        operation_id=operation,
        release_id=reintroduced_release,
        source_checkpoint_id=retired["result_checkpoint_id"],
        decisions=decisions,
    )
    assert ready["ready"] is True
    assert ready["decisions"] == decisions
    assert not (tmp_path / "operations" / operation).exists()
    result = reconciler.apply(
        operation_id=operation,
        release_id=reintroduced_release,
        source_checkpoint_id=retired["result_checkpoint_id"],
        decisions=decisions,
    )
    assert result["result_checkpoint_id"] == ready["result_checkpoint_id"]
    operation_root = tmp_path / "operations" / operation
    assert (operation_root / "reconciliation-review.json").is_file()
    assert (operation_root / "reconciliation-decisions.json").is_file()
    checkpoint = checkpoints / result["result_checkpoint_id"]
    verify_configuration_checkpoint(checkpoint)
    values = yaml.safe_load(
        (checkpoint / "parameter_sets/real/tracked/default.yaml").read_text()
    )["/**"]["ros__parameters"]
    assert values["/receiver_reconciliation_fixture/retired_gain"] == 2.0
