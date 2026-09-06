from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.source import (
    analyze_component_impact,
    capture_source_snapshot,
    provenance_markdown,
    release_manifest_source,
    validate_component_selection,
    verify_source_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
BASE_POLICY = json.loads((ROOT / "deployment/source-policy.json").read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return process.stdout.strip()


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")
    _write(path / ".gitignore", "/build/\n/log/\n*.key\n")
    _write(path / "deps/submodule-lock.txt", "fixture 0000000000000000000000000000000000000000\n")
    _write(path / "src/app.py", "VALUE = 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "fixture")
    return path


def _policy(*repositories: str) -> dict:
    value = deepcopy(BASE_POLICY)
    value["governed_repositories"] = list(repositories or (".",))
    value["workspace_source_roots"] = ["src"]
    value["workspace_source_files"] = [".gitignore"]
    value["component_rules"] = [
        {"id": "DRONE", "patterns": ["src/*.py", "src/III-Drone-Core", "src/III-Drone-Core/**"], "components": ["drone"]},
        {"id": "SHARED", "patterns": ["src/shared/**"], "components": ["drone", "gc"]},
    ]
    value["non_artifact_patterns"] = ["docs/**", "deps/submodule-lock.txt", ".gitignore"]
    REGISTRY.validate("source-policy", value)
    return value


def test_identical_content_has_identical_identity(tmp_path: Path) -> None:
    first = capture_source_snapshot(_repo(tmp_path / "one"), _policy(), REGISTRY)
    second = capture_source_snapshot(_repo(tmp_path / "two"), _policy(), REGISTRY)
    assert first["content_identity"] == second["content_identity"]
    verify_source_snapshot(first, REGISTRY)


def test_tracked_dirty_deletion_and_untracked_source_change_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    clean = capture_source_snapshot(repo, _policy(), REGISTRY)
    _write(repo / "src/app.py", "VALUE = 2\n")
    modified = capture_source_snapshot(repo, _policy(), REGISTRY)
    assert modified["repositories"][0]["state"] == "modified"
    assert modified["content_identity"] != clean["content_identity"]
    (repo / "src/app.py").unlink()
    deleted = capture_source_snapshot(repo, _policy(), REGISTRY)
    assert any(entry["kind"] == "deleted" for entry in deleted["repositories"][0]["entries"])
    _git(repo, "checkout", "--", "src/app.py")
    _write(repo / "src/new.py", "NEW = True\n")
    untracked = capture_source_snapshot(repo, _policy(), REGISTRY)
    assert untracked["repositories"][0]["state"] == "untracked"
    assert untracked["content_identity"] != clean["content_identity"]


def test_secrets_generated_outputs_datasets_and_unrelated_files_are_excluded(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "src/token.secret", "never capture\n")
    _write(repo / "src/private.key", "ignored\n")
    _write(repo / "build/object.o", "ignored\n")
    _write(repo / "notes.txt", "unrelated\n")
    snapshot = capture_source_snapshot(repo, _policy(), REGISTRY)
    captured = {entry["path"] for entry in snapshot["repositories"][0]["untracked"]}
    assert "src/token.secret" not in captured
    assert "src/private.key" not in captured
    assert "build/object.o" not in captured
    assert {"path": "src/token.secret", "reason": "sensitive"} in snapshot["excluded"]
    assert {"path": "notes.txt", "reason": "unrelated"} in snapshot["excluded"]


def test_tracked_unrelated_workspace_change_does_not_dirty_deployment_snapshot(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "scripts/workspace/research.py", "EXPERIMENT = 1\n")
    _git(repo, "add", "scripts/workspace/research.py")
    _git(repo, "commit", "-qm", "research helper")
    clean = capture_source_snapshot(repo, _policy(), REGISTRY)
    _write(repo / "scripts/workspace/research.py", "EXPERIMENT = 2\n")
    snapshot = capture_source_snapshot(repo, _policy(), REGISTRY)
    assert snapshot["repositories"][0]["state"] == "clean"
    assert snapshot["content_identity"] == clean["content_identity"]
    assert snapshot["changed_paths"] == []


def test_python_egg_info_generated_by_a_local_build_is_excluded(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "src/iii_drone_core.egg-info/PKG-INFO", "generated metadata\n")
    snapshot = capture_source_snapshot(repo, _policy(), REGISTRY)
    assert "src/iii_drone_core.egg-info/PKG-INFO" not in {
        entry["path"] for entry in snapshot["repositories"][0]["untracked"]
    }
    assert {
        "path": "src/iii_drone_core.egg-info/PKG-INFO",
        "reason": "generated",
    } in snapshot["excluded"]


def test_source_directory_named_build_is_not_mistaken_for_generated_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "scripts/build/release.py", "RELEASE = True\n")
    policy = _policy()
    policy["workspace_source_roots"] = ["src", "scripts"]
    policy["component_rules"].append({
        "id": "INTEGRATION", "patterns": ["scripts/**"], "components": ["drone", "gc"],
    })
    snapshot = capture_source_snapshot(repo, policy, REGISTRY)
    assert "scripts/build/release.py" in {
        entry["path"] for entry in snapshot["repositories"][0]["untracked"]
    }
    assert snapshot["impact"]["components"] == ["drone", "gc"]


def test_governed_submodule_dirty_and_untracked_state_changes_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    submodule = repo / "src/III-Drone-Core"
    submodule.mkdir(parents=True)
    _git(submodule, "init", "-q")
    _git(submodule, "config", "user.email", "test@example.invalid")
    _git(submodule, "config", "user.name", "Test")
    _write(submodule / "core.py", "CORE = 1\n")
    _git(submodule, "add", ".")
    _git(submodule, "commit", "-qm", "core")
    _git(repo, "add", "src/III-Drone-Core")
    _git(repo, "commit", "-qm", "pin core")
    policy = _policy(".", "src/III-Drone-Core")
    clean = capture_source_snapshot(repo, policy, REGISTRY)
    _write(submodule / "core.py", "CORE = 2\n")
    dirty = capture_source_snapshot(repo, policy, REGISTRY)
    assert dirty["repositories"][1]["state"] == "modified"
    assert dirty["content_identity"] != clean["content_identity"]
    _git(submodule, "checkout", "--", "core.py")
    _write(submodule / "new.py", "NEW = 1\n")
    untracked = capture_source_snapshot(repo, policy, REGISTRY)
    assert untracked["repositories"][1]["state"] == "untracked"


def test_paired_impact_explains_causes_and_unsafe_omission_fails() -> None:
    impact = analyze_component_impact(["src/shared/interface.json"], _policy())
    assert impact["components"] == ["drone", "gc"]
    assert impact["causes"]["drone"] == ["SHARED: src/shared/interface.json"]
    validate_component_selection(impact, ["drone", "gc"])
    with pytest.raises(ContractError, match="unsafe manual component omission.*gc"):
        validate_component_selection(impact, ["drone"])
    with pytest.raises(ContractError, match="ambiguous component impact"):
        analyze_component_impact(["src/unknown.bin"], _policy())


def test_field_manifest_and_human_report_include_dirty_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "src/new.py", "NEW = True\n")
    snapshot = capture_source_snapshot(repo, _policy(), REGISTRY)
    manifest = release_manifest_source(snapshot)
    assert manifest["clean"] is False
    assert manifest["untracked"][0]["path"] == "src/new.py"
    assert len(manifest["snapshot_sha256"]) == 64
    assert len(manifest["provenance_report_sha256"]) == 64
    report = provenance_markdown(snapshot)
    assert snapshot["content_identity"] in report
    assert "`src/new.py`" in report
    assert "Required artifacts: `" in report


def test_unsafe_symlink_fails_instead_of_omitting_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / "src/escape.py").symlink_to("../../outside")
    with pytest.raises(ContractError, match="unsafe source symlink"):
        capture_source_snapshot(repo, _policy(), REGISTRY)


def test_canonical_policy_matches_governed_repository_inventory() -> None:
    documentation = json.loads((ROOT / "deployment/documentation-policy.json").read_text())
    expected = [repo["path"] for repo in documentation["repositories"] if repo["governed"]]
    assert set(expected).issubset(BASE_POLICY["governed_repositories"])


def test_canonical_policy_captures_every_release_asset_repository() -> None:
    build_policy = json.loads((ROOT / "deployment/build-policy.json").read_text())
    governed = set(BASE_POLICY["governed_repositories"])
    for asset in build_policy["release_assets"]:
        source = Path(asset["source"])
        assert any(
            source == Path(repository) or Path(repository) in source.parents
            for repository in governed
            if repository != "."
        ), asset["source"]
