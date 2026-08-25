from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
import yaml

from iii_deployment.automation import (
    MutationAdapter,
    OperationStore,
    PermissionDenied,
    StalePlan,
    create_plan,
    execute_plan,
    load_automation_contract,
    plan_result,
)
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.result import Outcome


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
CONTRACT = load_automation_contract(ROOT / "deployment" / "automation-contract.json")


def _mutation(identifier: str, kind: str = "push") -> dict:
    return {
        "id": identifier,
        "kind": kind,
        "repository": "DIII-SDU-Group/III-Drone-Core",
        "ref": "refs/heads/feature-test",
        "expected_old_sha": "1" * 40,
        "new_sha": "2" * 40,
        "parameters": {"base": "develop", "trusted": True},
    }


def _plan(*mutations: dict, operation_id: str = "operation-123") -> dict:
    return create_plan(
        operation_id=operation_id,
        operation="stacked-pr",
        created_at="2026-08-26T00:00:00Z",
        policy={"branch": "develop"},
        trusted_inputs={"refs": {"develop": "1" * 40}},
        repositories=[
            {
                "repository": "DIII-SDU-Group/III-Drone-Core",
                "ref": "refs/heads/feature-test",
                "expected_old_sha": "1" * 40,
                "new_sha": "2" * 40,
            }
        ],
        checks=[{"id": "promotion-source-develop", "status": "passed", "evidence_sha256": "3" * 64}],
        permissions=[
            {"repository": "DIII-SDU-Group/III-Drone-Core", "permission": "contents:write"},
            {"repository": "DIII-SDU-Group/III-Drone-Core", "permission": "pull-requests:write"},
        ],
        mutations=mutations or (_mutation("push-core"),),
        contract=CONTRACT,
        registry=REGISTRY,
    )


class FakeAdapter(MutationAdapter):
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        interrupt_on: str | None = None,
        stale_on: str | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.interrupt_on = interrupt_on
        self.stale_on = stale_on
        self.preflighted: list[str] = []
        self.applied: list[str] = []

    def preflight(self, mutation) -> None:
        self.preflighted.append(mutation["id"])
        if mutation["id"] == self.fail_on:
            raise PermissionDenied("authenticated token lacks declared repository permission")
        if mutation["id"] == self.stale_on:
            raise StalePlan("authenticated current ref differs from expected old SHA")

    def apply(self, mutation) -> str:
        self.applied.append(mutation["id"])
        if mutation["id"] == self.interrupt_on:
            raise KeyboardInterrupt
        return "evidence:" + mutation["id"]


def test_plan_is_content_addressed_schema_valid_and_dry_run_is_machine_human_equivalent() -> None:
    plan = _plan()
    assert len(plan["plan_id"]) == 64
    result = plan_result(plan)
    assert result.outcome is Outcome.SUCCESS
    assert result.payload["plan"] == plan
    assert "--operation-id operation-123" in result.render_human()
    assert json.loads(result.render_json())["next_actions"][0]["mutating"] is True


def test_apply_persists_plan_and_is_idempotent(tmp_path: Path) -> None:
    plan = _plan()
    store = OperationStore(tmp_path / "operations", REGISTRY)
    adapter = FakeAdapter()
    result = execute_plan(plan, store=store, adapters={"push": adapter}, contract=CONTRACT)
    assert result.outcome is Outcome.SUCCESS
    assert store.load_plan("operation-123") == plan
    assert store.load("operation-123")["state"] == "completed"
    rerun = execute_plan(plan, store=store, adapters={"push": adapter}, contract=CONTRACT)
    assert rerun.code == "III_AUTOMATION_ALREADY_COMPLETE"
    assert adapter.applied == ["push-core"]
    assert store.path("operation-123").stat().st_mode & 0o777 == 0o600


