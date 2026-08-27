"""Exact authenticated automation plans for coordinated III PR stacks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from .automation import create_plan
from .contracts import ContractError, ContractRegistry, content_identity
from .governance import validate_pr_source


def _git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise ContractError(
            process.stderr.strip() or f"git {' '.join(arguments)} failed in {root}"
        )
    return process.stdout.strip()


def _remote_sha(root: Path, branch: str) -> str | None:
    output = _git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if not output:
        return None
    rows = [line.split() for line in output.splitlines() if line.split()]
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ContractError(f"ambiguous remote branch {branch!r} in {root}")
    return rows[0][0]


def _repository(root: Path) -> str:
    remote = _git(root, "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote,
    )
    if match is None:
        raise ContractError(f"origin is not an exact GitHub repository URL: {remote}")
    return match.group(1)


def build_stack_plan(
    *,
    root: Path,
    targets: Sequence[str],
    base: str,
    feature: str,
    operation_id: str,
    created_at: str | None,
    policy: Mapping[str, Any],
    contract: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    repositories: list[dict[str, Any]] = []
    permissions: list[dict[str, str]] = []
    mutations: list[dict[str, Any]] = []
    trusted_refs: dict[str, dict[str, str | None]] = {}
    selected = [
        (".", root),
        *[(target, root / target) for target in sorted(set(targets))],
    ]
    for index, (path, repository_root) in enumerate(selected):
        kind = "workspace" if path == "." else "submodule"
        validate_pr_source(policy, repository_kind=kind, base=base, head=feature)
        branch = _git(repository_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != feature:
            raise ContractError(f"{path} is on {branch!r}, expected {feature!r}")
        local_sha = _git(repository_root, "rev-parse", f"{feature}^{{commit}}")
        base_sha = _remote_sha(repository_root, base)
        if base_sha is None:
            raise ContractError(f"{path} has no authenticated origin/{base}")
        remote_feature = _remote_sha(repository_root, feature)
        repository = _repository(repository_root)
        ref = f"refs/heads/{feature}"
        repositories.append(
            {
                "repository": repository,
                "ref": ref,
                "expected_old_sha": remote_feature,
                "new_sha": local_sha,
            }
        )
        trusted_refs[repository] = {
            "base": base_sha,
            "feature": remote_feature,
            "local": local_sha,
        }
        for permission in ("contents:write", "pull-requests:write"):
            permissions.append({"repository": repository, "permission": permission})
        parameters = {
            "base": base,
            "base_sha": base_sha,
            "feature": feature,
            "path": path,
            "transport_is_authority": False,
        }
        if remote_feature != local_sha:
            mutations.append(
                {
                    "id": f"push-{index}",
                    "kind": "push",
                    "repository": repository,
                    "ref": ref,
                    "expected_old_sha": remote_feature,
                    "new_sha": local_sha,
                    "parameters": parameters,
                }
            )
        mutations.append(
            {
                "id": f"pr-upsert-{index}",
                "kind": "pr-upsert",
                "repository": repository,
                "ref": ref,
                "expected_old_sha": remote_feature,
                "new_sha": local_sha,
                "parameters": parameters,
            }
        )
    lock = root / "deps/submodule-lock.txt"
    lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    timestamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return create_plan(
        operation_id=operation_id,
        operation="stacked-pr",
        created_at=timestamp,
        policy=policy,
        trusted_inputs={"refs": trusted_refs, "dependency_lock_sha256": lock_sha},
        repositories=repositories,
        checks=(
            {
                "id": "branch-policy",
                "status": "passed",
                "evidence_sha256": content_identity(policy),
            },
            {
                "id": "dependency-lock-input",
                "status": "passed",
                "evidence_sha256": lock_sha,
            },
        ),
        permissions=permissions,
        mutations=mutations,
        contract=contract,
        registry=registry,
    )
