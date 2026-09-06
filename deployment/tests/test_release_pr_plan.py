from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from iii_deployment.automation import load_automation_contract
from iii_deployment.contracts import ContractRegistry
from iii_deployment.governance import load_json
from iii_deployment.release_pr_plan import build_release_pr_plan


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
POLICY = load_json(
    ROOT / "deployment/governance/branch-policy.json", "iii.branch-policy/v1"
)
CONTRACT = load_automation_contract(ROOT / "deployment/automation-contract.json")


def _repository(path: Path) -> Path:
    remote = path / "workspace.git"
    work = path / "workspace"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    (work / "README.md").write_text("# release\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "base"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "release"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "release"], check=True
    )
    return work


def test_release_pr_plan_binds_protected_remote_refs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _repository(tmp_path)
    original = subprocess.run

    def run(command, **kwargs):
        values = list(command)
        if values[-3:-1] == ["remote", "get-url"]:
            return subprocess.CompletedProcess(
                values,
                0,
                stdout="https://github.com/test-owner/III-Drone-ros2-ws.git\n",
                stderr="",
            )
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    plan = build_release_pr_plan(
        root=workspace,
        operation_id="promote-main-to-release",
        created_at="2026-08-27T00:00:00+00:00",
        policy=POLICY,
        contract=CONTRACT,
        registry=REGISTRY,
    )
    assert plan["operation"] == "main-to-release"
    assert [mutation["kind"] for mutation in plan["mutations"]] == ["pr-upsert"]
    mutation = plan["mutations"][0]
    assert mutation["parameters"]["base"] == "release"
    assert mutation["parameters"]["head"] == "main"
    assert mutation["parameters"]["base_sha"] == mutation["parameters"]["head_sha"]
