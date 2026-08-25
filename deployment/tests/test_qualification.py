from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry, classify_release
from iii_deployment.qualification import inspect_qualification, select_recovery_anchor


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
MANIFEST = json.loads((ROOT / "deployment" / "tests" / "fixtures" / "release_manifest.json").read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return process.stdout.strip()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _evidence(path: Path, repo: Path, version: str = "v1.2.3", **updates) -> Path:
    lock_hash = hashlib.sha256((repo / "deps/submodule-lock.txt").read_bytes()).hexdigest()
    value = {
        "schema_version": "1",
        "schema": "iii.qualification-evidence/v1",
        "evidence_type": "qualified-release-preflight",
        "source_commit": _git(repo, "rev-parse", "HEAD"),
        "version": version,
        "dependency_lock_sha256": lock_hash,
        "governance_verified": True,
        "required_checks": [
            {"id": check_id, "status": "passed", "evidence_sha256": "a" * 64}
            for check_id in (
                "arm64-build",
                "arm64-tests",
                "dependency-lock",
                "deployment-contracts",
                "governance-audit",
                "promotion-evidence",
            )
        ],
        "evidence_complete": True,
    }
    value.update(updates)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def release_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=release")
    _git(repo, "config", "user.name", "III Test")
    _git(repo, "config", "user.email", "iii-test@example.invalid")
    _write(repo / "deps/submodule-lock.txt", "# test lock\n")
    _write(repo / "payload.txt", "release\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release")
    _git(repo, "update-ref", "refs/remotes/origin/release", "HEAD")
    evidence = _evidence(tmp_path / "evidence.json", repo)
    return repo, evidence


def _inspect(repo: Path, evidence: Path, *, mode: str = "build"):
    return inspect_qualification(
        repo,
        version="v1.2.3",
        evidence_path=evidence,
        mode=mode,
        lock_command=("/bin/true",),
        registry=REGISTRY,
    )


def test_valid_release_tag_and_publish_preflight(release_repo: tuple[Path, Path]) -> None:
    repo, evidence = release_repo
    assert _inspect(repo, evidence, mode="publish").require_verified().verified
    _git(repo, "tag", "v1.2.3")
    assert _inspect(repo, evidence).require_verified().verified


def test_tag_outside_release_cannot_qualify(release_repo: tuple[Path, Path]) -> None:
    repo, evidence = release_repo
    _write(repo / "payload.txt", "feature\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "feature outside release")
    _git(repo, "tag", "v1.2.3")
    _evidence(evidence, repo)
    report = _inspect(repo, evidence)
    assert not report.verified
    assert next(check for check in report.checks if check.id == "release.reachable").passed is False
    manifest = deepcopy(MANIFEST)
    manifest["source"]["workspace_commit"] = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(ContractError, match="build preflight is not verified"):
        classify_release(manifest, requested="qualified", preflight=report.to_dict())


def test_dirty_source_fails_closed(release_repo: tuple[Path, Path]) -> None:
    repo, evidence = release_repo
    _git(repo, "tag", "v1.2.3")
    _write(repo / "untracked.txt", "dirty\n")
    with pytest.raises(ContractError, match="source.clean"):
        _inspect(repo, evidence).require_verified()


def test_moved_tag_fails_closed(release_repo: tuple[Path, Path]) -> None:
    repo, evidence = release_repo
    _git(repo, "tag", "v1.2.3")
    _write(repo / "payload.txt", "new release\n")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "new release head")
    _git(repo, "update-ref", "refs/remotes/origin/release", "HEAD")
    _evidence(evidence, repo)
    with pytest.raises(ContractError, match="tag.exact-commit"):
        _inspect(repo, evidence).require_verified()


def test_lock_divergence_and_incomplete_evidence_fail_closed(release_repo: tuple[Path, Path]) -> None:
    repo, evidence = release_repo
    _git(repo, "tag", "v1.2.3")
    _evidence(evidence, repo, dependency_lock_sha256="0" * 64)
    with pytest.raises(ContractError, match="evidence.lock-binding"):
        _inspect(repo, evidence).require_verified()
    _evidence(evidence, repo, evidence_complete=False)
    with pytest.raises(ContractError, match="evidence.contract"):
        _inspect(repo, evidence).require_verified()
    value = json.loads(_evidence(evidence, repo).read_text(encoding="utf-8"))
    value["required_checks"] = [
        check for check in value["required_checks"] if check["id"] != "promotion-evidence"
    ]
    evidence.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="evidence.required-check-set"):
        _inspect(repo, evidence).require_verified()


def test_qualified_manifest_cannot_be_claimed_by_field_release() -> None:
    manifest = deepcopy(MANIFEST)
    manifest["release_class"] = "field-development"
    with pytest.raises(ContractError, match="declare qualified"):
        classify_release(manifest, requested="qualified", preflight={})


def test_only_accepted_qualified_activation_replaces_recovery_anchor() -> None:
    current = "f" * 64
    preflight = {
        "schema": "iii.qualification-preflight-result/v1",
        "mode": "build",
        "version": MANIFEST["version"],
        "source_commit": MANIFEST["source"]["workspace_commit"],
        "release_commit": MANIFEST["source"]["workspace_commit"],
        "verified": True,
        "checks": [{"id": "test", "passed": True, "detail": "fixture"}],
    }
    accepted = {
        "action": "activate",
        "outcome": "accepted",
        "release_id": MANIFEST["release_id"],
        "activated_release_id": MANIFEST["release_id"],
        "qualification_preflight": preflight,
    }
    assert select_recovery_anchor(current, manifest=MANIFEST, deployment=accepted) == MANIFEST["release_id"]
    for key, value in (
        ("outcome", "failed"),
        ("action", "stage"),
        ("qualification_preflight", dict(preflight, verified=False)),
        ("activated_release_id", "e" * 64),
    ):
        result = dict(accepted, **{key: value})
        assert select_recovery_anchor(current, manifest=MANIFEST, deployment=result) == current
    field = deepcopy(MANIFEST)
    field["release_class"] = "field-development"
    assert select_recovery_anchor(current, manifest=field, deployment=accepted) == current
