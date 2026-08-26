from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iii_deployment.bundle import COMPONENT_FILES
from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json
import iii_deployment.github_publication as publication_module
from iii_deployment.github_publication import (
    ATTEMPT_LOG_NAME,
    ATTEMPT_NAME,
    GhReleasePublisher,
    publish_failed_attempt,
    publish_qualified_release,
    publish_release_status,
    STATUS_INDEX_NAME,
    STATUS_STATEMENT_NAME,
)
from iii_deployment.qualified_release import (
    NOTES_NAME, PROMOTION_NAME, PUBLICATION_NAME, QUALIFICATION_NAME, RECORD_NAME,
    component_asset_name, create_qualification_attempt,
)
from iii_deployment.release_status import append_status
from iii_deployment.signers import (
    add_trusted_signer, generate_signer, load_trusted_signers, signer_proof,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
VERSION = "v1.2.3"
COMMIT = "1" * 40
RELEASE_ID = "2" * 64


class FakePublisher:
    def __init__(self) -> None:
        self.tags = {VERSION: COMMIT}
        self.releases: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def release(self, tag):
        value = self.releases.get(tag)
        return None if value is None else {key: value[key] for key in ("id", "draft", "body")}

    def assets(self, tag):
        return dict(self.releases[tag]["assets"])

    def tag_commit(self, tag):
        return self.tags.get(tag)

    def create_draft(self, tag, *, title, body):
        self.calls.append(("create-draft", tag))
        self.releases[tag] = {"id": len(self.releases) + 1, "draft": True, "body": body, "assets": {}}

    def create_release(self, tag, *, target, title, body):
        self.calls.append(("create-release", tag, target))
        self.tags[tag] = target
        self.releases[tag] = {"id": len(self.releases) + 1, "draft": False, "body": body, "assets": {}}

    def upload(self, tag, name, path):
        self.calls.append(("upload", tag, name))
        if name in self.releases[tag]["assets"]:
            raise AssertionError("publisher must not overwrite")
        self.releases[tag]["assets"][name] = path.read_bytes()

    def set_draft(self, tag, draft):
        self.calls.append(("set-draft", tag, draft))
        self.releases[tag]["draft"] = draft


def _canonical(path: Path, value: dict) -> Path:
    path.write_bytes(canonical_json(value) + b"\n")
    return path


def _assets(tmp_path: Path) -> dict[str, Path]:
    values = {
        PUBLICATION_NAME: {"version": VERSION, "release_id": RELEASE_ID, "source_commit": COMMIT},
        NOTES_NAME: {"markdown": "# exact machine notes\n"},
        QUALIFICATION_NAME: {"evidence": True},
        PROMOTION_NAME: {"promotion": True},
        RECORD_NAME: {"record": True},
    }
    paths = {name: _canonical(tmp_path / name, value) for name, value in values.items()}
    for component in ("drone", "gc"):
        for filename in COMPONENT_FILES:
            name = component_asset_name(RELEASE_ID, component, filename)
            path = tmp_path / name
            path.write_bytes(f"{component}:{filename}\n".encode())
            paths[name] = path
    return paths


@pytest.fixture(autouse=True)
def bypass_already_covered_crypto(monkeypatch):
    monkeypatch.setattr(publication_module, "verify_release_publication", lambda *_a, **_k: None)
    monkeypatch.setattr(publication_module, "verify_release_notes", lambda *_a, **_k: None)
    monkeypatch.setattr(publication_module, "verify_release_record", lambda *_a, **_k: None)


def _publish(client: FakePublisher, paths: dict[str, Path]) -> str:
    return publish_qualified_release(
        client, version=VERSION, source_commit=COMMIT, asset_paths=paths,
        bundle_trust={}, registry=REGISTRY,
    )


def test_publication_uses_verified_draft_then_is_byte_idempotent(tmp_path: Path) -> None:
    client = FakePublisher()
    paths = _assets(tmp_path)
    assert _publish(client, paths) == "published"
    assert client.releases[VERSION]["draft"] is False
    assert client.releases[VERSION]["body"] == "# exact machine notes\n"
    assert set(client.releases[VERSION]["assets"]) == set(paths)
    calls = list(client.calls)
    assert _publish(client, paths) == "no-op"
    assert client.calls == calls


def test_partial_matching_draft_resumes_without_overwrite(tmp_path: Path) -> None:
    client = FakePublisher()
    paths = _assets(tmp_path)
    client.create_draft(VERSION, title="title", body="# exact machine notes\n")
    first = sorted(paths)[0]
    client.upload(VERSION, first, paths[first])
    assert _publish(client, paths) == "published"
    assert len([call for call in client.calls if call[0] == "upload"]) == len(paths)


def test_duplicate_version_with_changed_or_extra_bytes_is_refused(tmp_path: Path) -> None:
    client = FakePublisher()
    paths = _assets(tmp_path)
    assert _publish(client, paths) == "published"
    client.releases[VERSION]["assets"][NOTES_NAME] = b"tampered\n"
    with pytest.raises(ContractError, match="different immutable assets"):
        _publish(client, paths)
    client.releases[VERSION]["assets"][NOTES_NAME] = paths[NOTES_NAME].read_bytes()
    client.releases[VERSION]["assets"]["extra.bin"] = b"extra"
    with pytest.raises(ContractError, match="different immutable assets"):
        _publish(client, paths)


def test_remote_tag_mismatch_is_rejected_before_release_mutation(tmp_path: Path) -> None:
    client = FakePublisher()
    client.tags[VERSION] = "9" * 40
    with pytest.raises(ContractError, match="tag differs"):
        _publish(client, _assets(tmp_path))
    assert client.calls == []


def test_failed_attempt_retracts_draft_surface_and_permanently_blocks_version(tmp_path: Path) -> None:
    client = FakePublisher()
    paths = _assets(tmp_path)
    assert _publish(client, paths) == "published"
    log = tmp_path / ATTEMPT_LOG_NAME
    log.write_bytes(b"arm64 tests failed\n")
    attempt = create_qualification_attempt(
        version=VERSION, source_commit=COMMIT, recorded_at="2026-08-26T12:00:00Z",
        failure_stage="arm64-tests",
        findings=({"id": "ARM64_TEST_FAILED", "detail": "target test command failed"},),
        log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(), registry=REGISTRY,
    )
    attempt_path = _canonical(tmp_path / ATTEMPT_NAME, attempt)
    assert publish_failed_attempt(
        client, attempt_path=attempt_path, log_path=log, registry=REGISTRY
    ) == "published"
    assert client.releases[VERSION]["draft"] is True
    failed_tag = f"iii-attempt-{VERSION}"
    assert set(client.releases[failed_tag]["assets"]) == {ATTEMPT_NAME, ATTEMPT_LOG_NAME}
    assert publish_failed_attempt(
        client, attempt_path=attempt_path, log_path=log, registry=REGISTRY
    ) == "no-op"
    with pytest.raises(ContractError, match="failed qualification"):
        _publish(client, paths)


def test_partial_failed_attempt_release_resumes_without_overwrite(tmp_path: Path) -> None:
    client = FakePublisher()
    log = tmp_path / ATTEMPT_LOG_NAME
    log.write_bytes(b"gc build failed\n")
    attempt = create_qualification_attempt(
        version=VERSION, source_commit=COMMIT, recorded_at="2026-08-26T12:00:00Z",
        failure_stage="gc-build",
        findings=({"id": "GC_BUILD_FAILED", "detail": "pinned image build failed"},),
        log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(), registry=REGISTRY,
    )
    attempt_path = _canonical(tmp_path / ATTEMPT_NAME, attempt)
    tag = f"iii-attempt-{VERSION}"
    body = (
        f"Qualification failed for {VERSION} at stage `gc-build`.\n\n"
        "This protected SemVer is permanently unusable and contains no deployable assets.\n"
    )
    client.create_release(tag, target=COMMIT, title="partial", body=body)
    client.upload(tag, ATTEMPT_NAME, attempt_path)
    assert publish_failed_attempt(
        client, attempt_path=attempt_path, log_path=log, registry=REGISTRY
    ) == "published"
    assert set(client.releases[tag]["assets"]) == {ATTEMPT_NAME, ATTEMPT_LOG_NAME}


def test_signed_status_publication_is_serial_monotonic_and_immutable(tmp_path: Path) -> None:
    key = tmp_path / "status.pem"
    public = tmp_path / "status.public.json"
    generate_signer(key, public, authority="release-status", registry=REGISTRY)
    store_path = tmp_path / "status-trust.json"
    add_trusted_signer(store_path, public, signer_proof(key), REGISTRY)
    trust = load_trusted_signers(store_path, REGISTRY)
    initial = append_status(
        None, operation_id="qualified-release-test",
        release_id=RELEASE_ID, version=VERSION, status="qualified",
        reason="qualification passed", superseding_version=None,
        expected_statement_id=None, recorded_at="2026-08-26T12:00:00Z",
        private_key_path=key, trusted_signers=trust, registry=REGISTRY,
    )
    assert initial is not None
    statement, index = initial
    statement_path = _canonical(tmp_path / STATUS_STATEMENT_NAME, statement)
    index_path = _canonical(tmp_path / STATUS_INDEX_NAME, index)
    client = FakePublisher()
    assert publish_release_status(
        client, statement_path=statement_path, index_path=index_path,
        target_commit=COMMIT, trusted_signers=trust, registry=REGISTRY,
    ) == "published"
    assert client.tags["iii-status-1"] == COMMIT
    assert publish_release_status(
        client, statement_path=statement_path, index_path=index_path,
        target_commit=COMMIT, trusted_signers=trust, registry=REGISTRY,
    ) == "no-op"
    unsafe = append_status(
        index, operation_id="unsafe-release-test",
        release_id=RELEASE_ID, version=VERSION, status="unsafe",
        reason="safety bulletin", superseding_version="v1.2.4",
        expected_statement_id=statement["statement_id"],
        recorded_at="2026-08-26T13:00:00Z", private_key_path=key,
        trusted_signers=trust, registry=REGISTRY,
    )
    assert unsafe is not None and unsafe[0]["sequence"] == 2
    with pytest.raises(ContractError, match="stale"):
        append_status(
            unsafe[1], operation_id="stale-release-test",
            release_id=RELEASE_ID, version=VERSION, status="withdrawn",
            reason="stale request", superseding_version=None,
            expected_statement_id=statement["statement_id"],
            recorded_at="2026-08-26T14:00:00Z", private_key_path=key,
            trusted_signers=trust, registry=REGISTRY,
        )


def test_gh_adapter_stages_contract_asset_name_instead_of_using_display_label(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "generic.bin"
    source.write_bytes(b"exact bytes\n")
    client = GhReleasePublisher("owner/repository")
    observed = {}

    def fake_run(arguments, **_kwargs):
        upload = Path(arguments[3])
        observed["name"] = upload.name
        observed["bytes"] = upload.read_bytes()
        return ""

    monkeypatch.setattr(client, "_run", fake_run)
    client.upload("v1.2.3", "contract-name.bin", source)
    assert observed == {"name": "contract-name.bin", "bytes": b"exact bytes\n"}
