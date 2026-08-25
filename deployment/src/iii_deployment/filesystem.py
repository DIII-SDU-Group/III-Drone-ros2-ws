"""Onboard path, persistence, and storage-reserve contracts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Iterable, Mapping

from .contracts import ContractError


FILESYSTEM_SCHEMA = "iii.filesystem-contract/v1"


@dataclass(frozen=True)
class PathContract:
    path: PurePosixPath
    owner: str
    group: str
    mode: int
    kind: str
    persistence: str


@dataclass(frozen=True)
class FilesystemContract:
    paths: tuple[PathContract, ...]
    protected_release_subpaths: tuple[str, ...]
    host_systemd_units: tuple[str, ...]
    minimum_reserve_bytes: int
    minimum_reserve_percent: float

    @classmethod
    def load(cls, path: Path) -> "FilesystemContract":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid filesystem contract: {exc}") from exc
        if value.get("schema") != FILESYSTEM_SCHEMA:
            raise ContractError(f"unsupported filesystem contract {value.get('schema')!r}")
        paths: list[PathContract] = []
        seen: set[PurePosixPath] = set()
        for item in value.get("paths", []):
            posix = PurePosixPath(item["path"])
            if not posix.is_absolute() or ".." in posix.parts or posix == PurePosixPath("/"):
                raise ContractError(f"unsafe filesystem contract path {posix}")
            if posix in seen:
                raise ContractError(f"duplicate filesystem contract path {posix}")
            seen.add(posix)
            mode_text = item["mode"]
            if not isinstance(mode_text, str) or not __import__("re").fullmatch(r"0[0-7]{3}", mode_text):
                raise ContractError(f"invalid mode for {posix}: {mode_text!r}")
            paths.append(PathContract(posix, item["owner"], item["group"], int(mode_text, 8), item["kind"], item["persistence"]))
        reserve = value["minimum_storage_reserve"]
        return cls(
            tuple(paths), tuple(value["protected_release_subpaths"]),
            tuple(value["host_systemd_units"]), int(reserve["bytes"]), float(reserve["percent"]),
        )

    def under_root(self, target_root: Path, posix: PurePosixPath) -> Path:
        root = target_root.resolve()
        candidate = root.joinpath(*posix.parts[1:])
        if not candidate.is_relative_to(root):
            raise ContractError(f"contract path escapes target root: {posix}")
        return candidate

    def materialize_for_test(self, target_root: Path) -> None:
        """Create contract directories in an isolated root without chowning."""

        if target_root.resolve() == Path("/"):
            raise ContractError("test materialization refuses the host root")
        for item in self.paths:
            if item.kind == "atomic-selector":
                continue
            path = self.under_root(target_root, item.path)
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(item.mode)

    def validate_test_root(self, target_root: Path) -> list[str]:
        errors: list[str] = []
        for item in self.paths:
            path = self.under_root(target_root, item.path)
            if item.kind == "atomic-selector" and not path.exists() and not path.is_symlink():
                continue
            if item.kind != "atomic-selector" and not path.is_dir():
                errors.append(f"{item.path}: expected directory")
                continue
            if path.exists() and stat.S_IMODE(path.lstat().st_mode) != item.mode:
                errors.append(f"{item.path}: mode differs from {item.mode:04o}")
        return errors


@dataclass(frozen=True)
class StorageProjection:
    incoming_bytes: int
    extracted_bytes: int
    receiver_bytes: int
    checkpoint_bytes: int
    diagnostics_bytes: int
    retained_bytes: int

    @property
    def peak_bytes(self) -> int:
        return sum((self.incoming_bytes, self.extracted_bytes, self.receiver_bytes, self.checkpoint_bytes, self.diagnostics_bytes, self.retained_bytes))


def ensure_storage_reserve(
    projection: StorageProjection,
    *,
    available_bytes: int,
    filesystem_bytes: int,
    minimum_bytes: int = 2 * 1024**3,
    minimum_percent: float = 10.0,
) -> int:
    if min(available_bytes, filesystem_bytes, projection.peak_bytes) < 0 or available_bytes > filesystem_bytes:
        raise ContractError("invalid storage accounting")
    reserve = max(minimum_bytes, int(filesystem_bytes * minimum_percent / 100.0))
    remaining = available_bytes - projection.peak_bytes
    if remaining < reserve:
        raise ContractError(
            f"insufficient deployment storage: projected_peak={projection.peak_bytes}, "
            f"available={available_bytes}, remaining={remaining}, required_reserve={reserve}"
        )
    return remaining


def assert_regular_safe_tree(root: Path) -> None:
    """Reject symlinks, special files, and paths escaping a candidate tree."""

    resolved_root = root.resolve(strict=True)
    for directory, names, files in os.walk(resolved_root, followlinks=False):
        base = Path(directory)
        for name in (*names, *files):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ContractError(f"unsafe candidate filesystem entry: {path.relative_to(resolved_root)}")
            if not path.resolve().is_relative_to(resolved_root):
                raise ContractError(f"candidate filesystem entry escapes root: {path}")

