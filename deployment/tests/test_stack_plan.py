from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from iii_deployment.automation import load_automation_contract
from iii_deployment.contracts import ContractRegistry
from iii_deployment.governance import load_json
from iii_deployment.stack_plan import build_stack_plan

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
POLICY = load_json(
    ROOT / "deployment/governance/branch-policy.json", "iii.branch-policy/v1"
)
CONTRACT = load_automation_contract(ROOT / "deployment/automation-contract.json")


def _repository(path: Path, name: str) -> Path:
    remote = path / f"{name}.git"
    work = path / name
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "develop", str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    (work / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "base"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "develop"], check=True
    )
    subprocess.run(
        ["git", "-C", str(work), "switch", "-q", "-c", "deployment-redesign"],
        check=True,
    )
    (work / "feature").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "feature"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "feature"], check=True)
    (work / "deps").mkdir()
    (work / "deps/submodule-lock.txt").write_text("# lock\n", encoding="utf-8")
    return work


def _github_origin(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_stack_plan_binds_remote_and_local_heads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _repository(tmp_path, "III-Drone-ros2-ws")
    _github_origin(monkeypatch)
    plan = build_stack_plan(
        root=workspace,
        targets=(),
        base="develop",
        feature="deployment-redesign",
        operation_id="stack-deployment-redesign",
        created_at="2026-08-27T00:00:00+00:00",
        policy=POLICY,
        contract=CONTRACT,
        registry=REGISTRY,
    )
    assert plan["schema"] == "iii.automation-plan/v1"
    assert {row["kind"] for row in plan["mutations"]} == {"push", "pr-upsert"}
    assert plan["repositories"][0]["expected_old_sha"] is None
    assert len(plan["repositories"][0]["new_sha"]) == 40


def test_stack_plan_rejects_wrong_checked_out_feature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _repository(tmp_path, "III-Drone-ros2-ws")
    subprocess.run(["git", "-C", str(workspace), "switch", "develop"], check=True)
    _github_origin(monkeypatch)
    with pytest.raises(Exception, match="expected 'deployment-redesign'"):
        build_stack_plan(
            root=workspace,
            targets=(),
            base="develop",
            feature="deployment-redesign",
            operation_id="stack-deployment-redesign",
            created_at="2026-08-27T00:00:00+00:00",
            policy=POLICY,
            contract=CONTRACT,
            registry=REGISTRY,
        )
