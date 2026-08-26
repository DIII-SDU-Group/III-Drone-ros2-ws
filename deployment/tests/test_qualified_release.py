from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iii_deployment.bundle import (
    COMPONENT_FILES,
    extract_bundle,
    inspect_bundle,
    load_bundle_limits,
    package_bundle_set,
)
from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json, content_identity
from iii_deployment.qualified_release import (
    NOTES_NAME,
    PROMOTION_NAME, RECORD_NAME,
    PUBLICATION_NAME,
    QUALIFICATION_NAME,
    compare_immutable_assets,
    component_asset_name,
    create_qualification_attempt, create_release_record,
    create_release_notes,
    create_release_publication,
    verify_qualification_attempt,
    verify_release_notes,
    verify_release_publication,
    write_canonical,
)
from iii_deployment.release_registry import (
    GitHubReleaseSource,
    STATUS_INDEX_NAME,
    fetch_release,
    load_cached_release,
    materialize_cached_release,
    refresh_cached_status,
)
from iii_deployment.release_status import (
    create_status_index,
    create_status_statement,
    latest_status,
    require_fetchable_status,
    verify_status_index,
)
from iii_deployment.signers import (
    add_trusted_signer,
    generate_signer,
    load_trusted_signers,
    signer_proof,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
LIMITS = load_bundle_limits(ROOT / "deployment" / "operational-policy.json")
FIXTURE = ROOT / "deployment" / "tests" / "fixtures" / "release_manifest.json"
IMPACT = json.loads((ROOT / "deployment" / "governance" / "change-impact-policy.json").read_text())


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _promotion_attestation(
    private: Ed25519PrivateKey, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signer_id = hashlib.sha256(public).hexdigest()
    value: dict[str, Any] = {
        "schema_version": "1",
        "attestation_id": "0" * 64,
        "workspace_commit": manifest["source"]["workspace_commit"],
        "source_content_identity": manifest["source"]["content_identity"],
        "dependency_lock_sha256": manifest["dependency_lock"]["sha256"],
        "policy_sha256": content_identity(IMPACT),
        "categories": [
            {"id": "static-unit", "status": "passed", "summary_sha256": "7" * 64},
            {"id": "local-simulation", "status": "passed", "summary_sha256": "8" * 64},
        ],
        "waivers": [],
        "artifacts": [{"kind": "simulation-summary", "sha256": "9" * 64}],
        "signer_id": signer_id,
    }
    value["attestation_id"] = content_identity(
        {key: item for key, item in value.items() if key != "attestation_id"}
    )
    value["signature"] = _b64url(private.sign(canonical_json(value)))
    return value, {signer_id: _b64url(public)}


def _qualification(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "schema": "iii.qualification-evidence/v1",
        "evidence_type": "qualified-release-preflight",
        "source_commit": manifest["source"]["workspace_commit"],
        "version": manifest["version"],
        "dependency_lock_sha256": manifest["dependency_lock"]["sha256"],
        "governance_verified": True,
        "required_checks": [
            {"id": name, "status": "passed", "evidence_sha256": f"{index:x}" * 64}
            for index, name in enumerate(
                (
                    "arm64-build",
                    "arm64-tests",
                    "dependency-lock",
                    "deployment-contracts",
                    "gc-build",
                    "gc-tests",
                    "governance-audit",
                    "promotion-evidence",
                ),
                start=1,
            )
        ],
        "evidence_complete": True,
    }


@dataclass
class PublishedCase:
    root: Path
    version: str
    manifest: dict[str, Any]
    publication: dict[str, Any]
    notes: dict[str, Any]
    assets: dict[str, bytes]
    bundle_store: Path
    status_store: Path
    status_key: Path
    status_index: dict[str, Any]


def _published(tmp_path: Path) -> PublishedCase:
    bundle_key = tmp_path / "bundle.pem"
    bundle_public = tmp_path / "bundle.public.json"
    descriptor = generate_signer(
        bundle_key, bundle_public, authority="ci-qualified", registry=REGISTRY
    )
    bundle_store = tmp_path / "bundle-trust.json"
    add_trusted_signer(
        bundle_store, bundle_public, signer_proof(bundle_key), REGISTRY
    )
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest["signing"]["signer_id"] = descriptor["signer_id"]
    manifest_path = tmp_path / "release.json"
    write_canonical(manifest_path, manifest)
    roots = {}
    for component in manifest["components"]:
        payload = tmp_path / f"{component}-payload"
        (payload / "share").mkdir(parents=True)
        (payload / "share" / "payload.txt").write_text(component + "\n")
        roots[component] = payload
    bundle_paths = package_bundle_set(
        manifest_path,
        roots,
        bundle_key,
        tmp_path / f"{manifest['release_id']}.iii-release-v1",
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    verified = {
        name: inspect_bundle(
            paths.directory,
            bundle_store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )
        for name, paths in bundle_paths.items()
    }
    qualification = _qualification(manifest)
    qualification_path = tmp_path / QUALIFICATION_NAME
    write_canonical(qualification_path, qualification)
    promotion_private = Ed25519PrivateKey.generate()
    promotion, promotion_trust = _promotion_attestation(promotion_private, manifest)
    promotion_path = tmp_path / PROMOTION_NAME
    write_canonical(promotion_path, promotion)
    notes = create_release_notes(
        manifest,
        qualification,
        promotion,
        change_summary={
            "schema_version": "1",
            "summary_type": "iii.release-change-summary",
            "version": manifest["version"],
            "base_version": "v1.2.2",
            "base_commit": "0" * 40,
            "source_commit": manifest["source"]["workspace_commit"],
            "commits": [],
            "changed_paths": ["deployment/release.py"],
            "categories": {
                "drone": [], "gc": [], "missions": [], "configuration": [],
                "px4": [], "qgc": [], "host_provisioning": ["deployment/release.py"],
                "documentation": [],
            },
            "operator_changes": ["Signed paired release delivery"],
        },
        operator_changes=("Signed paired release delivery",),
        expected_downtime_s=120,
        pre_deploy_commands=(("iii", "field", "check"),),
        post_deploy_commands=(("iii", "deploy", "status"),),
        annotation=None,
        registry=REGISTRY,
    )
    notes_path = tmp_path / NOTES_NAME
    write_canonical(notes_path, notes)
    retained_bundle_paths = {
        component_asset_name(manifest["release_id"], component, filename): paths.directory / filename
        for component, paths in bundle_paths.items()
        for filename in COMPONENT_FILES
    }
    record = create_release_record(
        version=manifest["version"],
        release_id=manifest["release_id"],
        source_commit=manifest["source"]["workspace_commit"],
        repository="DIII-SDU-Group/III-Drone-ros2-ws",
        run_id="12345",
        run_attempt=1,
        build_inputs={"source_snapshot": manifest["source"]["snapshot_sha256"]},
        check_paths={"qualification-check.json": qualification_path},
        artifact_paths={
            QUALIFICATION_NAME: qualification_path,
            PROMOTION_NAME: promotion_path,
            NOTES_NAME: notes_path,
            **retained_bundle_paths,
        },
        signer_id=manifest["signing"]["signer_id"],
        created_at="2026-08-26T12:00:00Z",
        registry=REGISTRY,
    )
    record_path = tmp_path / RECORD_NAME
    write_canonical(record_path, record)
    publication = create_release_publication(
        drone=verified["drone"],
        gc=verified["gc"],
        qualification_evidence_path=qualification_path,
        promotion_attestation_path=promotion_path,
        release_notes_path=notes_path,
        release_record_path=record_path,
        promotion_trusted_signers=promotion_trust,
        impact_policy=IMPACT,
        private_key_path=bundle_key,
        created_at="2026-08-26T12:00:00Z",
        registry=REGISTRY,
    )
    assets = {
        PUBLICATION_NAME: canonical_json(publication) + b"\n",
        NOTES_NAME: notes_path.read_bytes(),
        QUALIFICATION_NAME: qualification_path.read_bytes(),
        PROMOTION_NAME: promotion_path.read_bytes(),
        RECORD_NAME: record_path.read_bytes(),
    }
    for component, paths in bundle_paths.items():
        for filename in COMPONENT_FILES:
            assets[component_asset_name(manifest["release_id"], component, filename)] = (
                paths.directory / filename
            ).read_bytes()

    status_key = tmp_path / "status.pem"
    status_public = tmp_path / "status.public.json"
    generate_signer(
        status_key, status_public, authority="release-status", registry=REGISTRY
    )
    status_store = tmp_path / "status-trust.json"
    add_trusted_signer(
        status_store, status_public, signer_proof(status_key), REGISTRY
    )
    status_trust = load_trusted_signers(status_store, REGISTRY)
    initial = create_status_statement(
        operation_id="qualified-release-test",
        release_id=manifest["release_id"],
        version=manifest["version"],
        status="qualified",
        reason="Qualification completed",
        superseding_version=None,
        recorded_at="2026-08-26T12:01:00Z",
        private_key_path=status_key,
        registry=REGISTRY,
        previous_global=None,
        previous_release=None,
    )
    status_index = create_status_index(
        [initial],
        generated_at="2026-08-26T12:01:00Z",
        private_key_path=status_key,
        trusted_signers=status_trust,
        registry=REGISTRY,
    )
    return PublishedCase(
        tmp_path,
        manifest["version"],
        manifest,
        publication,
        notes,
        assets,
        bundle_store,
        status_store,
        status_key,
        status_index,
    )


class FakeSource:
    def __init__(self, case: PublishedCase) -> None:
        self.case = case
        self.versions = [case.version]
        self.assets = dict(case.assets)
        self.index = canonical_json(case.status_index) + b"\n"
        self.attempts: dict[str, Mapping[str, Any]] = {}

    def list_versions(self) -> Sequence[str]:
        return self.versions

    def read_asset(self, tag: str, name: str) -> bytes:
        if tag != self.case.version or name not in self.assets:
            raise ContractError("missing fake release asset")
        return self.assets[name]

    def latest_status_index(self) -> bytes:
        return self.index

    def failed_attempt(self, version: str) -> Mapping[str, Any] | None:
        return self.attempts.get(version)


def test_status_chain_is_global_and_monotonic_per_release(tmp_path: Path) -> None:
    case = _published(tmp_path)
    trust = load_trusted_signers(case.status_store, REGISTRY)
    first = case.status_index["statements"][0]
    second = create_status_statement(
        operation_id="qualified-release-second",
        release_id="b" * 64,
        version="v2.0.0",
        status="qualified",
        reason="Second qualified release",
        superseding_version=None,
        recorded_at="2026-08-26T12:02:00Z",
        private_key_path=case.status_key,
        registry=REGISTRY,
        previous_global=first,
        previous_release=None,
    )
    withdrawn = create_status_statement(
        operation_id="withdraw-release-test",
        release_id=case.manifest["release_id"],
        version=case.version,
        status="withdrawn",
        reason="Compatibility defect",
        superseding_version="v2.0.0",
        recorded_at="2026-08-26T12:03:00Z",
        private_key_path=case.status_key,
        registry=REGISTRY,
        previous_global=second,
        previous_release=first,
    )
    index = create_status_index(
        [first, second, withdrawn],
        generated_at="2026-08-26T12:03:00Z",
        private_key_path=case.status_key,
        trusted_signers=trust,
        registry=REGISTRY,
    )
    latest = verify_status_index(index, trust, REGISTRY)
    assert latest[case.manifest["release_id"]]["status"] == "withdrawn"
    assert latest["b" * 64]["status"] == "qualified"
    with pytest.raises(ContractError, match="withdrawn"):
        require_fetchable_status(latest[case.manifest["release_id"]])
    tampered = json.loads(json.dumps(index))
    tampered["statements"][0]["reason"] = "hidden rewrite"
    with pytest.raises(ContractError):
        verify_status_index(tampered, trust, REGISTRY)


def test_release_notes_and_publication_are_identity_bound(tmp_path: Path) -> None:
    case = _published(tmp_path)
    verify_release_notes(case.notes, REGISTRY)
    trust = load_trusted_signers(case.bundle_store, REGISTRY)
    verify_release_publication(case.publication, trust, REGISTRY)
    assert "Signed paired release delivery" in case.notes["markdown"]
    assert case.publication["signer_id"] == case.manifest["signing"]["signer_id"]
    changed = json.loads(json.dumps(case.publication))
    changed["components"]["drone"]["compressed_bytes"] += 1
    with pytest.raises(ContractError, match="identity"):
        verify_release_publication(changed, trust, REGISTRY)


def test_qualification_failure_record_marks_version_unusable(tmp_path: Path) -> None:
    attempt = create_qualification_attempt(
        version="v9.9.9",
        source_commit="a" * 40,
        recorded_at="2026-08-26T12:00:00Z",
        failure_stage="arm64-tests",
        findings=({"id": "ARM64_TEST_FAILED", "detail": "one target test failed"},),
        log_sha256="b" * 64,
        registry=REGISTRY,
    )
    verify_qualification_attempt(attempt, REGISTRY)
    case = _published(tmp_path / "published")
    source = FakeSource(case)
    source.versions = []
    source.attempts["v9.9.9"] = attempt
    with pytest.raises(ContractError, match="unusable failed"):
        fetch_release(
            source,
            "v9.9.9",
            tmp_path / "cache",
            bundle_trust=case.bundle_store,
            status_trust=case.status_store,
            registry=REGISTRY,
            host_limits=LIMITS,
            fetched_at="2026-08-26T12:05:00Z",
        )


def test_immutable_publication_rerun_is_noop_or_rejected() -> None:
    assets = {"a": "1" * 64, "b": "2" * 64}
    assert compare_immutable_assets(assets, None) == "create"
    assert compare_immutable_assets(assets, dict(assets)) == "no-op"
    with pytest.raises(ContractError, match="different immutable assets"):
        compare_immutable_assets(assets, {"a": "3" * 64})


def test_fetch_verifies_caches_and_supports_offline_extract(tmp_path: Path) -> None:
    case = _published(tmp_path / "published")
    source = FakeSource(case)
    cached = fetch_release(
        source,
        case.version,
        tmp_path / "cache",
        bundle_trust=case.bundle_store,
        status_trust=case.status_store,
        registry=REGISTRY,
        host_limits=LIMITS,
        fetched_at="2026-08-26T12:05:00Z",
    )
    assert cached.status["status"] == "qualified"
    offline = load_cached_release(
        cached.root,
        bundle_trust=case.bundle_store,
        status_trust=case.status_store,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    destination = tmp_path / "offline-installed"
    extract_bundle(
        offline.root / "drone",
        destination,
        case.bundle_store,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    assert (destination / "payload" / "share" / "payload.txt").read_text() == "drone\n"


def test_tampered_download_and_online_withdrawal_fail_closed(tmp_path: Path) -> None:
    case = _published(tmp_path / "published")
    source = FakeSource(case)
    archive_name = component_asset_name(case.manifest["release_id"], "drone", "bundle.tar.zst")
    source.assets[archive_name] = source.assets[archive_name] + b"tamper"
    with pytest.raises(ContractError):
        fetch_release(
            source,
            case.version,
            tmp_path / "tampered-cache",
            bundle_trust=case.bundle_store,
            status_trust=case.status_store,
            registry=REGISTRY,
            host_limits=LIMITS,
            fetched_at="2026-08-26T12:05:00Z",
        )
    source = FakeSource(case)
    trust = load_trusted_signers(case.status_store, REGISTRY)
    initial = case.status_index["statements"][0]
    withdrawn = create_status_statement(
        operation_id="withdraw-release-cache-test",
        release_id=case.manifest["release_id"],
        version=case.version,
        status="withdrawn",
        reason="Withdrawn after qualification",
        superseding_version=None,
        recorded_at="2026-08-26T12:06:00Z",
        private_key_path=case.status_key,
        registry=REGISTRY,
        previous_global=initial,
        previous_release=initial,
    )
    index = create_status_index(
        [initial, withdrawn],
        generated_at="2026-08-26T12:06:00Z",
        private_key_path=case.status_key,
        trusted_signers=trust,
        registry=REGISTRY,
    )
    source.index = canonical_json(index) + b"\n"
    with pytest.raises(ContractError, match="withdrawn"):
        fetch_release(
            source,
            case.version,
            tmp_path / "withdrawn-cache",
            bundle_trust=case.bundle_store,
            status_trust=case.status_store,
            registry=REGISTRY,
            host_limits=LIMITS,
            fetched_at="2026-08-26T12:07:00Z",
        )


def test_cached_unsafe_status_cannot_be_downgraded_by_stale_signed_index(
    tmp_path: Path,
) -> None:
    case = _published(tmp_path / "published")
    source = FakeSource(case)
    cached = fetch_release(
        source,
        case.version,
        tmp_path / "cache",
        bundle_trust=case.bundle_store,
        status_trust=case.status_store,
        registry=REGISTRY,
        host_limits=LIMITS,
        fetched_at="2026-08-26T12:05:00Z",
    )
    qualified_index = canonical_json(case.status_index) + b"\n"
    trust = load_trusted_signers(case.status_store, REGISTRY)
    initial = case.status_index["statements"][0]
    unsafe = create_status_statement(
        operation_id="unsafe-release-cache-test",
        release_id=case.manifest["release_id"],
        version=case.version,
        status="unsafe",
        reason="Critical fleet safety defect",
        superseding_version=None,
        recorded_at="2026-08-26T12:06:00Z",
        private_key_path=case.status_key,
        registry=REGISTRY,
        previous_global=initial,
        previous_release=initial,
    )
    unsafe_index = create_status_index(
        [initial, unsafe],
        generated_at="2026-08-26T12:06:00Z",
        private_key_path=case.status_key,
        trusted_signers=trust,
        registry=REGISTRY,
    )
    refresh_cached_status(
        cached.root,
        canonical_json(unsafe_index) + b"\n",
        status_trust=trust,
        registry=REGISTRY,
    )
    with pytest.raises(ContractError, match="stale release-status index"):
        refresh_cached_status(
            cached.root,
            qualified_index,
            status_trust=trust,
            registry=REGISTRY,
        )
    learned = load_cached_release(
        cached.root,
        bundle_trust=case.bundle_store,
        status_trust=case.status_store,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    assert learned.status["status"] == "unsafe"
    with pytest.raises(ContractError, match="unsafe"):
        materialize_cached_release(
            learned,
            tmp_path / "must-not-materialize",
            bundle_trust=case.bundle_store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )


def test_github_adapter_has_no_mutating_surface() -> None:
    source = GitHubReleaseSource("DIII-SDU-Group/III-Drone-ros2-ws")
    assert not any(name.startswith(("publish", "delete", "upload", "merge")) for name in dir(source))
