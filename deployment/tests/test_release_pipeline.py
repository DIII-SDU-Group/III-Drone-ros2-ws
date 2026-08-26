from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json, content_identity
from iii_deployment.release_pipeline import (
    assemble_qualification_evidence,
    assemble_release_manifest,
    assemble_signed_release,
    create_qualification_check,
)
from iii_deployment.signers import generate_signer


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
VERSION = "v1.2.3"
COMMIT = "1" * 40


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _canonical(path: Path, value: dict) -> Path:
    return _write(path, canonical_json(value) + b"\n")


def _tree_identity(root: Path) -> str:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
    return content_identity(entries)


def _snapshot(path: Path) -> Path:
    repository = {
        "path": ".", "commit": COMMIT, "state": "clean", "content_identity": "2" * 64,
        "tracked_patch_sha256": None, "entries": [], "untracked": [],
    }
    value = {
        "schema": "iii.source-snapshot/v1",
        "content_identity": "0" * 64,
        "policy_sha256": "3" * 64,
        "workspace_commit": COMMIT,
        "branch": "DETACHED",
        "clean": True,
        "dependency_lock_sha256": "4" * 64,
        "repositories": [repository],
        "excluded": [],
        "changed_paths": [],
        "impact": {"components": [], "causes": {}},
    }
    value["content_identity"] = content_identity({
        "policy_sha256": value["policy_sha256"],
        "dependency_lock_sha256": value["dependency_lock_sha256"],
        "repositories": [{"path": ".", "content_identity": repository["content_identity"]}],
    })
    return _canonical(path, value)


def _checks(tmp_path: Path, lock: Path) -> tuple[dict[str, Path], Path]:
    check_paths = {}
    for check_id in (
        "arm64-build", "arm64-tests", "dependency-lock", "deployment-contracts",
        "gc-build", "gc-tests", "governance-audit", "promotion-evidence",
    ):
        log = _write(tmp_path / f"{check_id}.log", f"PASS {check_id}\n".encode())
        output = _write(tmp_path / f"{check_id}.output", b"evidence\n")
        record = create_qualification_check(
            check_id=check_id,
            source_commit=COMMIT,
            version=VERSION,
            started_at="2026-08-26T12:00:00Z",
            finished_at="2026-08-26T12:01:00Z",
            command=("iii-test", check_id),
            log_path=log,
            outputs={output.name: output},
            registry=REGISTRY,
        )
        check_paths[check_id] = _canonical(tmp_path / f"{check_id}.check.json", record)
    evidence = assemble_qualification_evidence(
        version=VERSION,
        source_commit=COMMIT,
        dependency_lock_path=lock,
        check_paths=check_paths,
        registry=REGISTRY,
    )
    return check_paths, _canonical(tmp_path / "qualification-evidence.json", evidence)


def _metadata(root: Path) -> Path:
    for name in ("configuration", "px4-real", "px4-sim", "px4-interface", "qgc", "mission"):
        _write(root / "inputs" / name, (name + "\n").encode())
    value = json.loads((ROOT / "deployment/release-metadata.json").read_text())
    value["input_paths"] = {
        "configuration": ["inputs/configuration"],
        "px4_real": ["inputs/px4-real"],
        "px4_sim": ["inputs/px4-sim"],
        "px4_interface": ["inputs/px4-interface"],
        "qgc_managed_settings": ["inputs/qgc"],
    }
    return _canonical(root / "release-metadata.json", value)