def test_partial_creation_resumes_without_repeating_completed_mutation(tmp_path: Path) -> None:
    plan = _plan(_mutation("push-core"), _mutation("open-pr", "pr-upsert"))
    store = OperationStore(tmp_path / "operations", REGISTRY)
    first = FakeAdapter(fail_on="open-pr")
    result = execute_plan(
        plan,
        store=store,
        adapters={"push": first, "pr-upsert": first},
        contract=CONTRACT,
    )
    assert result.outcome is Outcome.PARTIAL
    assert store.load("operation-123")["completed_mutations"] == ["push-core"]
    retry = FakeAdapter()
    completed = execute_plan(
        plan,
        store=store,
        adapters={"push": retry, "pr-upsert": retry},
        contract=CONTRACT,
    )
    assert completed.outcome is Outcome.SUCCESS
    assert retry.applied == ["open-pr"]


def test_permission_denial_before_mutation_is_rejected(tmp_path: Path) -> None:
    plan = _plan()
    adapter = FakeAdapter(fail_on="push-core")
    result = execute_plan(
        plan,
        store=OperationStore(tmp_path / "operations", REGISTRY),
        adapters={"push": adapter},
        contract=CONTRACT,
    )
    assert result.outcome is Outcome.REJECTED
    assert result.code == "AUTOMATION_PERMISSION_DENIED"
    assert adapter.applied == []


def test_stale_ref_is_rejected_before_mutation(tmp_path: Path) -> None:
    plan = _plan()
    adapter = FakeAdapter(stale_on="push-core")
    result = execute_plan(
        plan,
        store=OperationStore(tmp_path / "operations", REGISTRY),
        adapters={"push": adapter},
        contract=CONTRACT,
    )
    assert result.outcome is Outcome.REJECTED
    assert result.code == "AUTOMATION_STALE_PLAN"
    assert adapter.applied == []


def test_interrupted_run_retains_exact_resume_command_and_checkpoint(tmp_path: Path) -> None:
    plan = _plan()
    adapter = FakeAdapter(interrupt_on="push-core")
    result = execute_plan(
        plan,
        store=OperationStore(tmp_path / "operations", REGISTRY),
        adapters={"push": adapter},
        contract=CONTRACT,
    )
    assert result.outcome is Outcome.INTERRUPTED
    assert result.exit_code == 130
    assert result.next_actions[0].command == (
        "iii", "automation", "resume", "--operation-id", "operation-123"
    )


def test_stale_or_tampered_plan_is_rejected(tmp_path: Path) -> None:
    plan = _plan()
    store = OperationStore(tmp_path / "operations", REGISTRY)
    execute_plan(plan, store=store, adapters={"push": FakeAdapter()}, contract=CONTRACT)
    tampered = deepcopy(plan)
    tampered["mutations"][0]["new_sha"] = "f" * 40
    with pytest.raises(ContractError, match="identity mismatch"):
        execute_plan(tampered, store=store, adapters={"push": FakeAdapter()}, contract=CONTRACT)
    different = _plan(operation_id="operation-123")
    different["created_at"] = "2026-08-26T00:00:01Z"
    different["plan_id"] = "a" * 64
    with pytest.raises(ContractError):
        store.save_plan(different)


def test_operation_contract_covers_every_settled_automation_boundary() -> None:
    assert set(CONTRACT["operations"]) == {
        "feature-pr",
        "stacked-pr",
        "develop-to-main",
        "main-to-release",
        "qualification",
        "artifact-fetch",
        "deployment-handoff",
    }
    assert CONTRACT["trusted_boundaries"]["pull_request_body"] == "untrusted transport only"


