"""Authenticated retained plan for the workspace-only main-to-release PR."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

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
        raise ContractError(process.stderr.strip() or "Git inspection failed")
    return process.stdout.strip()


def _remote_sha(root: Path, branch: str) -> str:
    output = _git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    rows = [line.split() for line in output.splitlines() if line.split()]
    if len(rows) != 1 or len(rows[0]) != 2:
        raise ContractError(f"origin/{branch} did not resolve to exactly one ref")
    return rows[0][0]


def _repository(root: Path) -> str:
    remote = _git(root, "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote,
    )
    if match is None:
        raise ContractError("origin is not an exact GitHub repository URL")
    return match.group(1)


def build_release_pr_plan(
    *,
    root: Path,
    operation_id: str,
    created_at: str | None,
    policy: Mapping[str, Any],
    contract: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, Any]:
    validate_pr_source(policy, repository_kind="workspace", base="release", head="main")
    repository = _repository(root)
    main_sha = _remote_sha(root, "main")
    release_sha = _remote_sha(root, "release")
    refs = {"repository": repository, "main": main_sha, "release": release_sha}
    return create_plan(
        operation_id=operation_id,
        operation="main-to-release",
        created_at=created_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        policy=policy,
        trusted_inputs={"refs": refs},
        repositories=(
            {
                "repository": repository,
                "ref": "refs/heads/main",
                "expected_old_sha": main_sha,
                "new_sha": main_sha,
            },
        ),
        checks=(
            {
                "id": "branch-policy",
                "status": "passed",
                "evidence_sha256": content_identity(policy),
            },
            {
                "id": "remote-ref-binding",
                "status": "passed",
                "evidence_sha256": content_identity(refs),
            },
        ),
        permissions=({"repository": repository, "permission": "pull-requests:write"},),
        mutations=(
            {
                "id": "pr-upsert-release",
                "kind": "pr-upsert",
                "repository": repository,
                "ref": "refs/heads/main",
                "expected_old_sha": main_sha,
                "new_sha": main_sha,
                "parameters": {
                    "base": "release",
                    "base_sha": release_sha,
                    "head": "main",
                    "head_sha": main_sha,
                    "transport_is_authority": False,
                },
            },
        ),
        contract=contract,
        registry=registry,
    )