def _mission_catalog(drone: Path) -> dict:
    directory = drone / "install/iii_drone_mission/share/iii_drone_mission/mission_catalog"
    assets = directory / "assets/sha256"
    assets.mkdir(parents=True)
    asset_raw = b"executor_owned_mode: inspection_demo\nentries: []\n"
    asset_hash = "sha256:" + hashlib.sha256(asset_raw).hexdigest()
    (assets / asset_hash.removeprefix("sha256:")).write_bytes(asset_raw)
    models_raw = b"<root/>\n"
    state = {
        "schema": "iii.mission-source-state/v1",
        "state_hash": "",
        "source_files": {"mission_specification/mission_specification.yaml": asset_hash},
        "interface_contracts": {"MissionModeStatus.msg": "sha256:" + "1" * 64},
        "registration_manifest_sha256": "sha256:" + "2" * 64,
        "behavior_node_contract_sha256": "sha256:" + "3" * 64,
        "models_xml_sha256": "sha256:" + hashlib.sha256(models_raw).hexdigest(),
    }
    state["state_hash"] = "sha256:" + content_identity({k: v for k, v in state.items() if k != "state_hash"})
    entry = {
        "schema": "iii.mission-catalog-entry/v1",
        "id": "inspection-production",
        "entry_hash": "",
        "classification": "production",
        "status": "active",
        "profiles": ["hil", "opti_track", "real"],
        "default_for": ["opti_track", "real"],
        "experimental_warning": None,
        "compatibility": {"source_state_sha256": state["state_hash"]},
        "specification": {"executor_owned_mode": "inspection_demo", "entries": [], "intent_services": []},
        "assets": [{"kind": "mission_specification", "logical_name": "mission_specification/mission_specification.yaml", "content_hash": asset_hash, "asset_id": asset_hash}],
        "dependencies": [asset_hash],
    }
    entry["entry_hash"] = "sha256:" + content_identity({k: v for k, v in entry.items() if k != "entry_hash"})
    catalog = {
        "schema": "iii.mission-catalog/v1",
        "catalog_hash": "",
        "scope": "qualified",
        "compatibility": {"source_state_sha256": state["state_hash"]},
        "profiles": {
            "hil": {"commissioned": False, "onboard": True, "default_entry_id": None},
            "opti_track": {"commissioned": True, "onboard": True, "default_entry_id": "inspection-production"},
            "real": {"commissioned": True, "onboard": True, "default_entry_id": "inspection-production"},
        },
        "entries": [entry],
        "assets": entry["assets"],
    }
    catalog["catalog_hash"] = "sha256:" + content_identity({k: v for k, v in catalog.items() if k != "catalog_hash"})
    catalog_raw = canonical_json(catalog) + b"\n"
    (directory / "catalog.json").write_bytes(catalog_raw)
    (directory / "catalog.sha256").write_text(hashlib.sha256(catalog_raw).hexdigest() + "  catalog.json\n")
    (directory / "source-state.json").write_bytes(canonical_json(state) + b"\n")
    (directory / "models.xml").write_bytes(models_raw)
    project = {
        "schema": "iii.groot2-project/v1",
        "catalog": "catalog.json",
        "node_model": "models.xml",
        "catalog_hash": catalog["catalog_hash"],
    }
    (directory / "groot2-project.json").write_bytes(canonical_json(project) + b"\n")
    return catalog


