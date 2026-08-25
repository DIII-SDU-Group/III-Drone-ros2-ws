"""Composable plan/apply/resume primitives for CI, humans, and agents."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ContractError, ContractRegistry, content_identity
from .result import CommandResult, NextAction, Outcome


OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


class AutomationError(RuntimeError):
    code = "AUTOMATION_FAILED"


class StalePlan(AutomationError):
    code = "AUTOMATION_STALE_PLAN"


class PermissionDenied(AutomationError):
    code = "AUTOMATION_PERMISSION_DENIED"


class MutationFailed(AutomationError):
    code = "AUTOMATION_MUTATION_FAILED"


class MutationAdapter(Protocol):
    def preflight(self, mutation: Mapping[str, Any]) -> None: ...

    def apply(self, mutation: Mapping[str, Any]) -> str: ...


def load_automation_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load automation contract: {exc}") from exc
    if value.get("schema") != "iii.automation-contract/v1":
        raise ContractError("unsupported automation contract")
    return value


def create_plan(
    *,
    operation_id: str,
    operation: str,
    created_at: str,
    policy: Mapping[str, Any],
    trusted_inputs: Mapping[str, Any],
    repositories: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    permissions: Sequence[Mapping[str, Any]],
    mutations: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    if not OPERATION_ID.fullmatch(operation_id):
        raise ContractError("invalid automation operation ID")
    specification = contract["operations"].get(operation)
    if specification is None:
        raise ContractError(f"unsupported automation operation {operation!r}")
    unexpected = sorted(
        {str(mutation["kind"]) for mutation in mutations} - set(specification["mutation_kinds"])
    )
    if unexpected:
        raise ContractError(f"operation contains unsupported mutations: {', '.join(unexpected)}")
    granted = {str(permission["permission"]) for permission in permissions}
    missing_permissions = sorted(set(specification["permissions"]) - granted)
    if missing_permissions:
        raise ContractError(
            "operation plan omits required permissions: " + ", ".join(missing_permissions)
        )
    value: dict[str, Any] = {
        "schema": "iii.automation-plan/v1",
        "plan_id": "0" * 64,
        "operation_id": operation_id,
        "operation": operation,
        "created_at": created_at,
        "policy_sha256": content_identity(policy),
        "trusted_input_sha256": content_identity(trusted_inputs),
        "repositories": [dict(item) for item in repositories],
        "checks": [dict(item) for item in checks],
        "permissions": [dict(item) for item in permissions],
        "mutations": [dict(item) for item in mutations],
    }
    value["plan_id"] = content_identity({key: item for key, item in value.items() if key != "plan_id"})
    registry.validate("automation-plan", value)
    return value


def verify_plan_identity(plan: Mapping[str, Any], registry: ContractRegistry) -> None:
    registry.validate("automation-plan", plan)
    unsigned = {key: value for key, value in plan.items() if key != "plan_id"}
    if content_identity(unsigned) != plan["plan_id"]:
        raise ContractError("automation plan content identity mismatch")


@dataclass
class OperationStore:
    root: Path
    registry: ContractRegistry

    def path(self, operation_id: str) -> Path:
        if not OPERATION_ID.fullmatch(operation_id):
            raise ContractError("invalid automation operation ID")
        return self.root / f"{operation_id}.json"

    def plan_path(self, operation_id: str) -> Path:
        if not OPERATION_ID.fullmatch(operation_id):
            raise ContractError("invalid automation operation ID")
        return self.root / f"{operation_id}.plan.json"

    def load_plan(self, operation_id: str) -> dict[str, Any] | None:
        path = self.plan_path(operation_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load retained automation plan: {exc}") from exc
        verify_plan_identity(value, self.registry)
        return value

    def load(self, operation_id: str) -> dict[str, Any] | None:
        path = self.path(operation_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load operation state: {exc}") from exc
        self.registry.validate("operation-state", value)
        return value

    def save(self, state: Mapping[str, Any]) -> None:
        self.registry.validate("operation-state", state)
        self._atomic_save(self.path(str(state["operation_id"])), state)

    def save_plan(self, plan: Mapping[str, Any]) -> None:
        verify_plan_identity(plan, self.registry)
        existing = self.load_plan(str(plan["operation_id"]))
        if existing is not None and existing["plan_id"] != plan["plan_id"]:
            raise StalePlan("operation ID is already bound to a different retained plan")
        if existing is None:
            self._atomic_save(self.plan_path(str(plan["operation_id"])), plan)

    def _atomic_save(self, path: Path, value: Mapping[str, Any]) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        data = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def initial_state(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "iii.operation-state/v1",
        "operation_id": plan["operation_id"],
        "plan_id": plan["plan_id"],
        "state": "planned",
        "attempt": 0,
        "completed_mutations": [],
        "evidence": [],
        "failure": None,
    }


def _action(command: tuple[str, ...], reason: str, *, mutating: bool = False) -> NextAction:
    return NextAction(
        command,
        reason,
        mutating=mutating,
        prerequisites=("Review the retained plan and current live refs.",) if mutating else (),
        confirmation_required=mutating,
    )


def plan_result(plan: Mapping[str, Any]) -> CommandResult:
    return CommandResult(
        command=f"iii automation {plan['operation']}",
        outcome=Outcome.SUCCESS,
        summary=f"Automation plan {plan['plan_id']} is ready; no mutation was performed.",
        code="III_AUTOMATION_PLAN_READY",
        operation_id=str(plan["operation_id"]),
        state="planned",
        payload={"plan": dict(plan)},
        next_actions=(
            _action(
                ("iii", "automation", "apply", "--operation-id", str(plan["operation_id"])),
                "Apply this exact retained plan.",
                mutating=True,
            ),
        ),
    )


def execute_plan(
    plan: Mapping[str, Any],
    *,
    store: OperationStore,
    adapters: Mapping[str, MutationAdapter],
    contract: Mapping[str, Any],
) -> CommandResult:
    verify_plan_identity(plan, store.registry)
    store.save_plan(plan)
    operation_id = str(plan["operation_id"])
    state = store.load(operation_id) or initial_state(plan)
    if state["plan_id"] != plan["plan_id"]:
        raise StalePlan("operation ID is already bound to a different plan")
    if state["state"] == "completed":
        return _completed_result(plan, state, contract, no_op=True)
    state["attempt"] += 1
    state["state"] = "applying"
    state["failure"] = None
    store.save(state)
    completed = set(state["completed_mutations"])
    current_mutation: str | None = None
    try:
        for mutation in plan["mutations"]:
            current_mutation = str(mutation["id"])
            if current_mutation in completed:
                continue
            adapter = adapters.get(str(mutation["kind"]))
            if adapter is None:
                raise MutationFailed(f"no adapter for mutation kind {mutation['kind']!r}")
            adapter.preflight(mutation)
            evidence = adapter.apply(mutation)
            state["completed_mutations"].append(current_mutation)
            state["evidence"].append(evidence)
            completed.add(current_mutation)
            store.save(state)
    except KeyboardInterrupt:
        state["state"] = "interrupted"
        state["failure"] = {
            "code": "AUTOMATION_INTERRUPTED",
            "detail": "client interrupted after the last durable mutation checkpoint",
            "mutation_id": current_mutation,
        }
        store.save(state)
        return _resume_result(plan, state, Outcome.INTERRUPTED)
    except AutomationError as exc:
        state["state"] = "partial" if completed else "rejected"
        state["failure"] = {
            "code": exc.code,
            "detail": str(exc),
            "mutation_id": current_mutation,
        }
        store.save(state)
        if completed:
            return _resume_result(plan, state, Outcome.PARTIAL)
        return CommandResult(
            command=f"iii automation {plan['operation']}",
            outcome=Outcome.REJECTED,
            summary=f"Automation plan was rejected before mutation: {exc}",
            code=exc.code,
            operation_id=operation_id,
            state="rejected",
            payload={"plan_id": plan["plan_id"], "operation_state": state},
            next_actions=(
                _action(
                    ("iii", "automation", "plan", "--operation", str(plan["operation"])),
                    "Replan from authenticated current refs and permissions.",
                ),
            ),
        )
    state["state"] = "completed"
    state["failure"] = None
    store.save(state)
    return _completed_result(plan, state, contract, no_op=False)


def _resume_result(
    plan: Mapping[str, Any], state: Mapping[str, Any], outcome: Outcome
) -> CommandResult:
    return CommandResult(
        command=f"iii automation {plan['operation']}",
        outcome=outcome,
        summary="Automation stopped after a durable checkpoint; completed mutations will not repeat.",
        code=str(state["failure"]["code"]),
        operation_id=str(plan["operation_id"]),
        state=str(state["state"]),
        evidence=tuple(str(item) for item in state["evidence"]),
        payload={"plan_id": plan["plan_id"], "operation_state": dict(state)},
        next_actions=(
            _action(
                ("iii", "automation", "resume", "--operation-id", str(plan["operation_id"])),
                "Revalidate current refs and resume at the first incomplete mutation.",
                mutating=True,
            ),
        ),
    )


def _completed_result(
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    no_op: bool,
) -> CommandResult:
    next_command = tuple(contract["operations"][plan["operation"]]["next_command"])
    return CommandResult(
        command=f"iii automation {plan['operation']}",
        outcome=Outcome.SUCCESS,
        summary="Automation was already complete." if no_op else "Automation completed every planned mutation.",
        code="III_AUTOMATION_ALREADY_COMPLETE" if no_op else "III_AUTOMATION_COMPLETED",
        operation_id=str(plan["operation_id"]),
        state="completed",
        evidence=tuple(str(item) for item in state["evidence"]),
        payload={"plan_id": plan["plan_id"], "operation_state": dict(state)},
        next_actions=(
            _action(
                next_command + ("--operation-id", str(plan["operation_id"])),
                "Inspect or continue from the completed operation context.",
            ),
        ),
    )
