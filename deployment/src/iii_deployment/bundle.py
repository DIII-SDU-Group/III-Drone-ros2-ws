"""Deterministic, signed, independently installable III release bundles."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from typing import Any, BinaryIO, Mapping

import zstandard

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .signers import (
    load_private_key,
    load_trusted_signers,
    signer_id_for_public_key,
    trusted_public_key,
    verify,
)

ARCHIVE_NAME = "bundle.tar.zst"
BUNDLE_MANIFEST_NAME = "bundle.manifest.json"
RELEASE_MANIFEST_NAME = "release-manifest.json"
CHECKSUM_NAME = "bundle.sha256"
SIGNATURE_NAME = "bundle.sig.json"
COMPONENT_FILES = {
    ARCHIVE_NAME,
    BUNDLE_MANIFEST_NAME,
    RELEASE_MANIFEST_NAME,
    CHECKSUM_NAME,
    SIGNATURE_NAME,
}
SIGNATURE_DOMAIN = b"iii.bundle-signature/v1\0"
META_BUNDLE = "META/bundle-manifest.json"
META_RELEASE = "META/release-manifest.json"
FORBIDDEN_PAYLOAD_ROOTS = {".git", "build", "log", "src"}


@dataclass(frozen=True)
class BundlePaths:
    directory: Path
    archive: Path
    bundle_manifest: Path
    release_manifest: Path
    checksum: Path
    signature: Path

    @classmethod
    def from_directory(cls, directory: Path) -> "BundlePaths":
        return cls(
            directory=directory,
            archive=directory / ARCHIVE_NAME,
            bundle_manifest=directory / BUNDLE_MANIFEST_NAME,
            release_manifest=directory / RELEASE_MANIFEST_NAME,
            checksum=directory / CHECKSUM_NAME,
            signature=directory / SIGNATURE_NAME,
        )


@dataclass(frozen=True)
class VerifiedBundle:
    paths: BundlePaths
    release_manifest: dict[str, Any]
    bundle_manifest: dict[str, Any]
    signature: dict[str, Any]
    archive_sha256: str
    compressed_bytes: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or unsafe")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def load_bundle_limits(policy_path: Path) -> dict[str, int]:
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        source = policy["bundle"]
        limits = {
            "unpacked_bytes": int(source["maximum_unpacked_bytes"]),
            "entries": int(source["maximum_entries"]),
            "maximum_path_bytes": int(source["maximum_path_bytes"]),
            "maximum_path_depth": int(source["maximum_path_depth"]),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load operational bundle limits: {exc}") from exc
    if any(value <= 0 for value in limits.values()):
        raise ContractError("operational bundle limits must be positive")
    return limits


def validate_release_metadata(
    manifest: Mapping[str, Any], registry: ContractRegistry
) -> None:
    registry.validate("release-manifest", manifest)
    components = manifest["components"]
    if components != sorted(set(components)):
        raise ContractError("release components must be unique and sorted")
    if set(manifest["component_targets"]) != set(components):
        raise ContractError("component targets do not exactly match release components")
    expected_authority = (
        "ci-qualified"
        if manifest["release_class"] == "qualified"
        else "workstation-field"
    )
    if manifest["signing"]["authority"] != expected_authority:
        raise ContractError("release signer authority does not match release class")
    if (
        manifest["release_class"] == "qualified"
        and manifest["mission_catalog"]["scope"] != "qualified"
    ):
        raise ContractError("qualified release must bind a qualified mission catalog")
    profile_ids: set[str] = set()
    parameter_profiles = set(manifest["px4"]["manifests"])
    if set(manifest["px4"]["manifest_ids"]) != parameter_profiles:
        raise ContractError(
            "PX4 manifest identities do not exactly match parameter profiles"
        )
    for profile in manifest["profiles"]:
        if profile["id"] in profile_ids:
            raise ContractError("release profile identifiers must be unique")
        profile_ids.add(profile["id"])
        if profile["parameter_profile"] not in parameter_profiles:
            raise ContractError(
                "release profile references an unknown PX4 parameter manifest"
            )
        if profile["bootable"] and profile["status"] != "commissioned":
            raise ContractError("uncommissioned release profiles must fail closed")


def _compatibility_identity(manifest: Mapping[str, Any]) -> str:
    return content_identity(
        {
            "compatibility": manifest["compatibility"],
            "configuration": manifest["configuration"],
            "px4": manifest["px4"],
            "qgc": manifest["qgc"],
            "profiles": manifest["profiles"],
            "mission_catalog": manifest["mission_catalog"],
        }
    )


def _validate_path(name: str, limits: Mapping[str, int]) -> PurePosixPath:
    if not name or "\0" in name or "\\" in name:
        raise ContractError(f"unsafe bundle path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(f"unsafe bundle path: {name!r}")
    if len(name.encode("utf-8")) > limits["maximum_path_bytes"]:
        raise ContractError(f"bundle path exceeds byte limit: {name!r}")
    if len(path.parts) > limits["maximum_path_depth"]:
        raise ContractError(f"bundle path exceeds depth limit: {name!r}")
    return path


def _normalized_file_mode(metadata: os.stat_result) -> int:
    return 0o755 if metadata.st_mode & 0o111 else 0o644


def _index_payload(root: Path, host_limits: Mapping[str, int]) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"component payload root is missing or unsafe: {root}")
    content: list[dict[str, Any]] = []
    for current, directories, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        relative_parent = current_path.relative_to(root)
        if relative_parent == Path("."):
            forbidden = (set(directories) | set(filenames)) & FORBIDDEN_PAYLOAD_ROOTS
            if forbidden:
                raise ContractError(
                    "component payload contains source/build root: "
                    + ", ".join(sorted(forbidden))
                )
        for name in directories:
            source = current_path / name
            metadata = source.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or source.is_symlink():
                raise ContractError(
                    f"payload directory is a link or special file: {source}"
                )
            archive_path = PurePosixPath(
                "payload", relative_parent.as_posix(), name
            ).as_posix()
            archive_path = archive_path.replace("payload/./", "payload/")
            _validate_path(archive_path, host_limits)
            content.append(
                {
                    "path": archive_path,
                    "type": "directory",
                    "mode": 0o755,
                    "size": 0,
                    "sha256": None,
                }
            )
        for name in filenames:
            source = current_path / name
            metadata = source.lstat()
            if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
                raise ContractError(f"payload file is a link or special file: {source}")
            archive_path = PurePosixPath(
                "payload", relative_parent.as_posix(), name
            ).as_posix()
            archive_path = archive_path.replace("payload/./", "payload/")
            _validate_path(archive_path, host_limits)
            content.append(
                {
                    "path": archive_path,
                    "type": "file",
                    "mode": _normalized_file_mode(metadata),
                    "size": metadata.st_size,
                    "sha256": _sha256_file(source),
                }
            )
    content.sort(key=lambda entry: entry["path"].encode("utf-8"))
    if not content:
        raise ContractError("component payload must not be empty")
    if len(content) + 2 > host_limits["entries"]:
        raise ContractError("component payload exceeds host entry ceiling")
    unpacked = sum(entry["size"] for entry in content)
    if unpacked > host_limits["unpacked_bytes"]:
        raise ContractError("component payload exceeds host unpacked-byte ceiling")
    return content


def _payload_identity(content: list[dict[str, Any]]) -> str:
    return content_identity(content)


def _manifest_with_limits(
    base: dict[str, Any], release_bytes: bytes, host_limits: Mapping[str, int]
) -> tuple[dict[str, Any], bytes]:
    manifest = dict(base)
    manifest["limits"] = {
        "entries": len(base["content"]) + 2,
        "unpacked_bytes": 1,
        "maximum_path_bytes": 1,
        "maximum_path_depth": 1,
    }
    for _ in range(16):
        manifest_bytes = canonical_json(manifest) + b"\n"
        names = [META_BUNDLE, META_RELEASE] + [item["path"] for item in base["content"]]
        next_limits = {
            "entries": len(names),
            "unpacked_bytes": len(manifest_bytes)
            + len(release_bytes)
            + sum(item["size"] for item in base["content"]),
            "maximum_path_bytes": max(len(name.encode("utf-8")) for name in names),
            "maximum_path_depth": max(len(PurePosixPath(name).parts) for name in names),
        }
        for key, value in next_limits.items():
            if value > host_limits[key]:
                raise ContractError(f"bundle exceeds host {key} ceiling")
        if manifest["limits"] == next_limits:
            return manifest, manifest_bytes
        manifest["limits"] = next_limits
    raise ContractError("bundle manifest limits did not converge")


def _tar_info(
    name: str, *, mode: int, size: int, directory: bool = False
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.size = size
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    return info


class _VerifiedSource(io.RawIOBase):
    def __init__(
        self, stream: BinaryIO, expected_size: int, expected_sha256: str
    ) -> None:
        self.stream = stream
        self.remaining = expected_size
        self.expected_sha256 = expected_sha256
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.remaining == 0:
            return b""
        requested = self.remaining if size < 0 else min(size, self.remaining)
        block = self.stream.read(requested)
        if not block:
            raise ContractError("payload file changed while packaging")
        self.remaining -= len(block)
        self.digest.update(block)
        return block

    def finish(self) -> None:
        if self.remaining or self.digest.hexdigest() != self.expected_sha256:
            raise ContractError("payload file changed while packaging")


def _write_archive(
    destination: Path,
    root: Path,
    content: list[dict[str, Any]],
    manifest_bytes: bytes,
    release_bytes: bytes,
    additional_sources: Mapping[str, Path] | None = None,
) -> None:
    additional_sources = additional_sources or {}
    with destination.open("xb") as raw:
        compressor = zstandard.ZstdCompressor(
            level=19, write_checksum=True, write_content_size=False, threads=0
        )
        with compressor.stream_writer(raw, closefd=False) as encoded:
            with tarfile.open(
                fileobj=encoded, mode="w|", format=tarfile.USTAR_FORMAT
            ) as archive:
                for name, data in (
                    (META_BUNDLE, manifest_bytes),
                    (META_RELEASE, release_bytes),
                ):
                    archive.addfile(
                        _tar_info(name, mode=0o644, size=len(data)), io.BytesIO(data)
                    )
                for entry in content:
                    relative = PurePosixPath(entry["path"]).relative_to("payload")
                    source = additional_sources.get(
                        entry["path"], root.joinpath(*relative.parts)
                    )
                    if entry["type"] == "directory":
                        metadata = source.lstat()
                        if not stat.S_ISDIR(metadata.st_mode) or source.is_symlink():
                            raise ContractError(
                                "payload directory changed while packaging"
                            )
                        archive.addfile(
                            _tar_info(entry["path"], mode=0o755, size=0, directory=True)
                        )
                        continue
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        metadata = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_size != entry["size"]
                        ):
                            raise ContractError("payload file changed while packaging")
                        with os.fdopen(descriptor, "rb", closefd=False) as stream:
                            verified = _VerifiedSource(
                                stream, entry["size"], entry["sha256"]
                            )
                            archive.addfile(
                                _tar_info(
                                    entry["path"],
                                    mode=entry["mode"],
                                    size=entry["size"],
                                ),
                                verified,
                            )
                            verified.finish()
                            if os.fstat(descriptor).st_size != entry["size"]:
                                raise ContractError(
                                    "payload file changed while packaging"
                                )
                    finally:
                        os.close(descriptor)
        raw.flush()
        os.fsync(raw.fileno())


def _signature_message(document: Mapping[str, Any]) -> bytes:
    unsigned = dict(document)
    unsigned.pop("signature", None)
    return SIGNATURE_DOMAIN + canonical_json(unsigned)


def package_bundle_set(
    release_manifest_path: Path,
    component_roots: Mapping[str, Path],
    private_key_path: Path,
    output_directory: Path,
    *,
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
    shared_files: Mapping[str, Path] | None = None,
) -> dict[str, BundlePaths]:
    """Atomically create paired release components with identical shared evidence."""
    try:
        release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load release manifest: {exc}") from exc
    if not isinstance(release, dict):
        raise ContractError("release manifest must contain an object")
    validate_release_metadata(release, registry)
    if set(component_roots) != set(release["components"]):
        raise ContractError(
            "component payload roots do not exactly match release components"
        )
    key = load_private_key(private_key_path)
    signer_id = signer_id_for_public_key(key.public_key())
    if signer_id != release["signing"]["signer_id"]:
        raise ContractError("private signer identity does not match release manifest")
    release_bytes = canonical_json(release) + b"\n"
    release_sha = hashlib.sha256(release_bytes).hexdigest()
    indexed = {
        component: _index_payload(Path(component_roots[component]), host_limits)
        for component in release["components"]
    }
    shared_sources: dict[str, Path] = {}
    for raw_name, source in sorted((shared_files or {}).items()):
        relative = PurePosixPath(raw_name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ContractError("shared bundle payload path is unsafe")
        archive_name = (PurePosixPath("payload") / relative).as_posix()
        _validate_path(archive_name, host_limits)
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise ContractError("shared bundle payload is missing or linked")
        shared_sources[archive_name] = path
        entry = {
            "path": archive_name,
            "type": "file",
            "mode": 0o644,
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for component in release["components"]:
            if any(item["path"] == archive_name for item in indexed[component]):
                raise ContractError("shared bundle payload collides with component content")
            indexed[component].append(entry)
            indexed[component].sort(key=lambda item: item["path"])
    payloads = {
        component: _payload_identity(indexed[component])
        for component in release["components"]
    }
    compatibility_sha = _compatibility_identity(release)

    output_directory = output_directory.absolute()
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    if output_directory.exists() or output_directory.is_symlink():
        raise ContractError(
            f"refusing to replace release bundle set: {output_directory}"
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.", dir=output_directory.parent
        )
    )
    result: dict[str, BundlePaths] = {}
    try:
        for component in release["components"]:
            component_directory = staging / component
            component_directory.mkdir(mode=0o755)
            paths = BundlePaths.from_directory(component_directory)
            base = {
                "schema_version": "1",
                "manifest_type": "bundle",
                "format": {
                    "name": "iii.tar.zst",
                    "version": 1,
                    "tar": "ustar",
                    "zstd_level": 19,
                    "zstd_checksum": True,
                },
                "release_id": release["release_id"],
                "release_class": release["release_class"],
                "component": component,
                "release_manifest_sha256": release_sha,
                "mission_catalog_hash": release["mission_catalog"]["catalog_hash"],
                "compatibility_sha256": compatibility_sha,
                "component_payloads": payloads,
                "target": release["component_targets"][component],
                "content": indexed[component],
            }
            manifest, manifest_bytes = _manifest_with_limits(
                base, release_bytes, host_limits
            )
            registry.validate("bundle-manifest", manifest)
            paths.bundle_manifest.write_bytes(manifest_bytes)
            paths.release_manifest.write_bytes(release_bytes)
            _write_archive(
                paths.archive,
                Path(component_roots[component]),
                indexed[component],
                manifest_bytes,
                release_bytes,
                shared_sources,
            )
            archive_sha = _sha256_file(paths.archive)
            paths.checksum.write_text(
                f"{archive_sha}  {ARCHIVE_NAME}\n", encoding="ascii"
            )
            signature = {
                "schema_version": "1",
                "signature_type": "iii.bundle-signature",
                "algorithm": "Ed25519",
                "signer_id": signer_id,
                "authority": release["signing"]["authority"],
                "release_id": release["release_id"],
                "component": component,
                "archive_sha256": archive_sha,
                "bundle_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
            signature["signature"] = base64.b64encode(
                key.sign(_signature_message(signature))
            ).decode("ascii")
            registry.validate("bundle-signature", signature)
            paths.signature.write_bytes(canonical_json(signature) + b"\n")
        directory = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.chmod(staging, 0o755)
        os.replace(staging, output_directory)
        parent = os.open(output_directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        for component in release["components"]:
            result[component] = BundlePaths.from_directory(output_directory / component)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_bundle_semantics(
    bundle: Mapping[str, Any],
    release: Mapping[str, Any],
    host_limits: Mapping[str, int],
) -> None:
    component = bundle["component"]
    if (
        bundle["release_id"] != release["release_id"]
        or bundle["release_class"] != release["release_class"]
    ):
        raise ContractError("bundle/release identity disagreement")
    if component not in release["components"]:
        raise ContractError("bundle component is absent from release")
    if bundle["target"] != release["component_targets"][component]:
        raise ContractError("bundle/release target disagreement")
    if set(bundle["component_payloads"]) != set(release["components"]):
        raise ContractError("bundle payload identities do not cover paired components")
    if bundle["component_payloads"][component] != _payload_identity(bundle["content"]):
        raise ContractError("bundle payload identity disagrees with its content index")
    if bundle["compatibility_sha256"] != _compatibility_identity(release):
        raise ContractError("bundle compatibility evidence disagrees with release")
    if bundle["mission_catalog_hash"] != release["mission_catalog"]["catalog_hash"]:
        raise ContractError("bundle mission catalog identity disagrees with release")
    paths = [entry["path"] for entry in bundle["content"]]
    if paths != sorted(set(paths), key=lambda value: value.encode("utf-8")):
        raise ContractError(
            "bundle content paths must be unique and canonically sorted"
        )
    for entry in bundle["content"]:
        _validate_path(entry["path"], host_limits)
        if not entry["path"].startswith("payload/"):
            raise ContractError("bundle content must remain below payload")
        if entry["type"] == "directory":
            if (
                entry["size"] != 0
                or entry["sha256"] is not None
                or entry["mode"] != 0o755
            ):
                raise ContractError("bundle directory metadata is inconsistent")
        elif entry["sha256"] is None:
            raise ContractError("bundle file checksum is missing")
    expected_names = [META_BUNDLE, META_RELEASE] + paths
    actual_limits = {
        "entries": len(expected_names),
        "unpacked_bytes": sum(entry["size"] for entry in bundle["content"]),
        "maximum_path_bytes": max(len(name.encode("utf-8")) for name in expected_names),
        "maximum_path_depth": max(
            len(PurePosixPath(name).parts) for name in expected_names
        ),
    }
    # Metadata byte sizes are added by inspect_bundle after canonical documents are available.
    if bundle["limits"]["entries"] != actual_limits["entries"]:
        raise ContractError("bundle declared entry count is inconsistent")
    for key in ("maximum_path_bytes", "maximum_path_depth"):
        if bundle["limits"][key] != actual_limits[key]:
            raise ContractError(f"bundle declared {key} is inconsistent")
    for key, value in bundle["limits"].items():
        if value > host_limits[key]:
            raise ContractError(f"bundle declared {key} exceeds host ceiling")


def inspect_bundle(
    component_directory: Path,
    trusted_signers: Path | Mapping[str, Any],
    *,
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> VerifiedBundle:
    paths = BundlePaths.from_directory(component_directory.absolute())
    if paths.directory.is_symlink() or not paths.directory.is_dir():
        raise ContractError("bundle component directory is missing or unsafe")
    directory_entries = list(paths.directory.iterdir())
    entries = {item.name for item in directory_entries}
    if entries != COMPONENT_FILES or any(
        item.is_symlink() or not item.is_file() for item in directory_entries
    ):
        raise ContractError(
            "bundle component directory has missing, extra, or linked files"
        )
    release = _canonical_document(paths.release_manifest, label="release manifest")
    bundle = _canonical_document(paths.bundle_manifest, label="bundle manifest")
    signature = _canonical_document(paths.signature, label="bundle signature")
    validate_release_metadata(release, registry)
    registry.validate("bundle-manifest", bundle)
    registry.validate("bundle-signature", signature)
    release_bytes = canonical_json(release) + b"\n"
    bundle_bytes = canonical_json(bundle) + b"\n"
    if bundle["release_manifest_sha256"] != hashlib.sha256(release_bytes).hexdigest():
        raise ContractError("release manifest checksum disagreement")
    _validate_bundle_semantics(bundle, release, host_limits)
    expected_unpacked = (
        len(release_bytes)
        + len(bundle_bytes)
        + sum(entry["size"] for entry in bundle["content"])
    )
    if bundle["limits"]["unpacked_bytes"] != expected_unpacked:
        raise ContractError("bundle declared unpacked byte count is inconsistent")
    expected_signature = {
        "signer_id": release["signing"]["signer_id"],
        "authority": release["signing"]["authority"],
        "release_id": release["release_id"],
        "component": bundle["component"],
        "bundle_manifest_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
    }
    for field, value in expected_signature.items():
        if signature[field] != value:
            raise ContractError(f"bundle signature {field} disagreement")
    try:
        checksum_text = paths.checksum.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"cannot read bundle checksum: {exc}") from exc
    expected_checksum_line = f"{signature['archive_sha256']}  {ARCHIVE_NAME}\n"
    if checksum_text != expected_checksum_line:
        raise ContractError("bundle checksum sidecar disagreement")
    try:
        with paths.archive.open("rb") as stream:
            frame = zstandard.get_frame_parameters(stream.read(18))
    except (OSError, zstandard.ZstdError) as exc:
        raise ContractError(f"bundle zstd frame is invalid: {exc}") from exc
    if (
        not frame.has_checksum
        or frame.content_size != zstandard.CONTENTSIZE_UNKNOWN
        or frame.dict_id != 0
        or frame.window_size > 128 * 1024 * 1024
    ):
        raise ContractError("bundle zstd frame parameters are not canonical")
    archive_sha = _sha256_file(paths.archive)
    if archive_sha != signature["archive_sha256"]:
        raise ContractError("bundle archive checksum mismatch")
    store = (
        load_trusted_signers(trusted_signers, registry)
        if isinstance(trusted_signers, Path)
        else trusted_signers
    )
    public = trusted_public_key(store, signature["signer_id"], signature["authority"])
    verify(public, signature["signature"], _signature_message(signature))
    return VerifiedBundle(
        paths=paths,
        release_manifest=release,
        bundle_manifest=bundle,
        signature=signature,
        archive_sha256=archive_sha,
        compressed_bytes=paths.archive.stat().st_size,
    )


class _CountingReader(io.RawIOBase):
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.count = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        block = self.stream.read(size)
        self.count += len(block)
        return block


def _expected_tar_bytes(entries: list[tuple[str, int]]) -> int:
    used = sum(512 + ((size + 511) // 512) * 512 for _, size in entries) + 1024
    return ((used + 10239) // 10240) * 10240


def _member_matches(member: tarfile.TarInfo, expected: Mapping[str, Any]) -> None:
    if member.name != expected["path"]:
        raise ContractError(
            "archive ordering or path disagrees with signed content index"
        )
    if (
        member.uid != 0
        or member.gid != 0
        or member.uname
        or member.gname
        or member.mtime != 0
    ):
        raise ContractError("archive header metadata is not normalized")
    if member.pax_headers or member.linkname:
        raise ContractError("archive extensions and links are forbidden")
    if member.mode != expected["mode"] or member.size != expected["size"]:
        raise ContractError("archive header disagrees with signed content index")
    if expected["type"] == "directory":
        if not member.isdir():
            raise ContractError("archive entry type disagreement")
    elif not member.isfile():
        raise ContractError("archive links and special files are forbidden")


def _stream_archive_unchecked(
    verified: VerifiedBundle, destination: Path | None
) -> None:
    manifest_bytes = canonical_json(verified.bundle_manifest) + b"\n"
    release_bytes = canonical_json(verified.release_manifest) + b"\n"
    expected: list[dict[str, Any]] = [
        {
            "path": META_BUNDLE,
            "type": "file",
            "mode": 0o644,
            "size": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        {
            "path": META_RELEASE,
            "type": "file",
            "mode": 0o644,
            "size": len(release_bytes),
            "sha256": hashlib.sha256(release_bytes).hexdigest(),
        },
        *verified.bundle_manifest["content"],
    ]
    tar_size = _expected_tar_bytes([(item["path"], item["size"]) for item in expected])
    descriptor = os.open(verified.paths.archive, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as encoded:
        metadata = os.fstat(encoded.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError("bundle archive is not a regular file")
        digest = hashlib.sha256()
        for block in iter(lambda: encoded.read(1024 * 1024), b""):
            digest.update(block)
        if digest.hexdigest() != verified.archive_sha256:
            raise ContractError("bundle archive changed after detached verification")
        encoded.seek(0)
        decompressor = zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024)
        with decompressor.stream_reader(encoded, read_across_frames=True) as decoded:
            counted = _CountingReader(decoded)
            with tarfile.open(
                fileobj=counted, mode="r|", format=tarfile.USTAR_FORMAT
            ) as archive:
                seen = 0
                for member in archive:
                    if seen >= len(expected):
                        raise ContractError("archive contains unsigned extra entries")
                    signed = expected[seen]
                    _validate_path(
                        member.name,
                        {
                            "maximum_path_bytes": min(
                                verified.bundle_manifest["limits"][
                                    "maximum_path_bytes"
                                ],
                                255,
                            ),
                            "maximum_path_depth": min(
                                verified.bundle_manifest["limits"][
                                    "maximum_path_depth"
                                ],
                                32,
                            ),
                        },
                    )
                    _member_matches(member, signed)
                    seen += 1
                    target = (
                        destination.joinpath(*PurePosixPath(member.name).parts)
                        if destination
                        else None
                    )
                    if member.isdir():
                        if target is not None:
                            target.mkdir(mode=0o755, parents=True, exist_ok=False)
                            target.chmod(0o755)
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise ContractError("archive file payload is missing")
                    digest = hashlib.sha256()
                    written = 0
                    output: BinaryIO | None = None
                    if target is not None:
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                        descriptor = os.open(
                            target,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            signed["mode"],
                        )
                        output = os.fdopen(descriptor, "wb")
                    try:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            written += len(block)
                            digest.update(block)
                            if output is not None:
                                output.write(block)
                        if output is not None:
                            os.fchmod(output.fileno(), signed["mode"])
                            output.flush()
                            os.fsync(output.fileno())
                    finally:
                        if output is not None:
                            output.close()
                    if (
                        written != signed["size"]
                        or digest.hexdigest() != signed["sha256"]
                    ):
                        raise ContractError(
                            "archive content disagrees with signed content index"
                        )
                if seen != len(expected):
                    raise ContractError("archive is missing signed entries")
            while counted.read(1024 * 1024):
                pass
            if counted.count != tar_size:
                raise ContractError(
                    "archive has non-canonical framing or trailing content"
                )


def _stream_archive(verified: VerifiedBundle, destination: Path | None) -> None:
    try:
        _stream_archive_unchecked(verified, destination)
    except ContractError:
        raise
    except (OSError, EOFError, tarfile.TarError, zstandard.ZstdError) as exc:
        raise ContractError(f"bundle archive is corrupt or unsafe: {exc}") from exc


def verify_bundle(
    component_directory: Path,
    trusted_signers: Path | Mapping[str, Any],
    *,
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> VerifiedBundle:
    verified = inspect_bundle(
        component_directory, trusted_signers, registry=registry, host_limits=host_limits
    )
    _stream_archive(verified, None)
    return verified


def extract_bundle(
    component_directory: Path,
    destination: Path,
    trusted_signers: Path | Mapping[str, Any],
    *,
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> VerifiedBundle:
    verified = inspect_bundle(
        component_directory, trusted_signers, registry=registry, host_limits=host_limits
    )
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ContractError(f"refusing to replace extracted release: {destination}")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        _stream_archive(verified, staging)
        directory = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.chmod(staging, 0o755)
        os.replace(staging, destination)
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return verified
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