def _build_records(
    root: Path,
    snapshot: dict,
    drone: Path,
    gc: Path,
    checks: dict[str, Path],
) -> dict[str, Path]:
    build_policy = json.loads((ROOT / "deployment/build-policy.json").read_text())
    _canonical(root / "deployment/build-policy.json", build_policy)
    target = json.loads(
        (ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json").read_text()
    )
    drone_body = {
        "schema": "iii.build-record/v1",
        "source_identity": snapshot["content_identity"],
        "target_definition_id": target["definition_id"],
        "policy_sha256": content_identity(build_policy),
        "components": ["drone"],
        "requested_packages": ["iii_drone_core"],
        "impacted_packages": [],
        "packages": ["iii_drone_core"],
        "cache_keys": {"iii_drone_core": "7" * 64},
        "cache": {
            "context": "8" * 64,
            "reset": False,
            "hits": [],
            "misses": ["iii_drone_core"],
            "state_sha256": "9" * 64,
            "compiler": {
                "direct_hits": 0,
                "preprocessed_hits": 0,
                "misses": 1,
                "files": 1,
                "size_kibibytes": 1,
            },
        },
        "install_sha256": _tree_identity(drone / "install"),
        "assets": {"sha256": "a" * 64, "files": 1},
        "python": {"abi": "cp312", "lock_sha256": "b" * 64, "imports_verified": True},
        "elf": {"scanned": 0, "closure_verified": True},
        "complete": True,
    }
    drone_record = {"build_id": content_identity(drone_body), **drone_body}
    gc_inputs = (
        "src/III-Drone-GC/docker/proxy.Dockerfile",
        "src/III-Drone-GC/docker/proxy-requirements.lock",
        "src/III-Drone-GC/frontend/Dockerfile",
        "src/III-Drone-GC/frontend/package-lock.json",
    )
    input_hashes = {}
    for relative in gc_inputs:
        path = _write(root / relative, (relative + "\n").encode())
        input_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    images = []
    for name in ("frontend", "proxy"):
        archive = _write(gc / "images" / f"{name}.oci", f"{name}\n".encode())
        images.append({
            "name": name,
            "archive": archive.name,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "bytes": archive.stat().st_size,
            "manifest_digest": "sha256:" + ("c" if name == "frontend" else "d") * 64,
            "base_images": [f"example.invalid/{name}@sha256:" + "e" * 64],
            "smoke_test": "passed",
        })
    gc_body = {
        "schema": "iii.gc-build-record/v1",
        "source_identity": snapshot["content_identity"],
        "source_commit": COMMIT,
        "version": VERSION,
        "platform": {"os": "linux", "architecture": "amd64"},
        "inputs_sha256": content_identity(input_hashes),
        "test_record_sha256": hashlib.sha256(checks["gc-tests"].read_bytes()).hexdigest(),
        "images": images,
        "complete": True,
    }
    gc_record = {"build_id": content_identity(gc_body), **gc_body}
    return {
        "drone": _canonical(root / "drone-build.json", drone_record),
        "gc": _canonical(root / "gc-build.json", gc_record),
    }


def _promotion(key: Ed25519PrivateKey, source_identity: str, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signer_id = hashlib.sha256(public).hexdigest()
    impact = json.loads((ROOT / "deployment/governance/change-impact-policy.json").read_text())
    value = {
        "schema_version": "1", "attestation_id": "0" * 64,
        "workspace_commit": COMMIT, "source_content_identity": source_identity,
        "dependency_lock_sha256": "4" * 64, "policy_sha256": content_identity(impact),
        "categories": [{"id": "static-unit", "status": "passed", "summary_sha256": "5" * 64}],
        "waivers": [], "artifacts": [], "signer_id": signer_id,
    }
    value["attestation_id"] = content_identity({k: v for k, v in value.items() if k != "attestation_id"})
    value["signature"] = base64.urlsafe_b64encode(key.sign(canonical_json(value))).decode().rstrip("=")
    return _canonical(tmp_path / "promotion-attestation.json", value), {
        signer_id: base64.urlsafe_b64encode(public).decode().rstrip("=")
    }


@pytest.fixture
def pipeline_case(tmp_path: Path) -> dict:
    lock = _write(tmp_path / "deps/submodule-lock.txt", b"# lock\n")
    check_paths, evidence = _checks(tmp_path / "checks", lock)
    snapshot_path = _snapshot(tmp_path / "source-snapshot.json")
    snapshot = json.loads(snapshot_path.read_text())
    provenance = _write(tmp_path / "source-provenance.md", b"# provenance\n")
    metadata = _metadata(tmp_path)
    drone = _write(tmp_path / "payloads/drone/install/core", b"drone\n").parent.parent
    expected_catalog = _mission_catalog(drone)
    gc = tmp_path / "payloads/gc"
    records = _build_records(tmp_path, snapshot, drone, gc, check_paths)
    key_path = tmp_path / "qualified.pem"
    public_path = tmp_path / "qualified.public.json"
    generate_signer(key_path, public_path, authority="ci-qualified", registry=REGISTRY)
    manifest = assemble_release_manifest(
        root=tmp_path,
        version=VERSION,
        source_snapshot_path=snapshot_path,
        provenance_path=provenance,
        qualification_evidence_path=evidence,
        metadata_path=metadata,
        target_definition_path=ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json",
        operational_policy_path=ROOT / "deployment/operational-policy.json",
        component_roots={"drone": drone, "gc": gc},
        build_records=records,
        private_key_path=key_path,
        builder_id="github-actions-qualified",
        built_at="2026-08-26T12:00:00Z",
        source_date_epoch=1787745600,
        source_content_identity="6" * 64,
        registry=REGISTRY,
    )
    return {
        "root": tmp_path, "lock": lock, "checks": check_paths, "evidence": evidence,
        "snapshot": snapshot_path, "snapshot_value": snapshot, "provenance": provenance,
        "metadata": metadata, "drone": drone, "gc": gc, "records": records,
        "key": key_path, "manifest": manifest, "catalog": expected_catalog,
    }


def test_check_records_are_source_bound_and_evidence_requires_complete_set(tmp_path: Path) -> None:
    lock = _write(tmp_path / "lock", b"lock\n")
    checks, evidence = _checks(tmp_path, lock)
    value = json.loads(evidence.read_text())
    assert {item["id"] for item in value["required_checks"]} >= {
        "arm64-build", "arm64-tests", "promotion-evidence"
    }
    with pytest.raises(ContractError, match="incomplete"):
        assemble_qualification_evidence(
            version=VERSION, source_commit=COMMIT, dependency_lock_path=lock,
            check_paths={key: path for key, path in checks.items() if key != "arm64-tests"},
            registry=REGISTRY,
        )
    tampered = json.loads(checks["arm64-build"].read_text())
    tampered["source_commit"] = "9" * 40
    _canonical(checks["arm64-build"], tampered)
    with pytest.raises(ContractError, match="another source"):
        assemble_qualification_evidence(
            version=VERSION, source_commit=COMMIT, dependency_lock_path=lock,
            check_paths=checks, registry=REGISTRY,
        )


def test_manifest_is_derived_from_pinned_payload_policy_and_signer(pipeline_case: dict) -> None:
    manifest = pipeline_case["manifest"]
    REGISTRY.validate("release-manifest", manifest)
    assert manifest["release_class"] == "qualified"
    assert manifest["components"] == ["drone", "gc"]
    assert manifest["source"]["branch"] == "release"
    assert manifest["source"]["snapshot_content_identity"] == pipeline_case["snapshot_value"]["content_identity"]
    assert manifest["source"]["content_identity"] != manifest["source"]["snapshot_content_identity"]
    assert manifest["packages"][0]["content_sha256"] != manifest["packages"][1]["content_sha256"]
    assert manifest["mission_catalog"]["catalog_hash"] == pipeline_case["catalog"]["catalog_hash"]
    assert manifest["mission_catalog"]["catalog_sha256"] == hashlib.sha256(
        canonical_json(pipeline_case["catalog"]) + b"\n"
    ).hexdigest()
    assert manifest["mission_catalog"]["scope"] == "qualified"
    assert manifest["release_id"] == content_identity({k: v for k, v in manifest.items() if k != "release_id"})


def test_manifest_refuses_dirty_or_changed_candidate(pipeline_case: dict) -> None:
    snapshot = json.loads(pipeline_case["snapshot"].read_text())
    snapshot["clean"] = False
    snapshot["repositories"][0]["state"] = "modified"
    _canonical(pipeline_case["snapshot"], snapshot)
    with pytest.raises(ContractError, match="not clean"):
        assemble_release_manifest(
            root=pipeline_case["root"], version=VERSION,
            source_snapshot_path=pipeline_case["snapshot"], provenance_path=pipeline_case["provenance"],
            qualification_evidence_path=pipeline_case["evidence"], metadata_path=pipeline_case["metadata"],
            target_definition_path=ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json",
            operational_policy_path=ROOT / "deployment/operational-policy.json",
            component_roots={"drone": pipeline_case["drone"], "gc": pipeline_case["gc"]},
            build_records=pipeline_case["records"], private_key_path=pipeline_case["key"],
            builder_id="github-actions-qualified", built_at="2026-08-26T12:00:00Z",
            source_date_epoch=1787745600, registry=REGISTRY,
            source_content_identity="6" * 64,
        )


def test_manifest_refuses_tampered_build_record_or_payload(pipeline_case: dict) -> None:
    gc_record = json.loads(pipeline_case["records"]["gc"].read_text())
    gc_record["source_commit"] = "9" * 40
    _canonical(pipeline_case["records"]["gc"], gc_record)
    with pytest.raises(ContractError, match="build-record identity mismatch"):
        assemble_release_manifest(
            root=pipeline_case["root"], version=VERSION,
            source_snapshot_path=pipeline_case["snapshot"], provenance_path=pipeline_case["provenance"],
            qualification_evidence_path=pipeline_case["evidence"], metadata_path=pipeline_case["metadata"],
            target_definition_path=ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json",
            operational_policy_path=ROOT / "deployment/operational-policy.json",
            component_roots={"drone": pipeline_case["drone"], "gc": pipeline_case["gc"]},
            build_records=pipeline_case["records"], private_key_path=pipeline_case["key"],
            builder_id="github-actions-qualified", built_at="2026-08-26T12:00:00Z",
            source_date_epoch=1787745600, registry=REGISTRY,
            source_content_identity="6" * 64,
        )


def test_full_signed_release_record_binds_every_published_asset(pipeline_case: dict) -> None:
    promotion_path, promotion_trust = _promotion(
        Ed25519PrivateKey.generate(), pipeline_case["manifest"]["source"]["content_identity"],
        pipeline_case["root"],
    )
    impact = json.loads((ROOT / "deployment/governance/change-impact-policy.json").read_text())
    metadata = json.loads(pipeline_case["metadata"].read_text())
    change_summary = {
        "schema_version": "1", "summary_type": "iii.release-change-summary", "version": VERSION,
        "base_version": "v1.2.2", "base_commit": "0" * 40, "source_commit": COMMIT,
        "commits": [], "changed_paths": ["deployment/release_pipeline.py"],
        "categories": {
            "drone": [], "gc": [], "missions": [], "configuration": [], "px4": [], "qgc": [],
            "host_provisioning": ["deployment/release_pipeline.py"], "documentation": [],
        },
        "operator_changes": ["Qualified release pipeline changed"],
    }
    output = pipeline_case["root"] / "signed"
    assets = assemble_signed_release(
        root=ROOT,
        manifest=pipeline_case["manifest"],
        manifest_path=pipeline_case["root"] / "release-manifest.json",
        component_roots={"drone": pipeline_case["drone"], "gc": pipeline_case["gc"]},
        build_records=pipeline_case["records"], check_paths={path.name: path for path in pipeline_case["checks"].values()},
        qualification_evidence_path=pipeline_case["evidence"],
        promotion_attestation_path=promotion_path,
        promotion_trusted_signers=promotion_trust,
        impact_policy=impact, metadata=metadata, change_summary=change_summary,
        private_key_path=pipeline_case["key"], output=output,
        repository="DIII-SDU-Group/III-Drone-ros2-ws", run_id="12345", run_attempt=1,
        created_at="2026-08-26T12:00:00Z", registry=REGISTRY,
    )
    publication = json.loads(assets["release-publication.json"].read_text())
    record = json.loads(assets["release-record.json"].read_text())
    assert publication["release_record_sha256"] == hashlib.sha256(assets["release-record.json"].read_bytes()).hexdigest()
    assert {item["name"] for item in record["checks"]} == {path.name for path in pipeline_case["checks"].values()}
    assert len([name for name in assets if name.endswith("bundle.tar.zst")]) == 2


def test_signing_failure_leaves_no_partial_release(pipeline_case: dict) -> None:
    invalid_key = pipeline_case["root"] / "invalid.pem"
    invalid_key.write_text("not a key\n")
    invalid_key.chmod(0o600)
    output = pipeline_case["root"] / "failed-signing"
    with pytest.raises(ContractError, match="private signer key"):
        assemble_signed_release(
            root=ROOT, manifest=pipeline_case["manifest"],
            manifest_path=pipeline_case["root"] / "failed-manifest.json",
            component_roots={"drone": pipeline_case["drone"], "gc": pipeline_case["gc"]},
            build_records=pipeline_case["records"], check_paths={},
            qualification_evidence_path=pipeline_case["evidence"],
            promotion_attestation_path=pipeline_case["evidence"], promotion_trusted_signers={},
            impact_policy={}, metadata=json.loads(pipeline_case["metadata"].read_text()),
            change_summary={}, private_key_path=invalid_key, output=output,
            repository="DIII-SDU-Group/III-Drone-ros2-ws", run_id="1", run_attempt=1,
            created_at="2026-08-26T12:00:00Z", registry=REGISTRY,
        )
    assert not output.exists()
