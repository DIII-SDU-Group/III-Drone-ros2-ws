"""Unprivileged, content-bound resumable bundle upload state.

The SSH account may write only below the incoming root.  This module never
stages or trusts a release; it proves that a resumable partial still describes
the same complete local bundle and atomically exposes a completed upload for
the root receiver to independently verify and claim.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, Callable, Iterator, Mapping

from iii_deployment.bundle import COMPONENT_FILES
from iii_deployment.contracts import ContractError, canonical_json, content_identity
from iii_deployment.receiver.protocol import IDENTITY
from iii_deployment.receiver.state import atomic_document
from iii_deployment.staging import STATUS_INDEX_NAME


UPLOAD_SCHEMA = "iii.bundle-upload/v1"
ACTIVITY_SCHEMA = "iii.bundle-upload-activity/v1"
RESULT_SCHEMA = "iii.bundle-upload-result/v1"
MANIFEST_NAME = ".upload-manifest.json"
ACTIVITY_NAME = ".upload-activity.json"
PARTIAL_SUFFIX = ".partial"
EXPIRY_S = 7 * 24 * 60 * 60
HASH = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
CLOCK_TRUST_PATH = Path("/run/iii/clock-trust.json")
LOCK_PATH = Path("/run/iii/deployment-upload.lock")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ContractError("upload content contains a non-regular file")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _canonical(path: Path, *, label: str) -> dict[str, Any]:
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


def _boot_id() -> str:
    try:
        value = BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ContractError(f"cannot read upload boot identity: {exc}") from exc
    if not value:
        raise ContractError("upload boot identity is empty")
    return value


def _clock_trusted(path: Path = CLOCK_TRUST_PATH) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        value = _canonical(path, label="clock trust state")
    except ContractError:
        return False
    return value == {"schema": "iii.clock-trust/v1", "trusted": True}


class UploadStore:
    """Manage one fixed incoming root without privileged release authority."""

    def __init__(
        self,
        root: Path,
        *,
        lock_path: Path = LOCK_PATH,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time_ns: Callable[[], int] = time.time_ns,
        boot_id: Callable[[], str] = _boot_id,
        wall_clock_trusted: Callable[[], bool] = _clock_trusted,
    ) -> None:
        self.root = root.absolute()
        self.lock_path = lock_path
        self.monotonic = monotonic
        self.wall_time_ns = wall_time_ns
        self.boot_id = boot_id
        self.wall_clock_trusted = wall_clock_trusted
        if self.root.is_symlink():
            raise ContractError("incoming upload root is linked")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise ContractError("incoming upload root is not a fixed directory")
        observed = self.root.stat(follow_symlinks=False)
        self._root_identity = (observed.st_dev, observed.st_ino)

    def _assert_root(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ContractError("incoming upload root changed or became unsafe")
        observed = self.root.stat(follow_symlinks=False)
        if (observed.st_dev, observed.st_ino) != self._root_identity:
            raise ContractError("incoming upload root identity changed")

    @contextmanager
    def locked(self, *, nonblocking: bool = False) -> Iterator[int]:
        self._assert_root()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            os.close(descriptor)
            raise ContractError("an upload session is currently active") from None
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def partial_path(self, release_id: str) -> Path:
        self._release_id(release_id)
        return self.root / f"{release_id}{PARTIAL_SUFFIX}"

    def complete_path(self, release_id: str) -> Path:
        self._release_id(release_id)
        return self.root / release_id

    @staticmethod
    def _release_id(value: Any) -> str:
        if not isinstance(value, str) or not HASH.fullmatch(value):
            raise ContractError("invalid upload release identity")
        return value

    @staticmethod
    def validate_manifest(
        value: Mapping[str, Any], *, release_id: str, client_id: str
    ) -> dict[str, Any]:
        if (
            set(value)
            != {
                "schema",
                "upload_id",
                "release_id",
                "client_id",
                "files",
            }
            or value.get("schema") != UPLOAD_SCHEMA
        ):
            raise ContractError("upload manifest fields are malformed")
        if value.get("release_id") != release_id:
            raise ContractError("upload manifest release identity mismatch")
        if value.get("client_id") != client_id or not IDENTITY.fullmatch(client_id):
            raise ContractError("upload manifest client identity mismatch")
        files = value.get("files")
        if not isinstance(files, list) or not files:
            raise ContractError("upload manifest file index is empty")
        paths = [item.get("path") for item in files if isinstance(item, dict)]
        allowed = {f"drone/{name}" for name in COMPONENT_FILES} | {STATUS_INDEX_NAME}
        required = {f"drone/{name}" for name in COMPONENT_FILES}
        if (
            len(paths) != len(files)
            or paths != sorted(set(paths), key=lambda item: item.encode("utf-8"))
            or not required.issubset(paths)
            or not set(paths).issubset(allowed)
        ):
            raise ContractError("upload manifest has an unexpected file index")
        for item in files:
            if set(item) != {"path", "size", "sha256"}:
                raise ContractError("upload file metadata fields are malformed")
            if (
                isinstance(item["size"], bool)
                or not isinstance(item["size"], int)
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not HASH.fullmatch(item["sha256"])
            ):
                raise ContractError("upload file metadata is invalid")
        expected = content_identity(
            {key: item for key, item in value.items() if key != "upload_id"}
        )
        if value.get("upload_id") != expected:
            raise ContractError("upload manifest identity mismatch")
        return dict(value)

    def begin(
        self, manifest: Mapping[str, Any], *, release_id: str, client_id: str
    ) -> dict[str, Any]:
        manifest = self.validate_manifest(
            manifest, release_id=release_id, client_id=client_id
        )
        with self.locked():
            complete = self.complete_path(release_id)
            if complete.exists() or complete.is_symlink():
                self._verify_complete(complete, manifest)
                return self._status(manifest, complete=True, resumed=True)
            partial = self.partial_path(release_id)
            resumed = partial.exists() or partial.is_symlink()
            if resumed:
                if partial.is_symlink() or not partial.is_dir():
                    raise ContractError("upload partial path is unsafe")
                retained = _canonical(
                    partial / MANIFEST_NAME, label="retained upload manifest"
                )
                if retained != manifest:
                    raise ContractError(
                        "remote partial belongs to another bundle identity"
                    )
            else:
                partial.mkdir(mode=0o700)
                (partial / "drone").mkdir(mode=0o700)
                atomic_document(partial / MANIFEST_NAME, manifest, mode=0o600)
            self._touch_locked(partial, manifest)
            return self._status(manifest, complete=False, resumed=resumed)

    def touch(self, *, release_id: str, client_id: str) -> dict[str, Any]:
        with self.locked():
            partial, manifest = self._partial_manifest(release_id, client_id)
            self._touch_locked(partial, manifest)
            return self._status(manifest, complete=False, resumed=True)

    def inspect(self, *, release_id: str, client_id: str) -> dict[str, Any]:
        with self.locked():
            complete = self.complete_path(release_id)
            if complete.exists() or complete.is_symlink():
                raise ContractError(
                    "completed upload inspection requires identity-bound begin"
                )
            partial, manifest = self._partial_manifest(release_id, client_id)
            return self._status(manifest, complete=False, resumed=True)

    def finalize(self, *, release_id: str, client_id: str) -> dict[str, Any]:
        with self.locked():
            complete = self.complete_path(release_id)
            if complete.exists() or complete.is_symlink():
                raise ContractError("completed upload already exists")
            partial, manifest = self._partial_manifest(release_id, client_id)
            self._verify_complete(partial, manifest, partial=True)
            for name in (ACTIVITY_NAME, MANIFEST_NAME):
                (partial / name).unlink()
            self._fsync_directory(partial / "drone")
            self._fsync_directory(partial)
            os.replace(partial, complete)
            self._fsync_directory(self.root)
            return self._status(manifest, complete=True, resumed=True)

    def cleanup(self) -> dict[str, Any]:
        removed: list[str] = []
        retained: list[str] = []
        with self.locked(nonblocking=True):
            for path in sorted(self.root.glob(f"*{PARTIAL_SUFFIX}")):
                release_id = path.name[: -len(PARTIAL_SUFFIX)]
                try:
                    self._release_id(release_id)
                    if path.is_symlink() or not path.is_dir():
                        raise ContractError("upload cleanup found an unsafe partial")
                    activity = _canonical(path / ACTIVITY_NAME, label="upload activity")
                    stale = self._stale(activity, release_id=release_id)
                    if not stale:
                        retained.append(release_id)
                        continue
                    self._assert_safe_tree(path)
                    shutil.rmtree(path)
                    removed.append(release_id)
                except ContractError:
                    retained.append(release_id)
            if removed:
                self._fsync_directory(self.root)
        return {
            "schema": RESULT_SCHEMA,
            "state": "cleanup-complete",
            "removed_release_ids": removed,
            "retained_release_ids": retained,
        }

    def _partial_manifest(
        self, release_id: str, client_id: str
    ) -> tuple[Path, dict[str, Any]]:
        partial = self.partial_path(release_id)
        if partial.is_symlink() or not partial.is_dir():
            raise ContractError("upload partial is unavailable or unsafe")
        manifest = _canonical(partial / MANIFEST_NAME, label="upload manifest")
        return partial, self.validate_manifest(
            manifest, release_id=release_id, client_id=client_id
        )

    def _touch_locked(
        self, partial: Path, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": ACTIVITY_SCHEMA,
            "activity_id": "0" * 64,
            "release_id": manifest["release_id"],
            "upload_id": manifest["upload_id"],
            "client_id": manifest["client_id"],
            "boot_id": self.boot_id(),
            "monotonic": self.monotonic(),
            "wall_time_ns": self.wall_time_ns(),
            "wall_clock_trusted": self.wall_clock_trusted(),
        }
        value["activity_id"] = content_identity(
            {key: item for key, item in value.items() if key != "activity_id"}
        )
        atomic_document(partial / ACTIVITY_NAME, value, mode=0o600)
        return value

    def _stale(self, activity: Mapping[str, Any], *, release_id: str) -> bool:
        expected = {
            "schema",
            "activity_id",
            "release_id",
            "upload_id",
            "client_id",
            "boot_id",
            "monotonic",
            "wall_time_ns",
            "wall_clock_trusted",
        }
        if (
            set(activity) != expected
            or activity.get("schema") != ACTIVITY_SCHEMA
            or activity.get("release_id") != release_id
            or activity.get("activity_id")
            != content_identity(
                {key: item for key, item in activity.items() if key != "activity_id"}
            )
        ):
            raise ContractError("upload activity is malformed")
        current_boot = self.boot_id()
        if activity["boot_id"] == current_boot:
            observed = activity["monotonic"]
            now = self.monotonic()
            return (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and now >= observed
                and now - observed >= EXPIRY_S
            )
        if self.wall_clock_trusted() and activity["wall_clock_trusted"] is True:
            observed_ns = activity["wall_time_ns"]
            now_ns = self.wall_time_ns()
            return (
                isinstance(observed_ns, int)
                and not isinstance(observed_ns, bool)
                and now_ns >= observed_ns
                and now_ns - observed_ns >= EXPIRY_S * 1_000_000_000
            )
        return False

    def _status(
        self,
        manifest: Mapping[str, Any],
        *,
        complete: bool,
        resumed: bool,
    ) -> dict[str, Any]:
        root = (
            self.complete_path(manifest["release_id"])
            if complete
            else self.partial_path(manifest["release_id"])
        )
        files: dict[str, dict[str, Any]] = {}
        for item in manifest["files"]:
            path = root.joinpath(*item["path"].split("/"))
            if not path.exists() and not path.is_symlink():
                files[item["path"]] = {"size": 0, "sha256": None}
                continue
            if path.is_symlink() or not path.is_file():
                raise ContractError("remote upload contains an unsafe file")
            size = path.stat(follow_symlinks=False).st_size
            files[item["path"]] = {
                "size": size,
                "sha256": _sha256(path) if size == item["size"] else None,
            }
        return {
            "schema": RESULT_SCHEMA,
            "release_id": manifest["release_id"],
            "upload_id": manifest["upload_id"],
            "state": "complete" if complete else "partial",
            "resumed": resumed,
            "files": files,
        }

    def _verify_complete(
        self,
        root: Path,
        manifest: Mapping[str, Any],
        *,
        partial: bool = False,
    ) -> None:
        if root.is_symlink() or not root.is_dir():
            raise ContractError("completed upload root is unsafe")
        expected = {item["path"]: item for item in manifest["files"]}
        allowed_root = {"drone"}
        if STATUS_INDEX_NAME in expected:
            allowed_root.add(STATUS_INDEX_NAME)
        if partial:
            allowed_root |= {MANIFEST_NAME, ACTIVITY_NAME}
        if {path.name for path in root.iterdir()} != allowed_root:
            raise ContractError("upload contains missing or extra root entries")
        drone = root / "drone"
        if drone.is_symlink() or not drone.is_dir():
            raise ContractError("upload drone component is unavailable")
        if {path.name for path in drone.iterdir()} != COMPONENT_FILES:
            raise ContractError("upload drone component file set is incomplete")
        for relative, item in expected.items():
            path = root.joinpath(*relative.split("/"))
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat(follow_symlinks=False).st_size != item["size"]
                or _sha256(path) != item["sha256"]
            ):
                raise ContractError(
                    f"upload file differs from local identity: {relative}"
                )
        release = _canonical(
            drone / "release-manifest.json", label="uploaded release manifest"
        )
        if release.get("release_id") != manifest["release_id"]:
            raise ContractError("uploaded release manifest identity mismatch")

    @staticmethod
    def _assert_safe_tree(root: Path) -> None:
        for current, directories, files in os.walk(root, followlinks=False):
            base = Path(current)
            for name in (*directories, *files):
                path = base / name
                observed = path.lstat()
                if path.is_symlink() or not (
                    stat.S_ISDIR(observed.st_mode) or stat.S_ISREG(observed.st_mode)
                ):
                    raise ContractError("upload cleanup found an unsafe tree")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
