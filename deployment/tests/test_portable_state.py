from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tarfile

import pytest

from iii_deployment.portable_state import (
    PortableBackupController,
    PortableSecretError,
    PortableStateConflict,
    PortableStateError,
    SalvageError,
    inspect_archive,
    inspect_salvage_device,
    load_policy,
    read_only_mount,
    salvage_record,
    validate_external_receipt,
    _validate_policy_binding,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deployment/portable-state-policy.json"


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _state(root: Path) -> None:
    _json(
        root / "var/lib/iii/configuration/checkpoints/base.json",
        {"schema": "iii.configuration-checkpoint/v1", "gain": 1.5},
    )
    _json(
        root / "var/lib/iii/tuning/journals/session.json",
        {"schema": "iii.tuning-journal/v1", "operator_action": "accepted"},
    )
    _json(
        root / "var/lib/iii/px4/backups/params.json",
        {"schema": "iii.px4-parameter-snapshot/v1", "parameters": []},
    )
    _json(
        root / "var/lib/iii/deployment/activation/health.json",
        {"schema": "iii.activation-health/v1", "healthy": True},
    )


def _controller(
    root: Path,
    *,
    quiesce=None,
    resume=None,
    clean=True,
    health=True,
) -> PortableBackupController:
    return PortableBackupController(
        source_root=root,
        policy_path=POLICY,
        logical_target="drone",
        profile="real",
        active_release_id=lambda: "a" * 64,
        maintenance_safe=lambda: True,
        quiesce_writers=quiesce or (lambda: {"writers_stopped": True, "flushed": True}),
        resume_standby=resume or (lambda: {"standby_resumed": True}),
        clean_converged_host=lambda: clean,
        reconcile_restore=lambda path, _manifest: {
            "compatible": True,
            "staged_root": str(path),
            "schema_actions": [],
        },
        validate_health=lambda: {"healthy": health},
        now=lambda: "2026-08-27T12:00:00Z",
    )


def test_policy_declares_every_domain_and_exclusion_boundary() -> None:
    policy = load_policy(POLICY)
    assert [item["name"] for item in policy["domains"]] == [
        "configuration",
        "tuning",
        "px4",
        "hardware",
        "activation-evidence",
        "deployment-audits",
        "diagnostics",
    ]
    assert (
        "var/lib/iii/deployment/activation-transactions"
        in policy["structural_exclusions"]
    )
    assert policy["external_archive_warning_days"] == 30


def test_quiesced_seal_is_deterministic_verified_and_resumed_before_transfer(
    tmp_path: Path,
) -> None:
    _state(tmp_path)
    lifecycle: list[str] = []
    controller = _controller(
        tmp_path,
        quiesce=lambda: lifecycle.append("quiesce")
        or {"writers_stopped": True, "flushed": True},
        resume=lambda: lifecycle.append("resume") or {"standby_resumed": True},
    )
    receipt = controller.seal(operation_id="backup-operation-1")
    assert lifecycle == ["quiesce", "resume"]
    assert receipt["verified"] is True and receipt["external_verified"] is False
    verification = inspect_archive(Path(receipt["archive_path"]))
    assert verification["backup_id"] == receipt["backup_id"]
    assert verification["target_state_hash"] == receipt["target_state_hash"]
    assert {item["name"] for item in verification["manifest"]["domains"]} == {
        "configuration",
        "tuning",
        "px4",
        "hardware",
        "activation-evidence",
        "deployment-audits",
        "diagnostics",
    }
    assert controller.status()["backup_fresh"] is True
    duplicate = controller.seal(operation_id="backup-operation-2")
    assert duplicate["backup_id"] == receipt["backup_id"]
    assert duplicate["duplicate_content"] is True
    assert len(controller.list()) == 1


def test_concurrent_writer_and_failed_resume_fail_closed(tmp_path: Path) -> None:
    _state(tmp_path)

    def mutate_during_quiesce():
        path = tmp_path / "var/lib/iii/configuration/checkpoints/base.json"
        _json(path, {"schema": "iii.configuration-checkpoint/v1", "gain": 2.0})
        return {"writers_stopped": True, "flushed": True}

    with pytest.raises(PortableStateConflict, match="changed"):
        _controller(tmp_path, quiesce=mutate_during_quiesce).seal(
            operation_id="backup-operation-3"
        )
    with pytest.raises(PortableStateError, match="resume"):
        _controller(tmp_path, resume=lambda: {"standby_resumed": False}).seal(
            operation_id="backup-operation-4"
        )


def test_secret_links_special_files_and_tamper_are_rejected(tmp_path: Path) -> None:
    _state(tmp_path)
    secret = tmp_path / "var/lib/iii/diagnostics/credentials/state.json"
    _json(secret, {"safe": True})
    with pytest.raises(PortableSecretError, match="secret-shaped"):
        _controller(tmp_path).seal(operation_id="backup-operation-5")
    secret.unlink()
    secret.parent.rmdir()
    _json(
        tmp_path / "var/lib/iii/diagnostics/report.json",
        {"schema": "iii.diagnostic/v1", "password": "not-allowed"},
    )
    with pytest.raises(PortableSecretError, match="secret-bearing"):
        _controller(tmp_path).seal(operation_id="backup-operation-6")
    (tmp_path / "var/lib/iii/diagnostics/report.json").unlink()
    receipt = _controller(tmp_path).seal(operation_id="backup-operation-7")
    archive = Path(receipt["archive_path"])
    damaged = bytearray(archive.read_bytes())
    with tarfile.open(archive, "r:") as stream:
        member = next(
            item
            for item in stream.getmembers()
            if item.isfile() and item.name != "manifest.json"
        )
    damaged[member.offset_data] ^= 0x01
    archive.write_bytes(damaged)
    with pytest.raises(PortableStateError, match="mismatch"):
        inspect_archive(archive)


def test_restore_is_staged_atomic_health_checked_and_never_restores_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _state(source)
    receipt = _controller(source).seal(operation_id="backup-operation-8")
    archive = Path(receipt["archive_path"])
    _state(target)
    controller = _controller(target)
    plan = controller.plan_restore(archive, operation_id="restore-operation-1")
    result = controller.restore(plan)
    assert result["verified"] is True
    assert result["machine_identity_restored"] is False
    assert result["receiver_transactions_restored"] is False
    selector = Path(result["selector"])
    assert selector.is_symlink()
    assert (selector.resolve() / "configuration/checkpoints/base.json").is_file()


def test_restore_preserves_empty_present_domains_and_is_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _state(source)
    (source / "var/lib/iii/hardware").mkdir(parents=True)
    archive = Path(
        _controller(source).seal(operation_id="backup-operation-empty")["archive_path"]
    )
    _state(target)
    controller = _controller(target)
    plan = controller.plan_restore(archive, operation_id="restore-operation-empty")
    first = controller.restore(plan)
    second = controller.restore(plan)
    assert first["generation_path"] == second["generation_path"]
    assert (Path(second["generation_path"]) / "hardware").is_dir()
    assert Path(second["selector"]).resolve() == Path(second["generation_path"])


def test_manifest_must_bind_every_domain_and_exclusion_to_policy(
    tmp_path: Path,
) -> None:
    _state(tmp_path)
    receipt = _controller(tmp_path).seal(operation_id="backup-operation-policy")
    manifest = receipt["manifest"]
    policy = load_policy(POLICY)
    _validate_policy_binding(manifest, policy)
    omitted = {**manifest, "domains": manifest["domains"][:-1]}
    with pytest.raises(PortableStateError, match="omits"):
        _validate_policy_binding(omitted, policy)
    excluded = json.loads(json.dumps(manifest))
    audit = next(
        domain
        for domain in excluded["domains"]
        if domain["name"] == "deployment-audits"
    )
    audit["files"] = [
        {"path": "transactions/private.json", "bytes": 0, "sha256": "0" * 64}
    ]
    with pytest.raises(PortableStateError, match="not bound"):
        _validate_policy_binding(excluded, policy)


def test_restore_rejects_incompatible_dirty_stale_and_unhealthy_inputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _state(source)
    archive = Path(
        _controller(source).seal(operation_id="backup-operation-9")["archive_path"]
    )
    _state(target)
    with pytest.raises(PortableStateError, match="clean converged"):
        _controller(target, clean=False).plan_restore(
            archive, operation_id="restore-operation-2"
        )
    controller = _controller(target)
    plan = controller.plan_restore(archive, operation_id="restore-operation-3")
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises((PortableStateError, PortableStateConflict)):
        controller.restore(plan)

    source2 = tmp_path / "source2"
    target2 = tmp_path / "target2"
    _state(source2)
    archive2 = Path(
        _controller(source2).seal(operation_id="backup-operation-10")["archive_path"]
    )
    _state(target2)
    unhealthy = _controller(target2, health=False)
    plan2 = unhealthy.plan_restore(archive2, operation_id="restore-operation-4")
    with pytest.raises(PortableStateError, match="health"):
        unhealthy.restore(plan2)
    assert not unhealthy.paths.current_selector.exists()


def test_fresh_external_receipt_is_state_bound() -> None:
    from iii_deployment.contracts import content_identity

    receipt = {
        "schema": "iii.host-backup-receipt/v1",
        "receipt_id": "0" * 64,
        "verified": True,
        "external_verified": True,
        "fresh": True,
        "backup_id": "a" * 64,
        "target_state_hash": "b" * 64,
        "state_marker": "c" * 64,
        "archive_sha256": "e" * 64,
    }
    receipt["receipt_id"] = content_identity(
        {key: value for key, value in receipt.items() if key != "receipt_id"}
    )
    validate_external_receipt(receipt, current_marker="c" * 64)
    with pytest.raises(PortableStateError, match="current"):
        validate_external_receipt(receipt, current_marker="d" * 64)
    receipt["external_verified"] = False
    with pytest.raises(PortableStateError, match="external"):
        validate_external_receipt(receipt)


def test_removed_media_inspection_rejects_running_mounted_and_unknown_layouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = "/dev/disk/by-id/usb-fixture"
    disk = {
        "name": "sdz",
        "path": device,
        "type": "disk",
        "size": 16 * 1024**3,
        "model": "fixture",
        "serial": "serial-1",
        "rm": True,
        "tran": "usb",
        "mountpoints": [None],
        "children": [
            {
                "name": "sdz1",
                "path": "/dev/sdz1",
                "type": "part",
                "size": 512 * 1024**2,
                "fstype": "vfat",
                "mountpoints": [None],
            },
            {
                "name": "sdz2",
                "path": "/dev/sdz2",
                "type": "part",
                "size": 15 * 1024**3,
                "fstype": "ext4",
                "fsver": "1.0",
                "mountpoints": [None],
            },
        ],
    }
    evidence = inspect_salvage_device(device, lsblk={"blockdevices": [disk]})
    assert evidence["root_partition"] == "/dev/sdz2"
    disk["children"][1]["mountpoints"] = ["/media/card"]
    with pytest.raises(SalvageError, match="mounted"):
        inspect_salvage_device(device, lsblk={"blockdevices": [disk]})
    disk["children"][1]["mountpoints"] = [None]
    disk["children"][1]["fstype"] = "btrfs"
    with pytest.raises(SalvageError, match="layout"):
        inspect_salvage_device(device, lsblk={"blockdevices": [disk]})


def test_salvage_record_carries_hashes_omissions_and_recommissioning_notice(
    tmp_path: Path,
) -> None:
    _state(tmp_path)
    controller = _controller(tmp_path)
    device = {
        "stable_path": "/dev/disk/by-id/usb-fixture",
        "resolved_path": "/dev/sdz",
        "fingerprint": "f" * 64,
        "root_partition": "/dev/sdz2",
        "layout": "ubuntu-raspi-single-ext4-root",
    }
    record = salvage_record(
        controller=controller,
        device_evidence=device,
        operation_id="salvage-operation-1",
        omissions=("damaged-diagnostic",),
    )
    assert record["verified"] is True
    assert record["credentials_recovered"] is False
    assert record["recommissioning_required"] is True
    assert "Fresh credentials" in record["operator_notice"]
    assert "damaged-diagnostic" in record["omissions"]
    assert len(record["archive_sha256"]) == 64
    from iii_deployment.contracts import ContractRegistry

    ContractRegistry(ROOT / "deployment/schemas/v1").validate(
        "host-salvage-record", record
    )


def test_read_only_salvage_mount_proves_ro_and_cleans_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    original = Path.read_text

    def read_text(path: Path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return f"1 2 0:1 / {tmp_path / 'mount'} ro,nosuid - ext4 /dev/sdz2 ro\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(KeyboardInterrupt):
        with read_only_mount("/dev/sdz2", tmp_path / "mount"):
            raise KeyboardInterrupt
    assert [item[0] for item in calls] == ["e2fsck", "mount", "umount"]
    assert "ro,noload,nodev,nosuid,noexec" in calls[1]
    assert not (tmp_path / "mount").exists()


def test_read_only_salvage_mount_refuses_inconsistent_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 4, b"", b""),
    )
    with pytest.raises(SalvageError, match="inconsistent"):
        with read_only_mount("/dev/sdz2", tmp_path / "mount"):
            pass
    assert not (tmp_path / "mount").exists()
