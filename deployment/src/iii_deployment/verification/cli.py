"""Canonical deployment verification-matrix audit command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Iterable

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.result import CommandResult, Finding, NextAction, Outcome
from iii_deployment.signers import load_trusted_signers

from .backlog import BacklogError, parse_backlog
from .evidence import read_evidence
from .matrix import (
    audit_matrix,
    junit_xml,
    load_clause_migrations,
    load_policy,
    materialize,
    read_matrix,
    verification_result,
    verify_clause_baseline,
    write_json_atomic,
)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def audit(
    *,
    backlog_path: Path,
    baseline_path: Path,
    migrations_path: Path,
    policy_path: Path,
    matrix_path: Path,
    schema_root: Path,
    evidence_paths: Iterable[Path] = (),
    trusted_signers_path: Path | None = None,
    junit_path: Path | None = None,
    report_path: Path | None = None,
    write_matrix: bool = False,
    require_levels: Iterable[str] = (),
    require_complete: bool = False,
    audit_only: bool = False,
) -> CommandResult:
    try:
        backlog = parse_backlog(backlog_path)
        load_clause_migrations(migrations_path)
        policy = load_policy(policy_path)
        baseline_errors = verify_clause_baseline(
            backlog, baseline_path, migrations_path
        )
        if write_matrix:
            write_json_atomic(matrix_path, materialize(backlog, policy))
        matrix = read_matrix(matrix_path)
        errors = [*baseline_errors, *audit_matrix(backlog, policy, matrix)]
        if errors:
            return CommandResult(
                command="iii verify deployment",
                outcome=Outcome.REJECTED,
                summary=f"Deployment traceability rejected {len(errors)} definition error(s).",
                code="III_VERIFY_DEFINITION_DRIFT",
                findings=tuple(
                    Finding("III_VERIFY_DEFINITION_DRIFT", error) for error in errors
                ),
                next_actions=(
                    NextAction(
                        ("iii", "verify", "deployment", "--json"),
                        "Review clause, policy, and matrix drift before regenerating reviewed definitions.",
                    ),
                ),
            )
        registry = ContractRegistry(schema_root)
        trusted = (
            load_trusted_signers(trusted_signers_path, registry)
            if trusted_signers_path
            else None
        )
        records = [
            read_evidence(
                path,
                matrix=matrix,
                policy=policy,
                registry=registry,
                trusted_signers=trusted,
            )
            for path in evidence_paths
        ]
        result = verification_result(matrix, records)
        if report_path is not None:
            write_json_atomic(report_path, result)
        if junit_path is not None:
            _atomic_bytes(junit_path, junit_xml(result))
        if audit_only:
            return CommandResult(
                command="iii verify deployment",
                outcome=Outcome.SUCCESS,
                summary=f"Deployment matrix definitions are valid: {len(result['rows'])} governed rows.",
                code="III_VERIFY_DEFINITIONS_VALID",
                evidence=tuple(
                    str(path) for path in (report_path, junit_path) if path is not None
                ),
                payload_schema=result["schema"],
                payload=result,
                terminal_reason="Clause, ownership, test-path, policy, and matrix identities match the reviewed source.",
            )
        required = set(require_levels)
        invalid_levels = required - set(policy["levels"])
        if invalid_levels:
            raise BacklogError(
                "unknown required verification levels: "
                + ", ".join(sorted(invalid_levels))
            )
        blocking = [
            row
            for row in result["rows"]
            if row["blocking"]
            and (require_complete or row["level"] in required)
            and row["status"] not in {"pass", "warn"}
        ]
        if blocking:
            return CommandResult(
                command="iii verify deployment",
                outcome=Outcome.REJECTED,
                summary=f"Deployment verification is incomplete for {len(blocking)} required row(s).",
                code="III_VERIFY_REQUIRED_INCOMPLETE",
                findings=tuple(
                    Finding(
                        "III_VERIFY_REQUIRED_INCOMPLETE",
                        f"{row['id']}: {row['status']} ({row['reason']})",
                    )
                    for row in blocking[:100]
                ),
                evidence=tuple(
                    str(path)
                    for path in (report_path, junit_path, *evidence_paths)
                    if path is not None
                ),
                payload_schema=result["schema"],
                payload=result,
                next_actions=(
                    NextAction(
                        tuple(policy["levels"][blocking[0]["level"]]["owner_command"]),
                        f"Produce authenticated {blocking[0]['level']} evidence for the first incomplete row.",
                    ),
                ),
            )
        pending = result["counts"]["not_run"] + result["counts"]["skipped"]
        return CommandResult(
            command="iii verify deployment",
            outcome=Outcome.WARNING if pending else Outcome.SUCCESS,
            summary=(
                f"Deployment matrix is structurally complete: {len(result['rows'])} rows; "
                f"{pending} await authenticated execution evidence."
            ),
            code=("III_VERIFY_PENDING" if pending else "III_VERIFY_COMPLETE"),
            evidence=tuple(
                str(path)
                for path in (report_path, junit_path, *evidence_paths)
                if path is not None
            ),
            payload_schema=result["schema"],
            payload=result,
            next_actions=(
                (
                    NextAction(
                        tuple(policy["levels"]["host-independent"]["owner_command"]),
                        "Run and retain the CI-capable host-independent verification layer.",
                    ),
                )
                if pending
                else ()
            ),
            terminal_reason=(
                None
                if pending
                else "Every blocking matrix row has authenticated current-candidate evidence."
            ),
        )
    except (BacklogError, ContractError, OSError, ValueError) as exc:
        return CommandResult(
            command="iii verify deployment",
            outcome=Outcome.FAILED,
            summary="Deployment verification matrix could not be audited.",
            code="III_VERIFY_INVALID",
            findings=(Finding("III_VERIFY_INVALID", str(exc)),),
            next_actions=(
                NextAction(
                    ("iii", "verify", "deployment", "--json"),
                    "Inspect the structured validation failure without applying changes.",
                ),
            ),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iii-deployment-verify")
    parser.add_argument("--backlog", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--migrations", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--schema-root", type=Path, required=True)
    parser.add_argument("--evidence", action="append", type=Path, default=[])
    parser.add_argument("--trusted-signers", type=Path)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--write-matrix", action="store_true")
    parser.add_argument(
        "--require-level",
        action="append",
        choices=("host-independent", "target-equivalent", "physical"),
        default=[],
    )
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(arguments: list[str] | None = None) -> CommandResult:
    args = _parser().parse_args(arguments)
    return audit(
        backlog_path=args.backlog,
        baseline_path=args.baseline,
        migrations_path=args.migrations,
        policy_path=args.policy,
        matrix_path=args.matrix,
        schema_root=args.schema_root,
        evidence_paths=args.evidence,
        trusted_signers_path=args.trusted_signers,
        junit_path=args.junit,
        report_path=args.report,
        write_matrix=args.write_matrix,
        require_levels=args.require_level,
        require_complete=args.require_complete,
        audit_only=args.audit_only,
    )


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    result = run(args)
    print(result.render_json() if "--json" in args else result.render_human())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
