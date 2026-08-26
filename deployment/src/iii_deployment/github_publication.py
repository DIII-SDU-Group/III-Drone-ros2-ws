"""Fail-closed GitHub Release publication with immutable byte comparison."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Protocol

from .bundle import COMPONENT_FILES
from .contracts import ContractError, ContractRegistry, SEMVER, canonical_json
from .qualified_release import (
    NOTES_NAME,
    PROMOTION_NAME,
    PUBLICATION_NAME,
    QUALIFICATION_NAME,
    RECORD_NAME,
    component_asset_name,
    compare_immutable_assets,
    verify_qualification_attempt,
    verify_release_notes,
    verify_release_publication,
    verify_release_record,
)


ATTEMPT_NAME = "qualification-attempt.json"
ATTEMPT_LOG_NAME = "qualification-attempt.log"
STATUS_STATEMENT_NAME = "release-status-statement.json"
STATUS_INDEX_NAME = "release-status-index.json"


class ReleasePublisher(Protocol):
    def release(self, tag: str) -> Mapping[str, Any] | None: ...
    def assets(self, tag: str) -> Mapping[str, bytes]: ...
    def tag_commit(self, tag: str) -> str | None: ...
    def create_draft(self, tag: str, *, title: str, body: str) -> None: ...
    def create_release(self, tag: str, *, target: str, title: str, body: str) -> None: ...
    def upload(self, tag: str, name: str, path: Path) -> None: ...
    def set_draft(self, tag: str, draft: bool) -> None: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_file(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def expected_release_assets(
    publication: Mapping[str, Any], asset_paths: Mapping[str, Path]
) -> dict[str, str]:
    required = {PUBLICATION_NAME, NOTES_NAME, QUALIFICATION_NAME, PROMOTION_NAME, RECORD_NAME}
    for component in ("drone", "gc"):
        required.update(
            component_asset_name(publication["release_id"], component, name)
            for name in COMPONENT_FILES
        )
    missing = sorted(required - set(asset_paths))
    extra = sorted(set(asset_paths) - required)
    if missing or extra:
        raise ContractError(
            f"qualified publication asset set differs; missing={missing}; extra={extra}"
        )
    return {name: _sha256_file(asset_paths[name]) for name in sorted(required)}


def _existing_hashes(client: ReleasePublisher, tag: str) -> dict[str, str]:
    return {name: _sha256_bytes(data) for name, data in client.assets(tag).items()}


def _complete_exact_assets(
    client: ReleasePublisher,
    tag: str,
    expected: Mapping[str, str],
    paths: Mapping[str, Path],
) -> bool:
    """Resume a partial immutable release without ever overwriting an asset."""
    existing = _existing_hashes(client, tag)
    unexpected = sorted(set(existing) - set(expected))
    changed = sorted(
        name for name in set(existing) & set(expected) if existing[name] != expected[name]
    )
    if unexpected or changed:
        raise ContractError(
            f"release contains different immutable assets; unexpected={unexpected}; changed={changed}"
        )
    missing = sorted(set(expected) - set(existing))
    for name in missing:
        client.upload(tag, name, paths[name])
    compare_immutable_assets(expected, _existing_hashes(client, tag))
    return bool(missing)


def publish_qualified_release(
    client: ReleasePublisher,
    *,
    version: str,
    source_commit: str,
    asset_paths: Mapping[str, Path],
    bundle_trust: Mapping[str, Any],
    registry: ContractRegistry,
) -> str:
    """Create/finish a draft and publish only after exact byte verification."""

    if not SEMVER.fullmatch(version):
        raise ContractError("qualified GitHub release version is not strict SemVer")
    if client.tag_commit(version) != source_commit:
        raise ContractError("remote version tag differs from qualified source commit")
    if client.release(f"iii-attempt-{version}") is not None:
        raise ContractError("version has a retained failed qualification attempt")
    publication = _canonical_file(asset_paths[PUBLICATION_NAME], label="release publication")
    notes = _canonical_file(asset_paths[NOTES_NAME], label="release notes")
    record = _canonical_file(asset_paths[RECORD_NAME], label="release record")
    verify_release_publication(publication, bundle_trust, registry)
    verify_release_notes(notes, registry)
    verify_release_record(record, registry)
    if publication["version"] != version or publication["source_commit"] != source_commit:
        raise ContractError("signed publication differs from remote tag candidate")
    expected = expected_release_assets(publication, asset_paths)
    current = client.release(version)
    if current is None:
        client.create_draft(version, title=f"III-Drone {version}", body=notes["markdown"])
        current = client.release(version)
        if current is None or not current.get("draft"):
            raise ContractError("GitHub did not create the qualified release as a draft")
    if current.get("body") != notes["markdown"]:
        raise ContractError("GitHub release body differs from machine-derived notes")
    existing = _existing_hashes(client, version)
    if not current.get("draft"):
        compare_immutable_assets(expected, existing)
        return "no-op"
    _complete_exact_assets(client, version, expected, asset_paths)
    current = client.release(version)
    if current is None or current.get("body") != notes["markdown"] or not current.get("draft"):
        raise ContractError("qualified draft changed during publication")
    client.set_draft(version, False)
    published = client.release(version)
    if published is None or published.get("draft") or published.get("body") != notes["markdown"]:
        raise ContractError("qualified release did not become an exact published record")
    compare_immutable_assets(expected, _existing_hashes(client, version))
    return "published"


def publish_failed_attempt(
    client: ReleasePublisher,
    *,
    attempt_path: Path,
    log_path: Path,
    registry: ContractRegistry,
) -> str:
    attempt = _canonical_file(attempt_path, label="qualification attempt")
    verify_qualification_attempt(attempt, registry)
    if _sha256_file(log_path) != attempt["log_sha256"]:
        raise ContractError("qualification failure log differs from attempt record")
    version = attempt["version"]
    if client.tag_commit(version) != attempt["source_commit"]:
        raise ContractError("failed attempt differs from immutable version tag")
    qualified = client.release(version)
    if qualified is not None and not qualified.get("draft"):
        client.set_draft(version, True)
    tag = f"iii-attempt-{version}"
    paths = {ATTEMPT_NAME: attempt_path, ATTEMPT_LOG_NAME: log_path}
    expected = {name: _sha256_file(path) for name, path in paths.items()}
    body = (
        f"Qualification failed for {version} at stage `{attempt['failure_stage']}`.\n\n"
        "This protected SemVer is permanently unusable and contains no deployable assets.\n"
    )
    current = client.release(tag)
    if current is not None:
        if current.get("draft") or current.get("body") != body or client.tag_commit(tag) != attempt["source_commit"]:
            raise ContractError("failed-attempt release metadata differs from its retained record")
        changed = _complete_exact_assets(client, tag, expected, paths)
        return "published" if changed else "no-op"
    if client.tag_commit(tag) is not None:
        raise ContractError("failed-attempt tag exists without its immutable release record")
    client.create_release(
        tag,
        target=attempt["source_commit"],
        title=f"Failed qualification: {version}",
        body=body,
    )
    for name, path in paths.items():
        client.upload(tag, name, path)
    compare_immutable_assets(expected, _existing_hashes(client, tag))
    return "published"


def publish_release_status(
    client: ReleasePublisher,
    *,
    statement_path: Path,
    index_path: Path,
    target_commit: str,
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> str:
    from .release_status import verify_status_index, verify_status_statement

    statement = _canonical_file(statement_path, label="release-status statement")
    index = _canonical_file(index_path, label="release-status index")
    verify_status_statement(statement, trusted_signers, registry)
    verify_status_index(index, trusted_signers, registry)
    if index["sequence"] != statement["sequence"] or index["statements"][-1] != statement:
        raise ContractError("release-status publication does not append its statement")
    tag = f"iii-status-{statement['sequence']}"
    paths = {STATUS_STATEMENT_NAME: statement_path, STATUS_INDEX_NAME: index_path}
    expected = {name: _sha256_file(path) for name, path in paths.items()}
    body = (
        f"Signed release status sequence {statement['sequence']}: "
        f"{statement['version']} is `{statement['status']}`.\n"
    )
    current = client.release(tag)
    if current is not None:
        if current.get("draft") or current.get("body") != body or client.tag_commit(tag) != target_commit:
            raise ContractError("release-status metadata differs from its signed record")
        changed = _complete_exact_assets(client, tag, expected, paths)
        return "published" if changed else "no-op"
    if client.tag_commit(tag) is not None:
        raise ContractError("release-status tag exists without its immutable index release")
    client.create_release(
        tag,
        target=target_commit,
        title=f"III release status {statement['sequence']}",
        body=body,
    )
    for name, path in paths.items():
        client.upload(tag, name, path)
    compare_immutable_assets(expected, _existing_hashes(client, tag))
    return "published"


class GhReleasePublisher:
    """Minimal authenticated GitHub adapter; commands never interpolate through a shell."""

    def __init__(self, repository: str) -> None:
        self.repository = repository

    def _run(self, arguments: list[str], *, binary: bool = False) -> bytes | str:
        process = subprocess.run(
            ["gh", *arguments], capture_output=True, text=not binary, check=False
        )
        if process.returncode:
            error = process.stderr if not binary else process.stderr.decode(errors="replace")
            raise ContractError(error.strip() or "GitHub publication request failed")
        return process.stdout

    def release(self, tag: str) -> Mapping[str, Any] | None:
        process = subprocess.run(
            ["gh", "api", f"repos/{self.repository}/releases/tags/{tag}"],
            capture_output=True, text=True, check=False,
        )
        if process.returncode:
            if "HTTP 404" in process.stderr or "Not Found" in process.stderr:
                return None
            raise ContractError(process.stderr.strip() or "cannot inspect GitHub release")
        value = json.loads(process.stdout)
        return {"id": value["id"], "draft": value["draft"], "body": value.get("body") or ""}

    def assets(self, tag: str) -> Mapping[str, bytes]:
        value = self._run(["api", f"repos/{self.repository}/releases/tags/{tag}"])
        release = json.loads(str(value))
        assets: dict[str, bytes] = {}
        for asset in release.get("assets", []):
            name = asset["name"]
            if name in assets:
                raise ContractError(f"GitHub release contains duplicate asset {name}")
            assets[name] = self._run(
                ["api", asset["url"], "-H", "Accept: application/octet-stream"],
                binary=True,
            )  # type: ignore[assignment]
        return assets

    def tag_commit(self, tag: str) -> str | None:
        process = subprocess.run(
            ["gh", "api", f"repos/{self.repository}/commits/{tag}", "--jq", ".sha"],
            capture_output=True, text=True, check=False,
        )
        if process.returncode:
            if "HTTP 404" in process.stderr or "Not Found" in process.stderr:
                return None
            raise ContractError(process.stderr.strip() or "cannot resolve remote tag commit")
        return process.stdout.strip()

    def create_draft(self, tag: str, *, title: str, body: str) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            self._run([
                "release", "create", tag, "--repo", self.repository, "--verify-tag",
                "--draft", "--title", title, "--notes-file", stream.name,
            ])

    def create_release(self, tag: str, *, target: str, title: str, body: str) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            self._run([
                "release", "create", tag, "--repo", self.repository, "--target", target,
                "--title", title, "--notes-file", stream.name,
            ])

    def upload(self, tag: str, name: str, path: Path) -> None:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ContractError("GitHub release asset name is unsafe")
        if path.is_symlink() or not path.is_file():
            raise ContractError("GitHub release asset path is missing or unsafe")
        if path.name == name:
            self._run(["release", "upload", tag, str(path), "--repo", self.repository])
            return
        # `gh release upload file#label` changes only the display label, not the
        # immutable asset filename. Stage the same bytes under the contract name.
        with tempfile.TemporaryDirectory(prefix="iii-release-upload-") as directory:
            staged = Path(directory) / name
            try:
                os.link(path, staged)
            except OSError:
                shutil.copyfile(path, staged)
            self._run(["release", "upload", tag, str(staged), "--repo", self.repository])

    def set_draft(self, tag: str, draft: bool) -> None:
        current = self.release(tag)
        if current is None:
            raise ContractError("cannot change draft state of a missing release")
        self._run([
            "api", "--method", "PATCH", f"repos/{self.repository}/releases/{current['id']}",
            "-F", f"draft={'true' if draft else 'false'}",
        ])
