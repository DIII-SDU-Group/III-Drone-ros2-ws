from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, content_identity
from iii_deployment.portable_state import PortableBackupController
from iii_deployment.receiver.upload import BackupUploadStore


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deployment/portable-state-policy.json"
BACKUP = "a" * 64
CLIENT = "b" * 64


def _archive(tmp_path: Path) -> tuple[str, bytes]:
    state = tmp_path / "source/var/lib/iii/configuration/checkpoints/base.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    controller = PortableBackupController(
        source_root=tmp_path / "source",
        policy_path=POLICY,
        logical_target="drone",
        profile="real",
        active_release_id=lambda: "c" * 64,
        maintenance_safe=lambda: True,
        quiesce_writers=lambda: {"writers_stopped": True},
        resume_standby=lambda: {"standby_resumed": True},
        now=lambda: "2026-08-27T12:00:00Z",
    )
    receipt = controller.seal(operation_id="backup-upload-fixture")
    return receipt["backup_id"], Path(receipt["archive_path"]).read_bytes()


def _manifest(backup_id: str, raw: bytes) -> dict:
    value = {
        "schema": "iii.portable-backup-upload/v1",
        "upload_id": "0" * 64,
        "backup_id": backup_id,
        "client_id": CLIENT,
        "archive": {
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    value["upload_id"] = content_identity(
        {key: item for key, item in value.items() if key != "upload_id"}
    )
    return value


def test_portable_backup_upload_resumes_verifies_and_converges_duplicate(
    tmp_path: Path,
) -> None:
    backup_id, raw = _archive(tmp_path)
    store = BackupUploadStore(
        tmp_path / "incoming", lock_path=tmp_path / "run/upload.lock"
    )
    manifest = _manifest(backup_id, raw)
    first = store.begin(manifest, backup_id=backup_id, client_id=CLIENT)
    assert first["state"] == "partial" and first["archive"]["size"] == 0
    partial = store._partial(backup_id) / "portable-state.tar"
    partial.write_bytes(raw[:1024])
    resumed = store.begin(manifest, backup_id=backup_id, client_id=CLIENT)
    assert resumed["resumed"] is True
    assert resumed["archive"] == {"size": 1024, "sha256": None}
    with partial.open("ab") as stream:
        stream.write(raw[1024:])
    complete = store.finalize(backup_id=backup_id, client_id=CLIENT)
    assert complete["state"] == "complete"
    assert complete["archive"]["sha256"] == manifest["archive"]["sha256"]
    duplicate = store.begin(manifest, backup_id=backup_id, client_id=CLIENT)
    assert duplicate["state"] == "complete" and duplicate["resumed"] is True


def test_portable_backup_upload_rejects_changed_partial_tamper_and_wrong_content_id(
    tmp_path: Path,
) -> None:
    backup_id, raw = _archive(tmp_path)
    store = BackupUploadStore(
        tmp_path / "incoming", lock_path=tmp_path / "run/upload.lock"
    )
    manifest = _manifest(backup_id, raw)
    store.begin(manifest, backup_id=backup_id, client_id=CLIENT)
    changed = json.loads(json.dumps(manifest))
    changed["archive"]["size"] += 1
    changed["upload_id"] = content_identity(
        {key: item for key, item in changed.items() if key != "upload_id"}
    )
    with pytest.raises(ContractError, match="identity changed"):
        store.begin(changed, backup_id=backup_id, client_id=CLIENT)
    archive = store._partial(backup_id) / "portable-state.tar"
    archive.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0x01]))
    with pytest.raises(ContractError, match="differs"):
        store.finalize(backup_id=backup_id, client_id=CLIENT)

    wrong = _manifest("d" * 64, raw)
    other = BackupUploadStore(tmp_path / "other", lock_path=tmp_path / "run/other.lock")
    other.begin(wrong, backup_id="d" * 64, client_id=CLIENT)
    (other._partial("d" * 64) / "portable-state.tar").write_bytes(raw)
    with pytest.raises(ContractError, match="content identity"):
        other.finalize(backup_id="d" * 64, client_id=CLIENT)
