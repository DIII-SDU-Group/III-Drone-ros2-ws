from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from iii_deployment.bundle import COMPONENT_FILES
from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.receiver.ssh_gateway import dispatch, restrict_writes_to
from iii_deployment.receiver.upload import (
    EXPIRY_S,
    ReceiverUpdateUploadStore,
    UploadStore,
)


RELEASE = "a" * 64
CLIENT = "b" * 64
REGISTRY = ContractRegistry(Path(__file__).resolve().parents[1] / "schemas/v1")


class Clock:
    def __init__(self) -> None:
        self.monotonic_value = 100.0
        self.wall_value = 1_800_000_000_000_000_000
        self.boot = "boot-a"
        self.trusted = False


def _store(tmp_path: Path, clock: Clock) -> UploadStore:
    return UploadStore(
        tmp_path / "incoming",
        lock_path=tmp_path / "run/upload.lock",
        monotonic=lambda: clock.monotonic_value,
        wall_time_ns=lambda: clock.wall_value,
        boot_id=lambda: clock.boot,
        wall_clock_trusted=lambda: clock.trusted,
    )


def _manifest() -> dict:
    files = []
    for name in sorted(COMPONENT_FILES):
        content = (
            canonical_json({"release_id": RELEASE}) + b"\n"
            if name == "release-manifest.json"
            else f"content:{name}\n".encode()
        )
        files.append(
            {
                "path": f"drone/{name}",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    value = {
        "schema": "iii.bundle-upload/v1",
        "upload_id": "0" * 64,
        "release_id": RELEASE,
        "client_id": CLIENT,
        "files": files,
    }
    value["upload_id"] = content_identity(
        {key: item for key, item in value.items() if key != "upload_id"}
    )
    return value


def _write_bundle(root: Path, manifest: dict, *, corrupt: str | None = None) -> None:
    for item in manifest["files"]:
        path = root / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        name = path.name
        content = (
            canonical_json({"release_id": RELEASE}) + b"\n"
            if name == "release-manifest.json"
            else f"content:{name}\n".encode()
        )
        path.write_bytes(b"corrupt" if name == corrupt else content)


def _receiver_manifest() -> dict:
    files = []
    for name in (
        "receiver-update.manifest.json",
        "receiver-update.sig.json",
        "receiver-update.tar",
    ):
        content = f"receiver:{name}\n".encode()
        files.append(
            {
                "path": f"bundle/{name}",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    value = {
        "schema": "iii.receiver-update-upload/v1",
        "upload_id": "0" * 64,
        "receiver_id": RELEASE,
        "client_id": CLIENT,
        "files": files,
    }
    value["upload_id"] = content_identity(
        {key: item for key, item in value.items() if key != "upload_id"}
    )
    return value


def test_receiver_update_upload_is_resumable_and_exposes_exact_signed_bundle(
    tmp_path: Path,
) -> None:
    store = ReceiverUpdateUploadStore(
        tmp_path / "incoming", lock_path=tmp_path / "run/upload.lock"
    )
    manifest = _receiver_manifest()
    first = store.begin(manifest, receiver_id=RELEASE, client_id=CLIENT)
    REGISTRY.validate("receiver-update-upload", manifest)
    REGISTRY.validate("receiver-update-upload-result", first)
    assert first["state"] == "partial"
    partial = store.partial_path(RELEASE)
    first_file = manifest["files"][0]
    path = partial / first_file["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"receiver:")
    resumed = store.begin(manifest, receiver_id=RELEASE, client_id=CLIENT)
    assert resumed["resumed"] is True
    assert resumed["files"][first_file["path"]] == {
        "size": len(b"receiver:"),
        "sha256": None,
    }
    for item in manifest["files"]:
        destination = partial / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"receiver:{destination.name}\n".encode())
    completed = store.finalize(receiver_id=RELEASE, client_id=CLIENT)
    REGISTRY.validate("receiver-update-upload-result", completed)
    assert completed["state"] == "complete"
    root = store.complete_path(RELEASE)
    assert {path.name for path in root.iterdir()} == {
        ".upload-manifest.json",
        "bundle",
    }
    assert {path.name for path in (root / "bundle").iterdir()} == {
        "receiver-update.manifest.json",
        "receiver-update.sig.json",
        "receiver-update.tar",
    }


def test_partial_resume_requires_exact_manifest_and_remote_size_hash_agreement(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    manifest = _manifest()
    first = store.begin(manifest, release_id=RELEASE, client_id=CLIENT)
    REGISTRY.validate("bundle-upload", manifest)
    activity = __import__("json").loads(
        (store.partial_path(RELEASE) / ".upload-activity.json").read_bytes()
    )
    REGISTRY.validate("bundle-upload-activity", activity)
    REGISTRY.validate("bundle-upload-result", first)
    assert first["state"] == "partial" and first["resumed"] is False
    partial = store.partial_path(RELEASE)
    archive = next(
        item for item in manifest["files"] if item["path"] == "drone/bundle.tar.zst"
    )
    (partial / archive["path"]).write_bytes(b"con")
    resumed = store.begin(manifest, release_id=RELEASE, client_id=CLIENT)
    assert resumed["resumed"] is True
    assert resumed["files"][archive["path"]] == {"size": 3, "sha256": None}
    changed = {**manifest, "files": [dict(item) for item in manifest["files"]]}
    changed["files"][0]["size"] += 1
    changed["upload_id"] = content_identity(
        {key: item for key, item in changed.items() if key != "upload_id"}
    )
    with pytest.raises(ContractError, match="another bundle identity"):
        store.begin(changed, release_id=RELEASE, client_id=CLIENT)


def test_finalize_hashes_every_file_and_exposes_only_exact_complete_bundle(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    manifest = _manifest()
    store.begin(manifest, release_id=RELEASE, client_id=CLIENT)
    _write_bundle(store.partial_path(RELEASE), manifest, corrupt="bundle.tar.zst")
    with pytest.raises(ContractError, match="differs from local identity"):
        store.finalize(release_id=RELEASE, client_id=CLIENT)
    _write_bundle(store.partial_path(RELEASE), manifest)
    result = store.finalize(release_id=RELEASE, client_id=CLIENT)
    REGISTRY.validate("bundle-upload-result", result)
    assert result["state"] == "complete"
    complete = store.complete_path(RELEASE)
    assert {path.name for path in complete.iterdir()} == {"drone"}
    assert not store.partial_path(RELEASE).exists()


def test_cleanup_uses_same_boot_monotonic_or_trusted_cross_boot_wall_evidence(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    manifest = _manifest()
    store.begin(manifest, release_id=RELEASE, client_id=CLIENT)
    clock.monotonic_value += EXPIRY_S - 1
    retained = store.cleanup()
    REGISTRY.validate("bundle-upload-result", retained)
    assert retained["removed_release_ids"] == []
    clock.monotonic_value += 1
    assert store.cleanup()["removed_release_ids"] == [RELEASE]

    store.begin(manifest, release_id=RELEASE, client_id=CLIENT)
    clock.boot = "boot-b"
    clock.monotonic_value = 1.0
    clock.wall_value += (EXPIRY_S + 1) * 1_000_000_000
    assert store.cleanup()["removed_release_ids"] == []
    clock.boot = "boot-a"
    clock.trusted = True
    store.touch(release_id=RELEASE, client_id=CLIENT)
    clock.boot = "boot-c"
    clock.wall_value += (EXPIRY_S + 1) * 1_000_000_000
    assert store.cleanup()["removed_release_ids"] == [RELEASE]


def test_active_transfer_lock_prevents_cleanup_and_gateway_rejects_shell_input(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.begin(_manifest(), release_id=RELEASE, client_id=CLIENT)
    with store.locked():
        with pytest.raises(ContractError, match="currently active"):
            store.cleanup()
    with pytest.raises(ContractError, match="outside the fixed deployment gateway"):
        dispatch(
            client_id=CLIENT,
            original_command="sh -c id",
            incoming_root=tmp_path / "incoming",
            lock_path=tmp_path / "run/upload.lock",
        )


def test_upload_root_link_or_replacement_fails_closed(tmp_path: Path) -> None:
    linked = tmp_path / "linked-incoming"
    target = tmp_path / "target"
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ContractError, match="linked"):
        UploadStore(linked, lock_path=tmp_path / "run/linked.lock")

    clock = Clock()
    store = _store(tmp_path, clock)
    retained = store.root.with_name("retained-incoming")
    store.root.rename(retained)
    store.root.mkdir()
    with pytest.raises(ContractError, match="identity changed"):
        store.cleanup()


def test_gateway_execs_only_fixed_sftp_server_root_and_denies_links(
    tmp_path: Path,
) -> None:
    server = tmp_path / "usr/lib/openssh/sftp-server"
    server.parent.mkdir(parents=True)
    server.write_text("binary", encoding="ascii")
    incoming = tmp_path / "incoming"
    calls = []

    def execute(path, argv, environment):
        calls.append((path, argv, environment))
        raise RuntimeError("exec intercepted")

    with pytest.raises(RuntimeError, match="intercepted"):
        dispatch(
            client_id=CLIENT,
            original_command="internal-sftp",
            incoming_root=incoming,
            lock_path=tmp_path / "run/upload.lock",
            sftp_server=server,
            execve=execute,
            write_restrictor=lambda _root: None,
        )
    assert calls == [
        (
            str(server),
            [
                str(server),
                "-d",
                str(incoming),
                "-u",
                "027",
                "-P",
                "symlink,hardlink",
            ],
            {"PATH": "/usr/bin:/bin"},
        )
    ]


def test_landlock_sftp_boundary_allows_incoming_and_denies_sibling_write(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    outside = tmp_path / "configuration"
    outside.mkdir()
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_descriptor)
        result = []
        try:
            restrict_writes_to(incoming)
            (incoming / "allowed").write_text("ok", encoding="ascii")
            result.append("inside-ok")
            try:
                (outside / "denied").write_text("bad", encoding="ascii")
            except PermissionError:
                result.append("outside-denied")
        except Exception as exc:  # pragma: no cover - reported to parent
            result.append(f"error:{type(exc).__name__}:{exc}")
        os.write(write_descriptor, "\n".join(result).encode("utf-8"))
        os._exit(0)
    os.close(write_descriptor)
    observed = os.read(read_descriptor, 4096).decode("utf-8")
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == "inside-ok\noutside-denied"
    assert (incoming / "allowed").read_text(encoding="ascii") == "ok"
    assert not (outside / "denied").exists()
