from __future__ import annotations

import json
from pathlib import Path
import tomllib
import subprocess

import pytest

from iii_deployment import legacy_retirement
from iii_deployment.contracts import ContractRegistry


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "deployment/legacy-retirement-policy.json"


def test_distribution_packages_retirement_and_documentation_records() -> None:
    pyproject = tomllib.loads(
        (ROOT / "deployment/pyproject.toml").read_text(encoding="utf-8")
    )
    data_files = pyproject["tool"]["setuptools"]["data-files"]
    assert "legacy-retirement-policy.json" in data_files["share/iii-deployment/policy"]
    assert data_files["share/iii-deployment/documentation"] == [
        "documentation-manifest.json",
        "documentation-review.json",
    ]
    assert data_files["share/iii-deployment/retirement"] == [
        "legacy-archive-metadata.json"
    ]


def test_active_tree_has_no_legacy_deployment_entrypoint() -> None:
    policy = legacy_retirement.load_policy(POLICY)
    assert legacy_retirement.audit(ROOT, policy) == []


def test_retired_remote_bootstrap_is_helpful_and_never_mutates(tmp_path: Path) -> None:
    script = ROOT / "scripts/remote/install_remote.bash"
    environment = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [str(script)], capture_output=True, text=True, env=environment, check=False
    )
    assert result.returncode == 64
    assert "III_REMOTE_BOOTSTRAP_RETIRED" in result.stderr
    assert "Next: iii gc provision --help" in result.stderr
    assert list(tmp_path.iterdir()) == []

    help_result = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert help_result.returncode == 0
    assert "retired" in help_result.stdout


def test_audit_reports_reintroduced_pattern(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "setup/bad.bash"
    path.parent.mkdir(parents=True)
    path.write_text("export III_DRONE_DEPLOYMENT_BRANCH=staging\n", encoding="utf-8")
    policy = {
        "scan_roots": ["setup"],
        "excluded_patterns": [],
        "required_absent": [],
        "forbidden_active_patterns": [
            {
                "id": "legacy-repository-variable",
                "pattern": "III_DRONE_DEPLOYMENT_(?:BRANCH|URL|DIR_NAME)",
            }
        ],
        "retired_entrypoints": [],
    }
    monkeypatch.setattr(
        legacy_retirement, "_tracked_files", lambda _root, _roots: ["setup/bad.bash"]
    )
    monkeypatch.setattr(
        legacy_retirement, "validate_archive_metadata", lambda _root, _policy: []
    )
    assert legacy_retirement.audit(tmp_path, policy) == [
        "setup/bad.bash: retired pattern legacy-repository-variable"
    ]


def test_archive_metadata_cannot_claim_archive_before_q131() -> None:
    path = ROOT / "deployment/legacy-archive-metadata.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    ContractRegistry(ROOT / "deployment/schemas/v1").validate(
        "legacy-archive-metadata", value
    )
    assert value["state"] == "pending-q131"
    assert value["q131_retirement_evidence_id"] is None

    value["state"] = "archived"
    with pytest.raises(Exception):
        ContractRegistry(ROOT / "deployment/schemas/v1").validate(
            "legacy-archive-metadata", value
        )


def test_retired_cli_build_and_bare_config_paths_are_absent() -> None:
    from iii.__main__ import build_parser
    from iii.runner import inventory_parser

    leaves = set(inventory_parser(build_parser()))
    assert not any(path and path[0] == "build" for path in leaves)
    assert ("config",) not in leaves
    assert ("config", "capture", "pull") in leaves
    assert (ROOT / "tools/III-Drone-CLI/iii/container_manager.py").is_file()
    assert (ROOT / "src/III-Drone-GC/docker-compose.prod.yml").is_file()
