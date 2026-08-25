#!/usr/bin/env python3
"""Plan or publish one qualified version tag from the exact release head."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError  # noqa: E402
from iii_deployment.governance_audit import GhClient, GitHubAuditError, audit_governance  # noqa: E402
from iii_deployment.qualification import inspect_qualification  # noqa: E402
from iii_deployment.result import CommandResult, NextAction, Outcome  # noqa: E402


def _remote_refs(remote: str, version: str) -> tuple[str | None, str | None]:
    process = subprocess.run(
        [
            "git",
            "ls-remote",
            remote,
            "refs/heads/release",
            f"refs/tags/{version}",
            f"refs/tags/{version}^{{}}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise ContractError(process.stderr.strip() or f"cannot inspect release/tag refs on {remote}")
    refs = {
        reference: object_id
        for object_id, reference in (line.split("\t", 1) for line in process.stdout.splitlines())
    }
    return refs.get("refs/heads/release"), (
        refs.get(f"refs/tags/{version}^{{}}") or refs.get(f"refs/tags/{version}")
    )


def _render(result: CommandResult, as_json: bool) -> None:
    print(result.render_json() if as_json else result.render_human())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Publish; default is a read-only plan")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = inspect_qualification(
            ROOT,
            version=args.version,
            evidence_path=args.evidence.resolve(),
            mode="publish",
            release_ref=f"refs/remotes/{args.remote}/release",
        ).require_verified()
        governance = audit_governance(ROOT, GhClient())
        if governance["outcome"] != "passed":
            raise ContractError(
                f"live governance audit found {len(governance['findings'])} drift finding(s)"
            )
        remote_release, remote_tag = _remote_refs(args.remote, args.version)
        if remote_release != report.source_commit:
            raise ContractError(
                f"HEAD {report.source_commit} is not the live {args.remote}/release head "
                f"{remote_release or '<missing>'}"
            )
        if remote_tag is not None:
            raise ContractError(f"qualified version is already published at {remote_tag}")
        mutation = ["git", "push", args.remote, f"{report.source_commit}:refs/tags/{args.version}"]
        if not args.apply:
            result = CommandResult(
                command="iii release publish",
                outcome=Outcome.SUCCESS,
                summary=f"Qualified tag {args.version} is ready to publish from {report.source_commit}.",
                code="III_QUALIFIED_TAG_PLAN_READY",
                operation_id=args.operation_id,
                state="planned",
                payload={
                    "preflight": report.to_dict(),
                    "governance_audit": governance,
                    "mutations": [{"command": mutation}],
                },
                next_actions=(NextAction(tuple([sys.executable, str(Path(__file__).resolve()), "--version", args.version, "--evidence", str(args.evidence.resolve()), "--remote", args.remote, "--operation-id", args.operation_id, "--apply"]), "Publish the preflighted immutable tag.", mutating=True, prerequisites=("Review the exact source commit and retained evidence.",), confirmation_required=True),),
            )
        else:
            process = subprocess.run(mutation, cwd=ROOT, capture_output=True, text=True, check=False)
            if process.returncode:
                raise ContractError(process.stderr.strip() or "qualified tag push failed")
            result = CommandResult(
                command="iii release publish",
                outcome=Outcome.SUCCESS,
                summary=f"Published immutable qualified tag {args.version} at {report.source_commit}.",
                code="III_QUALIFIED_TAG_PUBLISHED",
                operation_id=args.operation_id,
                state="completed",
                evidence=(str(args.evidence.resolve()), governance["audit_id"]),
                payload={
                    "preflight": report.to_dict(),
                    "governance_audit": governance,
                    "remote": args.remote,
                },
                terminal_reason="The tag-triggered qualified CI pipeline now owns build and publication.",
            )
        _render(result, args.json)
        return result.exit_code
    except (ContractError, GitHubAuditError, OSError) as exc:
        result = CommandResult(
            command="iii release publish",
            outcome=Outcome.REJECTED,
            summary=f"Qualified tag publication refused: {exc}",
            code="III_QUALIFIED_TAG_REFUSED",
            operation_id=args.operation_id,
            state="rejected",
            terminal_reason="No qualified tag was published; correct the retained preflight state and use a fresh version after any failed publication.",
        )
        _render(result, args.json)
        return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
