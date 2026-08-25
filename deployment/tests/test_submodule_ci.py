from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = [
    "src/III-Drone-Configuration", "src/III-Drone-Contracts", "src/III-Drone-Core",
    "src/III-Drone-GC", "src/III-Drone-Interfaces", "src/III-Drone-Mission",
    "src/III-Drone-Runtime", "src/III-Drone-Simulation", "src/III-Drone-Supervision",
    "tools/III-Drone-CLI",
]


def test_trusted_baseline_check_passes_all_editable_repositories() -> None:
    script = ROOT / "scripts/ci/check_editable_iii_repository.py"
    for repository in REPOSITORIES:
        process = subprocess.run(
            [sys.executable, str(script), "--repository-root", str(ROOT / repository)],
            capture_output=True, text=True, check=False,
        )
        assert process.returncode == 0, f"{repository}: {process.stdout}\n{process.stderr}"


def test_submodule_workflows_and_rulesets_match_declared_policy() -> None:
    policy = json.loads((ROOT / "deployment/governance/branch-policy.json").read_text(encoding="utf-8"))
    for repository in REPOSITORIES:
        workflow_path = ROOT / repository / ".github/workflows/iii-governance.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        assert set(workflow[True]["pull_request"]["branches"]) == {"develop", "main"}
        assert set(workflow["jobs"]) == {"promotion-source", "iii-package-check"}
    for branch in ("develop", "main"):
        ruleset = json.loads(
            (ROOT / f"deployment/governance/rulesets/submodule-{branch}.json").read_text(encoding="utf-8")
        )
        checks = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
        contexts = [row["context"] for row in checks["parameters"]["required_status_checks"]]
        assert contexts == policy["submodule"][branch]["required_checks"]


def test_workspace_rulesets_match_declared_policy() -> None:
    policy = json.loads((ROOT / "deployment/governance/branch-policy.json").read_text(encoding="utf-8"))
    for branch in ("develop", "main", "release"):
        ruleset = json.loads(
            (ROOT / f"deployment/governance/rulesets/workspace-{branch}.json").read_text(encoding="utf-8")
        )
        checks = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
        contexts = [row["context"] for row in checks["parameters"]["required_status_checks"]]
        assert contexts == policy["workspace"][branch]["required_checks"]
