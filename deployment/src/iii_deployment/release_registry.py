"""Qualified GitHub release discovery, verification, and atomic offline cache."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from .bundle import COMPONENT_FILES, extract_bundle, inspect_bundle, verify_bundle
from .contracts import ContractError, ContractRegistry, canonical_json
from .qualified_release import (
    NOTES_NAME,
    PROMOTION_NAME,
    PUBLICATION_NAME,
    QUALIFICATION_NAME,
    RECORD_NAME,
    component_asset_name,
    verify_release_notes,
    verify_release_publication,
    verify_release_record,
)
from .release_status import latest_status, require_fetchable_status, verify_status_index
from .signers import load_trusted_signers


STATUS_INDEX_NAME = "release-status-index.json"
CACHE_RECORD_NAME = "cache-record.json"


class ReleaseSource(Protocol):
    def list_versions(self) -> Sequence[str]: ...
    def read_asset(self, tag: str, name: str) -> bytes: ...
    def latest_status_index(self) -> bytes: ...
    def failed_attempt(self, version: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class CachedRelease:
    root: Path
    publication: dict[str, Any]
    notes: dict[str, Any]
    record: dict[str, Any]
    status: dict[str, Any]
    status_index: dict[str, Any]


def inspect_remote_release(
    source: ReleaseSource,
    version: str,
    *,
    bundle_trust: Path | Mapping[str, Any],
    status_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
    status_index_data: bytes | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    publication_data = source.read_asset(version, PUBLICATION_NAME)
    publication = _canonical(publication_data, label="release publication")
    bundle_store = (
        load_trusted_signers(bundle_trust, registry)
        if isinstance(bundle_trust, Path)
        else bundle_trust
    )
    verify_release_publication(publication, bundle_store, registry)
    if publication["version"] != version:
        raise ContractError("release publication version differs from requested tag")
    notes_data = source.read_asset(version, NOTES_NAME)
    notes = _canonical(notes_data, label="release notes")
    verify_release_notes(notes, registry)
    if _sha256(notes_data) != publication["release_notes_sha256"]:
        raise ContractError("release notes differ from signed publication")
    record_data = source.read_asset(version, RECORD_NAME)
    record = _canonical(record_data, label="release record")
    verify_release_record(record, registry)
    if _sha256(record_data) != publication["release_record_sha256"]:
        raise ContractError("release record differs from signed publication")
    status_data = status_index_data or source.latest_status_index()
    status_index = _canonical(status_data, label="release-status index")
    status_store = (
        load_trusted_signers(status_trust, registry)
        if isinstance(status_trust, Path)
        else status_trust
    )
    statement = latest_status(
        status_index,
        release_id=publication["release_id"],
        version=version,
        trusted_signers=status_store,
        registry=registry,
    )
    return publication, notes, record, statement, status_index


def list_remote_releases(
    source: ReleaseSource,
    *,
    bundle_trust: Path | Mapping[str, Any],
    status_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
) -> list[dict[str, Any]]:
    status_data = source.latest_status_index()
    rows = []
    for version in source.list_versions():
        publication, _notes, _record, statement, index = inspect_remote_release(
            source,
            version,
            bundle_trust=bundle_trust,
            status_trust=status_trust,
            registry=registry,
            status_index_data=status_data,
        )
        rows.append(
            {
                "version": version,
                "release_id": publication["release_id"],
                "source_commit": publication["source_commit"],
                "status": statement["status"],
                "status_reason": statement["reason"],
                "status_recorded_at": statement["recorded_at"],
                "status_index_id": index["index_id"],
            }
        )
    return rows


def _canonical(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is not JSON: {exc}") from exc
    if not isinstance(value, dict) or data != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_names(publication: Mapping[str, Any]) -> list[str]:
    release_id = publication["release_id"]
    names = [PUBLICATION_NAME, NOTES_NAME, QUALIFICATION_NAME, PROMOTION_NAME, RECORD_NAME]
    for component in ("drone", "gc"):
        for filename in sorted(COMPONENT_FILES):
            names.append(component_asset_name(release_id, component, filename))
    return names


def _write_asset_tree(
    staging: Path,
    publication: Mapping[str, Any],
    assets: Mapping[str, bytes],
) -> None:
    for name in (PUBLICATION_NAME, NOTES_NAME, QUALIFICATION_NAME, PROMOTION_NAME, RECORD_NAME):
        (staging / name).write_bytes(assets[name])
    release_id = publication["release_id"]
    for component in ("drone", "gc"):
        directory = staging / component
        directory.mkdir(mode=0o755)
        for filename in COMPONENT_FILES:
            remote_name = component_asset_name(release_id, component, filename)
            (directory / filename).write_bytes(assets[remote_name])


def _validate_assets(
    staging: Path,
    publication: Mapping[str, Any],
    *,
    bundle_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> None:
    qualification_data = (staging / QUALIFICATION_NAME).read_bytes()
    promotion_data = (staging / PROMOTION_NAME).read_bytes()
    notes_data = (staging / NOTES_NAME).read_bytes()
    record_data = (staging / RECORD_NAME).read_bytes()
    qualification = _canonical(qualification_data, label="qualification evidence")
    promotion = _canonical(promotion_data, label="promotion attestation")
    notes = _canonical(notes_data, label="release notes")
    record = _canonical(record_data, label="release record")
    registry.validate("qualification-evidence", qualification)
    registry.validate("promotion-evidence", promotion)
    verify_release_notes(notes, registry)
    verify_release_record(record, registry)
    checks = {
        "qualification_evidence_sha256": _sha256(qualification_data),
        "promotion_attestation_sha256": _sha256(promotion_data),
        "release_notes_sha256": _sha256(notes_data),
        "release_record_sha256": _sha256(record_data),
    }
    for field, actual in checks.items():
        if publication[field] != actual:
            raise ContractError(f"publication {field} disagrees with downloaded asset")
    paired = {}
    for component in ("drone", "gc"):
        verified = verify_bundle(
            staging / component,
            bundle_trust,
            registry=registry,
            host_limits=host_limits,
        )
        paired[component] = verified
        expected = publication["components"][component]
        observed = {
            "archive_sha256": verified.archive_sha256,
            "bundle_manifest_sha256": _sha256(verified.paths.bundle_manifest.read_bytes()),
            "signature_sha256": _sha256(verified.paths.signature.read_bytes()),
            "compressed_bytes": verified.compressed_bytes,
        }
        if observed != expected:
            raise ContractError(f"published {component} component identity disagreement")
    if paired["drone"].release_manifest != paired["gc"].release_manifest:
        raise ContractError("downloaded paired release manifests disagree")
    release = paired["drone"].release_manifest
    if release["release_id"] != publication["release_id"] or release["version"] != publication["version"]:
        raise ContractError("downloaded bundle identity differs from publication")
    if publication["release_manifest_sha256"] != paired["drone"].bundle_manifest["release_manifest_sha256"]:
        raise ContractError("publication release-manifest identity disagreement")


def fetch_release(
    source: ReleaseSource,
    version: str,
    cache_root: Path,
    *,
    bundle_trust: Path | Mapping[str, Any],
    status_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
    fetched_at: str,
) -> CachedRelease:
    failed = source.failed_attempt(version)
    versions = set(source.list_versions())
    if version not in versions:
        if failed is not None:
            raise ContractError("version is an unusable failed qualification attempt")
        raise ContractError("qualified release version does not exist")
    publication_data = source.read_asset(version, PUBLICATION_NAME)
    publication = _canonical(publication_data, label="release publication")
    bundle_store = (
        load_trusted_signers(bundle_trust, registry)
        if isinstance(bundle_trust, Path)
        else bundle_trust
    )
    verify_release_publication(publication, bundle_store, registry)
    if publication["version"] != version:
        raise ContractError("release publication version differs from requested tag")
    status_index_data = source.latest_status_index()
    status_index = _canonical(status_index_data, label="release-status index")
    status_store = (
        load_trusted_signers(status_trust, registry)
        if isinstance(status_trust, Path)
        else status_trust
    )
    statement = latest_status(
        status_index,
        release_id=publication["release_id"],
        version=version,
        trusted_signers=status_store,
        registry=registry,
    )
    require_fetchable_status(statement)

    assets = {PUBLICATION_NAME: publication_data}
    for name in _asset_names(publication):
        if name != PUBLICATION_NAME:
            assets[name] = source.read_asset(version, name)
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / version / publication["release_id"]
    if destination.exists():
        cached = load_cached_release(
            destination,
            bundle_trust=bundle_store,
            status_trust=status_store,
            registry=registry,
            host_limits=host_limits,
        )
        if cached.publication != publication:
            raise ContractError("cached version is bound to different immutable publication")
        refresh_cached_status(
            destination,
            status_index_data,
            status_trust=status_store,
            registry=registry,
        )
        return load_cached_release(
            destination,
            bundle_trust=bundle_store,
            status_trust=status_store,
            registry=registry,
            host_limits=host_limits,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{publication['release_id']}.", dir=destination.parent))
    try:
        _write_asset_tree(staging, publication, assets)
        _validate_assets(
            staging,
            publication,
            bundle_trust=bundle_store,
            registry=registry,
            host_limits=host_limits,
        )
        (staging / STATUS_INDEX_NAME).write_bytes(status_index_data)
        record = {
            "schema": "iii.qualified-release-cache/v1",
            "version": version,
            "release_id": publication["release_id"],
            "publication_id": publication["publication_id"],
            "status_index_id": status_index["index_id"],
            "fetched_at": fetched_at,
        }
        (staging / CACHE_RECORD_NAME).write_bytes(canonical_json(record) + b"\n")
        os.chmod(staging, 0o755)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_cached_release(
        destination,
        bundle_trust=bundle_store,
        status_trust=status_store,
        registry=registry,
        host_limits=host_limits,
    )


def load_cached_release(
    root: Path,
    *,
    bundle_trust: Path | Mapping[str, Any],
    status_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> CachedRelease:
    if root.is_symlink() or not root.is_dir():
        raise ContractError("cached release directory is missing or unsafe")
    publication = _canonical((root / PUBLICATION_NAME).read_bytes(), label="cached publication")
    bundle_store = (
        load_trusted_signers(bundle_trust, registry)
        if isinstance(bundle_trust, Path)
        else bundle_trust
    )
    verify_release_publication(publication, bundle_store, registry)
    notes = _canonical((root / NOTES_NAME).read_bytes(), label="cached release notes")
    verify_release_notes(notes, registry)
    record = _canonical((root / RECORD_NAME).read_bytes(), label="cached release record")
    verify_release_record(record, registry)
    _validate_assets(
        root,
        publication,
        bundle_trust=bundle_store,
        registry=registry,
        host_limits=host_limits,
    )
    index = _canonical((root / STATUS_INDEX_NAME).read_bytes(), label="cached status index")
    status_store = (
        load_trusted_signers(status_trust, registry)
        if isinstance(status_trust, Path)
        else status_trust
    )
    statement = latest_status(
        index,
        release_id=publication["release_id"],
        version=publication["version"],
        trusted_signers=status_store,
        registry=registry,
    )
    return CachedRelease(root, publication, notes, record, statement, index)


def refresh_cached_status(
    root: Path,
    status_index_data: bytes,
    *,
    status_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    publication = _canonical((root / PUBLICATION_NAME).read_bytes(), label="cached publication")
    index = _canonical(status_index_data, label="release-status index")
    store = (
        load_trusted_signers(status_trust, registry)
        if isinstance(status_trust, Path)
        else status_trust
    )
    statement = latest_status(
        index,
        release_id=publication["release_id"],
        version=publication["version"],
        trusted_signers=store,
        registry=registry,
    )
    current_path = root / STATUS_INDEX_NAME
    if current_path.exists():
        current = _canonical(current_path.read_bytes(), label="cached status index")
        verify_status_index(current, store, registry)
        current_sequence = int(current["sequence"])
        incoming_sequence = int(index["sequence"])
        if incoming_sequence < current_sequence:
            raise ContractError("stale release-status index cannot replace the cached safety state")
        if incoming_sequence == current_sequence:
            if index["index_id"] != current["index_id"]:
                raise ContractError("conflicting release-status index at the cached sequence")
            return latest_status(
                current,
                release_id=publication["release_id"],
                version=publication["version"],
                trusted_signers=store,
                registry=registry,
            )
        retained = current["statements"]
        if index["statements"][: len(retained)] != retained:
            raise ContractError("release-status index does not extend the cached signed chain")
    temporary = root / f".{STATUS_INDEX_NAME}.{os.getpid()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(status_index_data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / STATUS_INDEX_NAME)
    finally:
        temporary.unlink(missing_ok=True)
    return statement


def materialize_cached_release(
    cached: CachedRelease,
    destination: Path,
    *,
    bundle_trust: Path | Mapping[str, Any],
    registry: ContractRegistry,
    host_limits: Mapping[str, int],
) -> Path:
    require_fetchable_status(cached.status)
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ContractError(f"refusing to replace local deployment handoff: {destination}")
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for component in ("drone", "gc"):
            extract_bundle(
                cached.root / component,
                staging / component,
                bundle_trust,
                registry=registry,
                host_limits=host_limits,
            )
        (staging / PUBLICATION_NAME).write_bytes(
            canonical_json(cached.publication) + b"\n"
        )
        (staging / NOTES_NAME).write_bytes(canonical_json(cached.notes) + b"\n")
        (staging / RECORD_NAME).write_bytes(canonical_json(cached.record) + b"\n")
        os.chmod(staging, 0o755)
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class GitHubReleaseSource:
    """Read-only authenticated GitHub release adapter; no aircraft/network side effects."""

    def __init__(self, repository: str) -> None:
        self.repository = repository

    def _run(self, arguments: Sequence[str], *, binary: bool = False) -> bytes | str:
        process = subprocess.run(
            ["gh", *arguments], capture_output=True, text=not binary, check=False
        )
        if process.returncode:
            error = process.stderr if not binary else process.stderr.decode(errors="replace")
            raise ContractError(error.strip() or "GitHub release request failed")
        return process.stdout

    def _release(self, tag: str) -> dict[str, Any]:
        value = self._run(
            ["api", f"repos/{self.repository}/releases/tags/{tag}"]
        )
        return json.loads(str(value))

    def list_versions(self) -> Sequence[str]:
        value = self._run(
            ["release", "list", "--repo", self.repository, "--limit", "1000", "--json", "tagName,isDraft,isPrerelease"]
        )
        rows = json.loads(str(value))
        return sorted(
            row["tagName"]
            for row in rows
            if not row["isDraft"]
            and not row["isPrerelease"]
            and row["tagName"].startswith("v")
        )

    def read_asset(self, tag: str, name: str) -> bytes:
        release = self._release(tag)
        matches = [asset for asset in release.get("assets", []) if asset["name"] == name]
        if len(matches) != 1:
            raise ContractError(f"release {tag} does not contain exactly one {name}")
        return self._run(
            ["api", matches[0]["url"], "-H", "Accept: application/octet-stream"],
            binary=True,
        )  # type: ignore[return-value]

    def latest_status_index(self) -> bytes:
        value = self._run(
            ["release", "list", "--repo", self.repository, "--limit", "1000", "--json", "tagName,isDraft"]
        )
        rows = json.loads(str(value))
        tags = [
            row["tagName"]
            for row in rows
            if not row["isDraft"] and row["tagName"].startswith("iii-status-")
        ]
        try:
            latest = max(tags, key=lambda tag: int(tag.removeprefix("iii-status-")))
        except (ValueError, TypeError):
            raise ContractError("no valid signed release-status index is published")
        return self.read_asset(latest, STATUS_INDEX_NAME)

    def failed_attempt(self, version: str) -> Mapping[str, Any] | None:
        try:
            data = self.read_asset(f"iii-attempt-{version}", "qualification-attempt.json")
        except ContractError:
            return None
        return _canonical(data, label="qualification attempt")
