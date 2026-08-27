"""Coordinated portable host-state backup, restore, and read-only salvage.

The archive is deliberately narrower than ``/var/lib/iii``.  A tracked policy
declares each portable domain, and the implementation refuses links, special
files, secret-shaped paths/content, receiver transactions, selectors, and host
identity.  Both normal backup and removed-media salvage produce the same archive
format and pass the same verifier.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_identity


POLICY_SCHEMA = "iii.portable-state-policy/v1"
MANIFEST_SCHEMA = "iii.portable-backup-manifest/v1"
RECEIPT_SCHEMA = "iii.host-backup-receipt/v1"
SALVAGE_SCHEMA = "iii.host-salvage-record/v1"
RESTORE_PLAN_SCHEMA = "iii.portable-restore-plan/v1"
RESTORE_RESULT_SCHEMA = "iii.portable-restore-result/v1"
HASH = re.compile(r"^[a-f0-9]{64}$")
DOMAIN_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_FILE_BYTES = 16 * 1024**3
MAX_ARCHIVE_MEMBERS = 100_000
MAX_MANIFEST_BYTES = 64 * 1024**2
DEFAULT_CHUNK_BYTES = 4 * 1024**2

PROHIBITED_PARTS = frozenset(
    {
        ".ssh",
        "authorized_keys",
        "credentials",
        "machine-id",
        "private-key",
        "private_key",
        "secrets",
        "selectors",
        "system-connections",
        "transactions",
        "wifi",
    }
)
PROHIBITED_KEYS = frozenset(
    {
        "api_key",
        "bootstrap_credential",
        "credential",
        "credentials",
        "machine_id",
        "password",
        "passphrase",
        "private_key",
        "private_key_path",
        "psk",
        "runtime_api_token",
        "secret",
        "signing_key",
        "ssh_private_key",
        "token",
        "wifi_password",
        "wifi_psk",
    }
)
PRIVATE_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)
SECRET_ASSIGNMENT = re.compile(
    rb"(?im)(?:^|\n)\s*(?:api_key|credential|password|passphrase|private_key|psk|runtime_api_token|secret|token|wifi_password|wifi_psk)\s*[=:]\s*([^\r\n#]+)"
)


class PortableStateError(ContractError):
    code = "III_PORTABLE_STATE_ERROR"


class PortableSecretError(PortableStateError):
    code = "III_PORTABLE_SECRET_REJECTED"


class PortableStateConflict(PortableStateError):
    code = "III_PORTABLE_STATE_CONFLICT"


class SalvageError(PortableStateError):
    code = "III_HOST_SALVAGE_REJECTED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise PortableStateError(f"portable file is not regular: {path}")
        if observed.st_size > MAX_FILE_BYTES:
            raise PortableStateError(f"portable file exceeds the fixed limit: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > MAX_FILE_BYTES:
            raise PortableStateError(
                f"portable source is not a bounded regular file: {path}"
            )
        result = bytearray()
        while len(result) <= MAX_FILE_BYTES:
            block = os.read(
                descriptor, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(result))
            )
            if not block:
                break
            result.extend(block)
        if len(result) != observed.st_size:
            raise PortableStateConflict(
                f"portable source changed while being read: {path}"
            )
        return bytes(result)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.parent.is_symlink() or path.is_symlink():
        raise PortableStateError("portable record path is linked")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = _read_regular(path)
        value = json.loads(raw)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        PortableStateError,
    ) as exc:
        raise PortableStateError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise PortableStateError(f"{label} is not canonical JSON")
    return value


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_policy(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableStateError("portable-state policy is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableStateError(f"cannot load portable-state policy: {exc}") from exc
    if not isinstance(value, dict):
        raise PortableStateError("portable-state policy must be one JSON object")
    if (
        value.get("schema") != POLICY_SCHEMA
        or value.get("portable_schema_version") != 1
    ):
        raise PortableStateError("portable-state policy schema/version is unsupported")
    domains = value.get("domains")
    if not isinstance(domains, list) or not domains:
        raise PortableStateError("portable-state policy declares no domains")
    names: set[str] = set()
    paths: set[str] = set()
    for domain in domains:
        if not isinstance(domain, dict) or set(domain) != {
            "name",
            "path",
            "optional",
            "exclude",
        }:
            raise PortableStateError("portable domain fields are invalid")
        name = domain["name"]
        relative = PurePosixPath(domain["path"])
        if not isinstance(name, str) or not DOMAIN_NAME.fullmatch(name):
            raise PortableStateError("portable domain name is invalid")
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise PortableStateError(f"portable domain path is unsafe: {relative}")
        if name in names or relative.as_posix() in paths:
            raise PortableStateError("portable domain names and paths must be unique")
        if not isinstance(domain["optional"], bool):
            raise PortableStateError("portable domain optional flag must be boolean")
        excludes = domain["exclude"]
        if not isinstance(excludes, list) or any(
            not isinstance(item, str)
            or PurePosixPath(item).is_absolute()
            or ".." in PurePosixPath(item).parts
            for item in excludes
        ):
            raise PortableStateError("portable domain exclusions are unsafe")
        names.add(name)
        paths.add(relative.as_posix())
    if value.get("external_archive_warning_days") != 30:
        raise PortableStateError("external archive warning policy must remain 30 days")
    mutations = value.get("invalidating_mutations")
    if (
        not isinstance(mutations, list)
        or not mutations
        or mutations != sorted(set(mutations))
    ):
        raise PortableStateError("invalidating mutations must be a sorted unique list")
    return value


def policy_id(policy: Mapping[str, Any]) -> str:
    return content_identity(policy)


def _json_secret_paths(value: Any, prefix: str = "") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            locator = f"{prefix}/{name}" if prefix else name
            if name.lower() in PROHIBITED_KEYS:
                issues.append(locator)
            issues.extend(_json_secret_paths(item, locator))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(_json_secret_paths(item, f"{prefix}/{index}"))
    return issues


def assert_secret_free(relative: PurePosixPath, data: bytes) -> None:
    lowered = {part.lower() for part in relative.parts}
    if lowered.intersection(PROHIBITED_PARTS):
        raise PortableSecretError(f"prohibited secret-shaped path: {relative}")
    if any(marker in data for marker in PRIVATE_MARKERS):
        raise PortableSecretError(f"private-key material detected: {relative}")
    if SECRET_ASSIGNMENT.search(data):
        raise PortableSecretError(f"secret assignment detected: {relative}")
    if relative.suffix.lower() in {".json", ".jsonl"}:
        rows = data.splitlines() if relative.suffix.lower() == ".jsonl" else [data]
        for row in rows:
            if not row.strip():
                continue
            try:
                value = json.loads(row)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PortableStateError(
                    f"JSON-shaped portable state is malformed: {relative}: {exc}"
                ) from exc
            issues = _json_secret_paths(value)
            if issues:
                raise PortableSecretError(
                    f"secret-bearing JSON rejected at {relative}: {', '.join(issues[:5])}"
                )


def _excluded(relative: PurePosixPath, exclusions: Sequence[str]) -> bool:
    return any(
        relative == PurePosixPath(item) or relative.is_relative_to(PurePosixPath(item))
        for item in exclusions
    )


def _domain_files(
    root: Path, exclusions: Sequence[str]
) -> list[tuple[PurePosixPath, Path]]:
    if not root.exists() and not root.is_symlink():
        return []
    if root.is_symlink() or not root.is_dir():
        raise PortableStateError(
            f"portable domain root is linked or not a directory: {root}"
        )
    result: list[tuple[PurePosixPath, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _excluded(relative, exclusions):
            continue
        observed = path.lstat()
        if stat.S_ISDIR(observed.st_mode):
            continue
        if not stat.S_ISREG(observed.st_mode) or path.is_symlink():
            raise PortableStateError(
                f"portable state contains a link or special file: {path}"
            )
        result.append((relative, path))
    return result


def state_marker(source_root: Path, policy: Mapping[str, Any]) -> str:
    """Cheap state identity used to invalidate freshness after any declared-tree mutation."""

    rows: list[dict[str, Any]] = []
    for domain in policy["domains"]:
        root = source_root / domain["path"]
        files = _domain_files(root, domain["exclude"])
        rows.append(
            {
                "domain": domain["name"],
                "present": root.is_dir() and not root.is_symlink(),
                "files": [
                    {
                        "path": relative.as_posix(),
                        "size": path.stat(follow_symlinks=False).st_size,
                        "mtime_ns": path.stat(follow_symlinks=False).st_mtime_ns,
                        "ctime_ns": path.stat(follow_symlinks=False).st_ctime_ns,
                    }
                    for relative, path in files
                ],
            }
        )
    return content_identity(
        {
            "policy_id": policy_id(policy),
            "invalidating_mutations": policy["invalidating_mutations"],
            "domains": rows,
        }
    )


def _flush_domains(source_root: Path, policy: Mapping[str, Any]) -> None:
    """Durably flush every declared source file and directory while writers stop."""

    for domain in policy["domains"]:
        root = source_root / domain["path"]
        files = _domain_files(root, domain["exclude"])
        for _relative, path in files:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if root.is_dir() and not root.is_symlink():
            directories = [root]
            for item in root.rglob("*"):
                relative = PurePosixPath(item.relative_to(root).as_posix())
                if _excluded(relative, domain["exclude"]):
                    continue
                if item.is_dir():
                    directories.append(item)
            for directory in sorted(
                directories, key=lambda item: len(item.parts), reverse=True
            ):
                if directory.is_symlink():
                    raise PortableStateError(
                        f"portable state contains a linked directory: {directory}"
                    )
                _fsync_dir(directory)


def _copy_and_inventory(
    source_root: Path, destination: Path, policy: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    domains: list[dict[str, Any]] = []
    for declaration in policy["domains"]:
        name = declaration["name"]
        root = source_root / declaration["path"]
        files = _domain_files(root, declaration["exclude"])
        present = root.is_dir() and not root.is_symlink()
        if not present and not declaration["optional"]:
            raise PortableStateError(
                f"required portable state domain is absent: {name}"
            )
        rows: list[dict[str, Any]] = []
        for relative, source in files:
            data = _read_regular(source)
            assert_secret_free(PurePosixPath(name) / relative, data)
            target = destination / name / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            rows.append(
                {
                    "path": relative.as_posix(),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        domains.append(
            {
                "name": name,
                "source_path": declaration["path"],
                "present": present,
                "optional": declaration["optional"],
                "files": rows,
                "domain_hash": content_identity(rows),
            }
        )
    return domains, content_identity(domains)


def _tar_info(name: str, size: int, *, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    if name.endswith("/"):
        info.type = tarfile.DIRTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_archive(path: Path, manifest: Mapping[str, Any], payload: Path) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        with temporary.open("xb") as output:
            with tarfile.open(
                fileobj=output, mode="w", format=tarfile.USTAR_FORMAT
            ) as archive:
                manifest_bytes = canonical_json(manifest) + b"\n"
                archive.addfile(
                    _tar_info("manifest.json", len(manifest_bytes), mode=0o600),
                    fileobj=_BytesReader(manifest_bytes),
                )
                for path_item in sorted(
                    payload.rglob("*"), key=lambda item: item.as_posix()
                ):
                    relative = path_item.relative_to(payload).as_posix()
                    if path_item.is_dir():
                        archive.addfile(
                            _tar_info(f"portable/{relative}/", 0, mode=0o750)
                        )
                    else:
                        data = path_item.read_bytes()
                        archive.addfile(
                            _tar_info(f"portable/{relative}", len(data), mode=0o600),
                            fileobj=_BytesReader(data),
                        )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._offset
        result = self._data[self._offset : self._offset + size]
        self._offset += len(result)
        return result


def inspect_archive(
    path: Path,
    *,
    expected_policy_id: str | None = None,
    expected_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PortableStateError("portable backup archive is missing or linked")
    observed_members: set[str] = set()
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise PortableStateError("portable backup archive member count is invalid")
        first = members[0]
        if first.name != "manifest.json" or not first.isfile():
            raise PortableStateError(
                "portable backup manifest is not the first regular member"
            )
        if first.size > MAX_MANIFEST_BYTES:
            raise PortableStateError("portable backup manifest exceeds the fixed limit")
        stream = archive.extractfile(first)
        if stream is None:
            raise PortableStateError("portable backup manifest cannot be read")
        raw = stream.read()
        try:
            manifest = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortableStateError(
                f"portable backup manifest is invalid: {exc}"
            ) from exc
        if not isinstance(manifest, dict) or raw != canonical_json(manifest) + b"\n":
            raise PortableStateError("portable backup manifest is not canonical JSON")
        _validate_manifest(
            manifest,
            expected_policy_id=(
                policy_id(expected_policy)
                if expected_policy is not None
                else expected_policy_id
            ),
        )
        if expected_policy is not None:
            _validate_policy_binding(manifest, expected_policy)
        expected = {
            f"portable/{domain['name']}/{item['path']}": item
            for domain in manifest["domains"]
            for item in domain["files"]
        }
        for member in members[1:]:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] != "portable"
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise PortableStateError(
                    f"unsafe portable archive member: {member.name}"
                )
            if member.isdir():
                continue
            if member.name in observed_members or member.name not in expected:
                raise PortableStateError(
                    f"unexpected/duplicate portable archive member: {member.name}"
                )
            stream = archive.extractfile(member)
            if stream is None:
                raise PortableStateError(
                    f"portable archive member cannot be read: {member.name}"
                )
            data = stream.read(MAX_FILE_BYTES + 1)
            row = expected[member.name]
            if (
                len(data) != row["bytes"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
            ):
                raise PortableStateError(
                    f"portable archive content mismatch: {member.name}"
                )
            assert_secret_free(PurePosixPath(*pure.parts[1:]), data)
            observed_members.add(member.name)
        if observed_members != set(expected):
            raise PortableStateError("portable archive is missing declared files")
    return {
        "schema": "iii.portable-backup-verification/v1",
        "backup_id": manifest["backup_id"],
        "archive_sha256": _sha256(path),
        "archive_bytes": path.stat().st_size,
        "target_state_hash": manifest["target_state_hash"],
        "state_marker": manifest["state_marker"],
        "manifest": manifest,
        "verified": True,
    }


def _validate_manifest(
    manifest: Mapping[str, Any], *, expected_policy_id: str | None = None
) -> None:
    required = {
        "schema",
        "backup_id",
        "portable_schema_version",
        "policy_id",
        "sealed_at",
        "target",
        "release_id",
        "state_marker",
        "target_state_hash",
        "domains",
        "structural_exclusions",
        "invalidating_mutations",
        "quiescence",
        "source",
    }
    if set(manifest) != required or manifest.get("schema") != MANIFEST_SCHEMA:
        raise PortableStateError("portable backup manifest fields/schema are invalid")
    if manifest.get("portable_schema_version") != 1:
        raise PortableStateError("portable backup schema version is incompatible")
    if (
        expected_policy_id is not None
        and manifest.get("policy_id") != expected_policy_id
    ):
        raise PortableStateError("portable backup was sealed under another policy")
    for field in ("backup_id", "policy_id", "state_marker", "target_state_hash"):
        if not isinstance(manifest.get(field), str) or not HASH.fullmatch(
            manifest[field]
        ):
            raise PortableStateError(f"portable backup {field} is invalid")
    expected = content_identity(
        {key: value for key, value in manifest.items() if key != "backup_id"}
    )
    if manifest["backup_id"] != expected:
        raise PortableStateError("portable backup identity mismatch")
    domains = manifest.get("domains")
    if not isinstance(domains, list) or not domains:
        raise PortableStateError("portable backup domains are missing")
    if content_identity(domains) != manifest["target_state_hash"]:
        raise PortableStateError("portable backup target-state hash mismatch")
    names: set[str] = set()
    for domain in domains:
        if not isinstance(domain, dict) or set(domain) != {
            "name",
            "source_path",
            "present",
            "optional",
            "files",
            "domain_hash",
        }:
            raise PortableStateError("portable backup domain fields are invalid")
        name = domain["name"]
        if (
            not isinstance(name, str)
            or not DOMAIN_NAME.fullmatch(name)
            or name in names
        ):
            raise PortableStateError(
                "portable backup domain name is invalid or duplicated"
            )
        source_path = domain["source_path"]
        if not isinstance(source_path, str):
            raise PortableStateError("portable backup domain source path is invalid")
        source_relative = PurePosixPath(source_path)
        if (
            source_relative.is_absolute()
            or not source_relative.parts
            or any(part in {"", ".", ".."} for part in source_relative.parts)
            or not isinstance(domain["present"], bool)
            or not isinstance(domain["optional"], bool)
        ):
            raise PortableStateError("portable backup domain metadata is invalid")
        names.add(name)
        files = domain["files"]
        if (
            not isinstance(files, list)
            or content_identity(files) != domain["domain_hash"]
        ):
            raise PortableStateError(
                "portable backup domain inventory identity mismatch"
            )
        paths: list[str] = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
                raise PortableStateError("portable backup file fields are invalid")
            relative = PurePosixPath(str(item["path"]))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or not isinstance(item["bytes"], int)
                or isinstance(item["bytes"], bool)
                or not 0 <= item["bytes"] <= MAX_FILE_BYTES
                or not isinstance(item["sha256"], str)
                or not HASH.fullmatch(item["sha256"])
            ):
                raise PortableStateError("portable backup file metadata is invalid")
            paths.append(relative.as_posix())
        if paths != sorted(set(paths)):
            raise PortableStateError(
                "portable backup file paths are unsorted or duplicated"
            )
        if not domain["present"] and files:
            raise PortableStateError("absent portable backup domain declares files")
    target = manifest["target"]
    if (
        not isinstance(target, dict)
        or set(target) != {"logical_id", "profile"}
        or not all(isinstance(value, str) and value for value in target.values())
        or not isinstance(manifest["sealed_at"], str)
        or not manifest["sealed_at"]
        or not (
            manifest["release_id"] is None or isinstance(manifest["release_id"], str)
        )
        or not isinstance(manifest["quiescence"], dict)
        or manifest["source"] not in {"receiver", "salvage"}
        or not isinstance(manifest["structural_exclusions"], list)
        or not isinstance(manifest["invalidating_mutations"], list)
    ):
        raise PortableStateError("portable backup provenance metadata is invalid")


def _validate_policy_binding(
    manifest: Mapping[str, Any], policy: Mapping[str, Any]
) -> None:
    """Prove the manifest represents every and only policy-declared domain."""

    declarations = policy["domains"]
    domains = manifest["domains"]
    if len(domains) != len(declarations):
        raise PortableStateError("portable backup omits policy-declared domains")
    for domain, declaration in zip(domains, declarations, strict=True):
        if (
            domain["name"] != declaration["name"]
            or domain["source_path"] != declaration["path"]
            or domain["optional"] != declaration["optional"]
            or any(
                _excluded(PurePosixPath(item["path"]), declaration["exclude"])
                for item in domain["files"]
            )
        ):
            raise PortableStateError(
                "portable backup domain inventory is not bound to policy"
            )
    if (
        manifest["structural_exclusions"] != policy["structural_exclusions"]
        or manifest["invalidating_mutations"] != policy["invalidating_mutations"]
    ):
        raise PortableStateError(
            "portable backup exclusions/mutations are not bound to policy"
        )


def _verify_generation(root: Path, manifest: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise PortableStateError("portable restore generation is unsafe")
    expected = {
        f"{domain['name']}/{item['path']}": item
        for domain in manifest["domains"]
        for item in domain["files"]
    }
    expected_directories: set[str] = set()
    for domain in manifest["domains"]:
        if domain["present"]:
            expected_directories.add(domain["name"])
        for item in domain["files"]:
            relative = PurePosixPath(domain["name"]) / item["path"]
            expected_directories.update(
                parent.as_posix()
                for parent in relative.parents
                if parent != PurePosixPath(".")
            )
    observed: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir() and not path.is_symlink():
            if path.relative_to(root).as_posix() not in expected_directories:
                raise PortableStateError(
                    "portable restore generation has unexpected directories"
                )
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file() or relative not in expected:
            raise PortableStateError(
                "portable restore generation has unexpected content"
            )
        row = expected[relative]
        if (
            path.stat(follow_symlinks=False).st_size != row["bytes"]
            or _sha256(path) != row["sha256"]
        ):
            raise PortableStateError("portable restore generation content mismatch")
        observed.add(relative)
    if observed != set(expected):
        raise PortableStateError("portable restore generation is incomplete")


def _extract_verified(
    path: Path, destination: Path, verification: Mapping[str, Any]
) -> None:
    manifest = verification["manifest"]
    expected = {
        f"portable/{domain['name']}/{item['path']}"
        for domain in manifest["domains"]
        for item in domain["files"]
    }
    destination.mkdir(parents=True, mode=0o750)
    for domain in manifest["domains"]:
        if domain["present"]:
            (destination / domain["name"]).mkdir(mode=0o750)
    with tarfile.open(path, "r:") as archive:
        for member in archive.getmembers()[1:]:
            if not member.isfile():
                continue
            if member.name not in expected:
                raise PortableStateError("archive changed after verification")
            relative = PurePosixPath(member.name).relative_to("portable")
            target = destination / Path(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            source = archive.extractfile(member)
            if source is None:
                raise PortableStateError("archive changed after verification")
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())


@dataclass(frozen=True)
class BackupPaths:
    state_root: Path
    backup_root: Path
    generation_root: Path
    current_selector: Path

    @classmethod
    def under(cls, root: Path) -> "BackupPaths":
        return cls(
            state_root=root / "var/lib/iii",
            backup_root=root / "var/lib/iii/backups",
            generation_root=root / "var/lib/iii/portable-generations",
            current_selector=root / "var/lib/iii/portable-selector/current",
        )


class PortableBackupController:
    """Receiver-owned coordinator; caller supplies runtime safety/lifecycle hooks."""

    def __init__(
        self,
        *,
        source_root: Path,
        storage_root: Path | None = None,
        policy_path: Path,
        logical_target: str,
        profile: str,
        active_release_id: Callable[[], str | None],
        maintenance_safe: Callable[[], bool],
        quiesce_writers: Callable[[], Mapping[str, Any]],
        resume_standby: Callable[[], Mapping[str, Any]],
        clean_converged_host: Callable[[], bool] = lambda: False,
        reconcile_restore: Callable[
            [Path, Mapping[str, Any]], Mapping[str, Any]
        ] = lambda path, _manifest: {"staged_root": str(path), "compatible": True},
        validate_health: Callable[[], Mapping[str, Any]] = lambda: {"healthy": True},
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.source_root = source_root.absolute()
        self.policy_path = policy_path.absolute()
        self.policy = load_policy(self.policy_path)
        self.paths = BackupPaths.under((storage_root or source_root).absolute())
        self.logical_target = logical_target
        self.profile = profile
        self.active_release_id = active_release_id
        self.maintenance_safe = maintenance_safe
        self.quiesce_writers = quiesce_writers
        self.resume_standby = resume_standby
        self.clean_converged_host = clean_converged_host
        self.reconcile_restore = reconcile_restore
        self.validate_health = validate_health
        self.now = now

    def seal(self, *, operation_id: str, source: str = "receiver") -> dict[str, Any]:
        if source not in {"receiver", "salvage"}:
            raise PortableStateError("portable backup source is invalid")
        if (
            source == "receiver"
            and self.profile in {"real", "opti_track"}
            and not self.maintenance_safe()
        ):
            raise PortableStateError(
                "real/OptiTrack backup requires maintenance-safe state"
            )
        before = state_marker(self.source_root, self.policy)
        resumed: Mapping[str, Any] | None = None
        quiescence: Mapping[str, Any] = {
            "mode": "offline-salvage",
            "writers_stopped": True,
        }
        temporary_parent = self.paths.backup_root / ".staging"
        temporary_parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        staging = Path(
            tempfile.mkdtemp(prefix=f"{operation_id}-", dir=temporary_parent)
        )
        payload = staging / "portable"
        payload.mkdir(mode=0o750)
        try:
            if source == "receiver":
                quiescence = dict(self.quiesce_writers())
                if quiescence.get("writers_stopped") is not True:
                    raise PortableStateError(
                        "persistent-state writers did not prove quiescence"
                    )
                _flush_domains(self.source_root, self.policy)
                quiescence = {**quiescence, "flushed": True}
            domains, target_hash = _copy_and_inventory(
                self.source_root, payload, self.policy
            )
            after = state_marker(self.source_root, self.policy)
            if before != after:
                raise PortableStateConflict(
                    "persistent state changed across the sealed boundary"
                )
            manifest: dict[str, Any] = {
                "schema": MANIFEST_SCHEMA,
                "backup_id": "0" * 64,
                "portable_schema_version": self.policy["portable_schema_version"],
                "policy_id": policy_id(self.policy),
                "sealed_at": self.now(),
                "target": {"logical_id": self.logical_target, "profile": self.profile},
                "release_id": self.active_release_id(),
                "state_marker": after,
                "target_state_hash": target_hash,
                "domains": domains,
                "structural_exclusions": self.policy["structural_exclusions"],
                "invalidating_mutations": self.policy["invalidating_mutations"],
                "quiescence": dict(quiescence),
                "source": source,
            }
            manifest["backup_id"] = content_identity(
                {key: value for key, value in manifest.items() if key != "backup_id"}
            )
            final_root = self.paths.backup_root / manifest["backup_id"]
            if final_root.is_dir() and not final_root.is_symlink():
                existing = self.show(manifest["backup_id"])
                return {
                    **existing["receipt"],
                    "archive_path": str(final_root / "portable-state.tar"),
                    "manifest": existing["verification"]["manifest"],
                    "duplicate_content": True,
                }
            final_root.mkdir(parents=True, exist_ok=False, mode=0o750)
            archive_path = final_root / "portable-state.tar"
            _write_archive(archive_path, manifest, payload)
            verification = inspect_archive(archive_path, expected_policy=self.policy)
            receipt: dict[str, Any] = {
                "schema": RECEIPT_SCHEMA,
                "backup_id": manifest["backup_id"],
                "receipt_id": "0" * 64,
                "sealed_at": manifest["sealed_at"],
                "verified_at": self.now(),
                "verified": True,
                "external_verified": False,
                "fresh": True,
                "target": manifest["target"],
                "release_id": manifest["release_id"],
                "state_marker": manifest["state_marker"],
                "target_state_hash": manifest["target_state_hash"],
                "archive_sha256": verification["archive_sha256"],
                "archive_bytes": verification["archive_bytes"],
                "policy_id": manifest["policy_id"],
                "source": source,
                "operation_id": operation_id,
                "protected": True,
                "references": [manifest["backup_id"]],
            }
            receipt["receipt_id"] = content_identity(
                {key: value for key, value in receipt.items() if key != "receipt_id"}
            )
            _atomic_json(final_root / "receipt.json", receipt)
            return {**receipt, "archive_path": str(archive_path), "manifest": manifest}
        except Exception:
            # Do not leave a final-looking archive without a verified receipt.
            if "final_root" in locals():
                shutil.rmtree(final_root, ignore_errors=True)
            raise
        finally:
            if source == "receiver":
                resumed = self.resume_standby()
                if resumed.get("standby_resumed") is not True:
                    # The archive may be sound, but a receiver operation must fail
                    # loudly if the safe lifecycle state was not restored.
                    shutil.rmtree(
                        locals().get("final_root", Path("/__none__")),
                        ignore_errors=True,
                    )
                    raise PortableStateError(
                        "persistent-state writers did not resume in standby"
                    )
            shutil.rmtree(staging, ignore_errors=True)

    def list(self) -> list[dict[str, Any]]:
        if not self.paths.backup_root.exists():
            return []
        rows = []
        for receipt_path in sorted(self.paths.backup_root.glob("*/receipt.json")):
            try:
                value = _load_object(receipt_path, label="host backup receipt")
            except PortableStateError:
                continue
            if value.get("schema") == RECEIPT_SCHEMA:
                rows.append(value)
        return rows

    def show(self, backup_id: str) -> dict[str, Any]:
        if not HASH.fullmatch(backup_id):
            raise PortableStateError("backup identity is invalid")
        receipt = _load_object(
            self.paths.backup_root / backup_id / "receipt.json",
            label="host backup receipt",
        )
        verification = inspect_archive(
            self.paths.backup_root / backup_id / "portable-state.tar",
            expected_policy=self.policy,
        )
        if (
            receipt.get("backup_id") != backup_id
            or receipt.get("archive_sha256") != verification["archive_sha256"]
        ):
            raise PortableStateError("backup receipt/archive binding mismatch")
        return {"receipt": receipt, "verification": verification}

    def chunk(
        self, backup_id: str, *, offset: int, length: int = DEFAULT_CHUNK_BYTES
    ) -> dict[str, Any]:
        detail = self.show(backup_id)
        path = self.paths.backup_root / backup_id / "portable-state.tar"
        size = path.stat().st_size
        if offset < 0 or length <= 0 or length > DEFAULT_CHUNK_BYTES or offset > size:
            raise PortableStateError("backup chunk bounds are invalid")
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        import base64

        return {
            "schema": "iii.portable-backup-chunk/v1",
            "backup_id": backup_id,
            "offset": offset,
            "bytes": len(data),
            "total_bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_base64": base64.b64encode(data).decode("ascii"),
            "archive_sha256": detail["verification"]["archive_sha256"],
        }

    def status(self) -> dict[str, Any]:
        backups = self.list()
        latest = max(backups, key=lambda item: item["sealed_at"], default=None)
        marker = state_marker(self.source_root, self.policy)
        fresh = latest is not None and latest.get("state_marker") == marker
        return {
            "schema": "iii.portable-backup-status/v1",
            "latest_backup_id": latest and latest["backup_id"],
            "current_state_marker": marker,
            "backup_fresh": fresh,
            "backup_count": len(backups),
        }

    def plan_restore(self, archive_path: Path, *, operation_id: str) -> dict[str, Any]:
        verification = inspect_archive(archive_path, expected_policy=self.policy)
        manifest = verification["manifest"]
        if not self.clean_converged_host():
            raise PortableStateError("restore requires a clean converged host")
        active = self.active_release_id()
        if active is None or manifest["release_id"] not in {None, active}:
            raise PortableStateError(
                "restore backup is incompatible with the deployed release"
            )
        plan: dict[str, Any] = {
            "schema": RESTORE_PLAN_SCHEMA,
            "plan_id": "0" * 64,
            "operation_id": operation_id,
            "backup_id": manifest["backup_id"],
            "archive_path": str(archive_path.absolute()),
            "archive_sha256": verification["archive_sha256"],
            "policy_id": manifest["policy_id"],
            "portable_schema_version": manifest["portable_schema_version"],
            "active_release_id": active,
            "clean_converged_host": True,
            "mutations": [
                "extract-private-staging",
                "reconcile-versioned-state",
                "atomic-portable-root-selector",
                "validate-restored-health",
            ],
        }
        plan["plan_id"] = content_identity(
            {key: value for key, value in plan.items() if key != "plan_id"}
        )
        return plan

    def restore(
        self, plan: Mapping[str, Any], *, archive_path: Path | None = None
    ) -> dict[str, Any]:
        expected = content_identity(
            {key: value for key, value in plan.items() if key != "plan_id"}
        )
        if plan.get("schema") != RESTORE_PLAN_SCHEMA or plan.get("plan_id") != expected:
            raise PortableStateError("restore requires an exact retained plan")
        archive = archive_path or Path(str(plan["archive_path"]))
        verification = inspect_archive(archive, expected_policy=self.policy)
        if verification["archive_sha256"] != plan["archive_sha256"]:
            raise PortableStateConflict("restore archive changed after planning")
        if (
            not self.clean_converged_host()
            or self.active_release_id() != plan["active_release_id"]
        ):
            raise PortableStateConflict(
                "restore host/release state changed after planning"
            )
        backup_id = verification["backup_id"]
        generation = self.paths.generation_root / backup_id
        generation_exists = generation.exists() or generation.is_symlink()
        stage = self.paths.generation_root / f".{backup_id}.staging-{os.getpid()}"
        stage.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if stage.parent.is_symlink() or not stage.parent.is_dir():
            raise PortableStateError("portable generation root is unsafe")
        previous = None
        try:
            _extract_verified(archive, stage, verification)
            reconciliation = dict(
                self.reconcile_restore(stage, verification["manifest"])
            )
            if reconciliation.get("compatible") is not True:
                raise PortableStateError("staged restore reconciliation is unresolved")
            if generation_exists:
                _verify_generation(generation, verification["manifest"])
                _verify_generation(stage, verification["manifest"])
                shutil.rmtree(stage)
            else:
                os.replace(stage, generation)
            _fsync_dir(generation.parent)
            selector = self.paths.current_selector
            selector.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            if selector.parent.is_symlink() or not selector.parent.is_dir():
                raise PortableStateError("portable-state selector root is unsafe")
            if selector.is_symlink():
                previous = os.readlink(selector)
                resolved = selector.resolve(strict=True)
                generation_root = self.paths.generation_root.resolve(strict=True)
                if (
                    not resolved.is_relative_to(generation_root)
                    or not resolved.is_dir()
                ):
                    raise PortableStateError(
                        "portable-state selector resolves outside versioned generations"
                    )
            elif selector.exists():
                raise PortableStateError(
                    "portable-state selector is not a symbolic link"
                )
            replacement = selector.parent / f".{selector.name}.{os.getpid()}.tmp"
            replacement.symlink_to(Path(os.path.relpath(generation, selector.parent)))
            os.replace(replacement, selector)
            _fsync_dir(selector.parent)
            try:
                health = dict(self.validate_health())
            except Exception as exc:
                if previous is None:
                    selector.unlink(missing_ok=True)
                else:
                    rollback = (
                        selector.parent / f".{selector.name}.rollback-{os.getpid()}"
                    )
                    rollback.symlink_to(previous)
                    os.replace(rollback, selector)
                _fsync_dir(selector.parent)
                raise PortableStateError(
                    "restored persistent-state health check failed"
                ) from exc
            if health.get("healthy") is not True:
                if previous is None:
                    selector.unlink(missing_ok=True)
                else:
                    rollback = (
                        selector.parent / f".{selector.name}.rollback-{os.getpid()}"
                    )
                    rollback.symlink_to(previous)
                    os.replace(rollback, selector)
                _fsync_dir(selector.parent)
                raise PortableStateError(
                    "restored persistent state failed health validation"
                )
            result: dict[str, Any] = {
                "schema": RESTORE_RESULT_SCHEMA,
                "result_id": "0" * 64,
                "operation_id": plan["operation_id"],
                "backup_id": backup_id,
                "generation_path": str(generation),
                "selector": str(selector),
                "reconciliation": reconciliation,
                "health": health,
                "machine_identity_restored": False,
                "receiver_transactions_restored": False,
                "verified": True,
            }
            result["result_id"] = content_identity(
                {key: value for key, value in result.items() if key != "result_id"}
            )
            return result
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def validate_external_receipt(
    receipt: Mapping[str, Any], *, current_marker: str | None = None
) -> None:
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("verified") is not True
        or receipt.get("external_verified") is not True
        or receipt.get("fresh") is not True
        or not isinstance(receipt.get("backup_id"), str)
        or not HASH.fullmatch(receipt["backup_id"])
        or not isinstance(receipt.get("target_state_hash"), str)
        or not HASH.fullmatch(receipt["target_state_hash"])
        or not isinstance(receipt.get("state_marker"), str)
        or not HASH.fullmatch(receipt["state_marker"])
        or not isinstance(receipt.get("archive_sha256"), str)
        or not HASH.fullmatch(receipt["archive_sha256"])
        or not isinstance(receipt.get("receipt_id"), str)
        or not HASH.fullmatch(receipt["receipt_id"])
    ):
        raise PortableStateError(
            "backup receipt is not fresh verified external evidence"
        )
    if receipt["receipt_id"] != content_identity(
        {key: value for key, value in receipt.items() if key != "receipt_id"}
    ):
        raise PortableStateError("backup receipt content identity mismatch")
    if current_marker is not None and receipt.get("state_marker") != current_marker:
        raise PortableStateError(
            "backup receipt does not represent current persistent state"
        )


def _run_json(command: Sequence[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        raise SalvageError(f"salvage inspection command failed: {command[0]}")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SalvageError(
            f"salvage inspection returned invalid JSON: {command[0]}"
        ) from exc
    if not isinstance(value, dict):
        raise SalvageError("salvage inspection must return one JSON object")
    return value


def inspect_salvage_device(
    device: str,
    *,
    lsblk: Mapping[str, Any] | None = None,
    allow_loopback_test: bool = False,
) -> dict[str, Any]:
    """Authenticate an explicit removed disk and reject running/in-use media."""

    if not device.startswith("/dev/disk/by-id/") or Path(device).name in {
        "",
        ".",
        "..",
    }:
        raise SalvageError("salvage requires one explicit /dev/disk/by-id device")
    value = dict(
        lsblk
        or _run_json(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--output",
                "NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,FSTYPE,FSVER,FSAVAIL,FSUSE%,MOUNTPOINTS,PKNAME",
            ]
        )
    )
    disks = [
        item
        for item in value.get("blockdevices", [])
        if item.get("type") == "disk"
        or (
            allow_loopback_test
            and item.get("type") == "loop"
            and str(item.get("path", "")).startswith("/dev/loop")
        )
    ]
    resolved = Path(device).resolve()
    matches = [
        item for item in disks if Path(str(item.get("path", ""))).resolve() == resolved
    ]
    if len(matches) != 1:
        raise SalvageError("salvage target is not one enumerated whole disk")
    disk = matches[0]
    loopback = str(disk.get("path", "")).startswith("/dev/loop")
    if not (disk.get("rm") is True and disk.get("tran") in {"usb", "mmc"}) and not (
        allow_loopback_test and loopback
    ):
        raise SalvageError("salvage target is not explicit removable USB/MMC media")
    descendants: list[Mapping[str, Any]] = []

    def walk(item: Mapping[str, Any]) -> None:
        descendants.append(item)
        for child in item.get("children") or []:
            walk(child)

    walk(disk)
    if any(
        any(point for point in (item.get("mountpoints") or [])) for item in descendants
    ):
        raise SalvageError("salvage refuses mounted or in-use media")
    root_sources = {
        Path(line.split()[0]).resolve()
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        if len(line.split()) >= 2
        and line.split()[1] in {"/", "/boot", "/boot/firmware"}
    }
    descendant_paths = {
        Path(str(item.get("path", ""))).resolve() for item in descendants
    }
    if root_sources.intersection(descendant_paths):
        raise SalvageError("salvage refuses the running system disk")
    partitions = [item for item in descendants[1:] if item.get("type") == "part"]
    if any(item.get("type") != "part" for item in descendants[1:]):
        raise SalvageError("salvage refuses layered, mapped, or in-use media")
    ext4 = [item for item in partitions if item.get("fstype") == "ext4"]
    if len(ext4) != 1 or any(
        item.get("fstype") not in {None, "vfat", "ext4"} for item in partitions
    ):
        raise SalvageError("salvage requires the known single-ext4 III layout")
    fingerprint = content_identity(
        {
            "stable_path": device,
            "resolved_path": str(resolved),
            "size": disk.get("size"),
            "model": disk.get("model"),
            "serial": disk.get("serial"),
            "partitions": [
                {key: item.get(key) for key in ("path", "size", "fstype", "fsver")}
                for item in partitions
            ],
        }
    )
    return {
        "stable_path": device,
        "resolved_path": str(resolved),
        "fingerprint": fingerprint,
        "read_only": bool(disk.get("ro")),
        "root_partition": str(ext4[0]["path"]),
        "layout": "ubuntu-raspi-single-ext4-root",
        "partitions": partitions,
    }


@contextmanager
def read_only_mount(partition: str, mount_root: Path):
    """Mount one ext4 root kernel-read-only; always detach it on every exit path."""

    mount_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    mounted = False
    try:
        probe = subprocess.run(
            ["e2fsck", "-fn", partition], capture_output=True, check=False
        )
        # e2fsck bit 4/8/16/32/128 represent uncorrected/operational failures.
        if probe.returncode & (4 | 8 | 16 | 32 | 128):
            raise SalvageError("salvage filesystem is inconsistent or mid-transaction")
        result = subprocess.run(
            [
                "mount",
                "-t",
                "ext4",
                "-o",
                "ro,noload,nodev,nosuid,noexec",
                partition,
                str(mount_root),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SalvageError("kernel-enforced read-only salvage mount failed")
        mounted = True
        options = next(
            (
                line.split()[5]
                for line in Path("/proc/self/mountinfo")
                .read_text(encoding="utf-8")
                .splitlines()
                if len(line.split()) > 5 and line.split()[4] == str(mount_root)
            ),
            "",
        )
        if "ro" not in options.split(","):
            raise SalvageError("salvage mount is not kernel-enforced read-only")
        yield mount_root
    finally:
        if mounted:
            subprocess.run(
                ["umount", "--", str(mount_root)], check=False, capture_output=True
            )
        try:
            mount_root.rmdir()
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY}:
                raise


def salvage_record(
    *,
    controller: PortableBackupController,
    device_evidence: Mapping[str, Any],
    operation_id: str,
    omissions: Sequence[str] = (),
) -> dict[str, Any]:
    receipt = controller.seal(operation_id=operation_id, source="salvage")
    manifest = receipt["manifest"]
    record: dict[str, Any] = {
        "schema": SALVAGE_SCHEMA,
        "salvage_id": "0" * 64,
        "backup_id": receipt["backup_id"],
        "outcome": "verified",
        "verified": True,
        "recorded_at": utc_now(),
        "source_device": {
            key: device_evidence[key]
            for key in (
                "stable_path",
                "resolved_path",
                "fingerprint",
                "root_partition",
                "layout",
            )
        },
        "filesystem": {
            "type": "ext4",
            "mount_enforcement": "kernel-read-only-ro-noload-nodev-nosuid-noexec",
            "transaction_consistency": "e2fsck-read-only-clean",
            "source_modified": False,
        },
        "recoverable_domains": [
            domain["name"] for domain in manifest["domains"] if domain["present"]
        ],
        "omissions": sorted(
            set(omissions)
            | {
                domain["name"]
                for domain in manifest["domains"]
                if not domain["present"]
            }
        ),
        "target_state_hash": manifest["target_state_hash"],
        "archive_sha256": receipt["archive_sha256"],
        "credentials_recovered": False,
        "recommissioning_required": True,
        "operator_notice": "Fresh credentials, a clean reimage, and full recommissioning remain mandatory; this salvage is not bootable media.",
    }
    record["salvage_id"] = content_identity(
        {key: value for key, value in record.items() if key != "salvage_id"}
    )
    return record


def salvage_main() -> int:
    """Privileged helper intended to run only inside ``unshare --mount``."""

    import argparse

    parser = argparse.ArgumentParser(prog="iii-host-salvage-worker")
    parser.add_argument("--device", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument(
        "--allow-loopback-test", action="store_true", help=argparse.SUPPRESS
    )
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("read-only block-device salvage requires root")
    try:
        evidence = inspect_salvage_device(
            arguments.device, allow_loopback_test=arguments.allow_loopback_test
        )
        mount_root = Path(tempfile.mkdtemp(prefix="iii-salvage-mount-"))
        mount_root.rmdir()
        with read_only_mount(evidence["root_partition"], mount_root) as source:
            controller = PortableBackupController(
                source_root=source,
                storage_root=arguments.output_root,
                policy_path=arguments.policy,
                logical_target="drone",
                profile="real",
                active_release_id=lambda: None,
                maintenance_safe=lambda: True,
                quiesce_writers=lambda: {
                    "writers_stopped": True,
                    "mode": "powered-off-read-only-salvage",
                },
                resume_standby=lambda: {"standby_resumed": True},
            )
            record = salvage_record(
                controller=controller,
                device_evidence=evidence,
                operation_id=arguments.operation_id,
            )
            backup_root = controller.paths.backup_root / record["backup_id"]
            _atomic_json(backup_root / "salvage-record.json", record)
        sys_output = canonical_json(
            {
                "schema": "iii.host-salvage-worker-result/v1",
                "backup_id": record["backup_id"],
                "salvage_id": record["salvage_id"],
                "record_path": str(backup_root / "salvage-record.json"),
                "archive_path": str(backup_root / "portable-state.tar"),
            }
        )
        os.write(1, sys_output + b"\n")
        return 0
    except (PortableStateError, OSError) as exc:
        parser.error(str(exc))
    return 64


if __name__ == "__main__":
    raise SystemExit(salvage_main())