@pytest.mark.parametrize("operation", sorted(CONTRACT["operations"]))
def test_every_operation_family_produces_the_same_versioned_plan_contract(operation: str) -> None:
    specification = CONTRACT["operations"][operation]
    kind = specification["mutation_kinds"][0]
    plan = create_plan(
        operation_id="operation-family-test",
        operation=operation,
        created_at="2026-08-26T00:00:00Z",
        policy={"operation": operation},
        trusted_inputs={"source": "authenticated"},
        repositories=[
            {
                "repository": "DIII-SDU-Group/III-Drone-ros2-ws",
                "ref": "refs/tags/v1.2.3" if kind == "tag-publish" else "refs/heads/develop",
                "expected_old_sha": None,
                "new_sha": "2" * 40,
            }
        ],
        checks=[
            {"id": "contract-test", "status": "passed", "evidence_sha256": "3" * 64}
        ],
        permissions=[
            {
                "repository": "DIII-SDU-Group/III-Drone-ros2-ws",
                "permission": permission,
            }
            for permission in specification["permissions"]
        ],
        mutations=[
            {
                "id": "family-mutation",
                "kind": kind,
                "repository": "DIII-SDU-Group/III-Drone-ros2-ws",
                "ref": "refs/tags/v1.2.3" if kind == "tag-publish" else "refs/heads/develop",
                "expected_old_sha": None,
                "new_sha": "2" * 40,
                "parameters": {"non_interactive": True},
            }
        ],
        contract=CONTRACT,
        registry=REGISTRY,
    )
    assert plan["schema"] == "iii.automation-plan/v1"
    assert plan_result(plan).payload["plan"] == plan


def test_plan_refuses_undeclared_mutation_and_missing_permission() -> None:
    with pytest.raises(ContractError, match="unsupported mutations"):
        _plan(_mutation("bad-mutation", "tag-publish"))
    with pytest.raises(ContractError, match="required permissions"):
        create_plan(
            operation_id="operation-456",
            operation="stacked-pr",
            created_at="2026-08-26T00:00:00Z",
            policy={}, trusted_inputs={},
            repositories=[{"repository": "DIII-SDU-Group/III-Drone-Core", "ref": "refs/heads/x", "expected_old_sha": None, "new_sha": "2" * 40}],
            checks=[], permissions=[{"repository": "DIII-SDU-Group/III-Drone-Core", "permission": "contents:write"}],
            mutations=[_mutation("push-core")], contract=CONTRACT, registry=REGISTRY,
        )


def test_workflows_are_pinned_bounded_least_privilege_and_trust_explicit() -> None:
    root_workflow = yaml.safe_load(
        (ROOT / ".github/workflows/dependency-governance.yml").read_text(encoding="utf-8")
    )
    assert root_workflow["permissions"] == {}
    assert root_workflow["concurrency"]["cancel-in-progress"] is False
    for job_id, job in root_workflow["jobs"].items():
        assert 1 <= job["timeout-minutes"] <= 10
        assert "permissions" in job
        assert all(value != "write" for value in job["permissions"].values()), job_id
        for step in job.get("steps", []):
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[a-f0-9]{40}", step["uses"])
    linked = root_workflow["jobs"]["verify-linked-submodule-prs-merged"]
    assert linked["permissions"] == {"contents": "read", "pull-requests": "read"}
    checkout = linked["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "verify_linked_submodule_prs.py" in linked["steps"][1]["run"]
    assert all("legacy" not in job_id for job_id in root_workflow["jobs"])
    assert "comment-iii-submodule-status" not in root_workflow["jobs"]
    workflow_source = (
        ROOT / ".github/workflows/dependency-governance.yml"
    ).read_text(encoding="utf-8")
    assert "context.payload.pull_request.body" not in workflow_source

    submodule_workflow = yaml.safe_load(
        (ROOT / "deployment/governance/submodule-workflow.yml").read_text(encoding="utf-8")
    )
    assert submodule_workflow["permissions"] == {"contents": "read"}
    assert submodule_workflow["concurrency"]["cancel-in-progress"] is False
    for job in submodule_workflow["jobs"].values():
        assert job["timeout-minutes"] == 10
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[a-f0-9]{40}", step["uses"])
                assert step.get("with", {}).get("persist-credentials") is False


def test_linked_pr_verifier_emits_machine_marker_and_human_summary() -> None:
    source = (ROOT / "scripts/ci/verify_linked_submodule_prs.py").read_text(encoding="utf-8")
    assert "iii-linked-submodule-pr-verification-v1" in source
    assert "Linked III Submodule PR Gate" in source
    workflow = (ROOT / ".github/workflows/dependency-governance.yml").read_text(
        encoding="utf-8"
    )
    assert "iii-submodule-target-verification-v1" in workflow
