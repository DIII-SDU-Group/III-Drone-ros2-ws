"""CLI for the deployment backlog and verification matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from iii_deployment.result import CommandResult, NextAction, Outcome
from .backlog import BacklogError, parse_backlog
from .matrix import clause_baseline, materialize, verify_clause_baseline, write_json_atomic


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iii-deployment-verify")
    parser.add_argument("--backlog", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--write-matrix", action="store_true")
    return parser


def run(arguments: list[str] | None = None) -> CommandResult:
    args = _parser().parse_args(arguments)
    try:
        backlog = parse_backlog(args.backlog)
        if args.update_baseline:
            write_json_atomic(args.baseline, clause_baseline(backlog))
        errors = verify_clause_baseline(backlog, args.baseline)
        if args.write_matrix:
            if args.matrix is None:
                raise BacklogError("--matrix is required with --write-matrix")
            write_json_atomic(args.matrix, materialize(backlog))
        if errors:
            return CommandResult(
                command="iii verify deployment",
                outcome=Outcome.REJECTED,
                summary=f"Deployment traceability audit rejected {len(errors)} clause changes.",
                code="III_VERIFY_CLAUSE_DRIFT",
                payload={"errors": errors},
                next_actions=(
                    NextAction(
                        ("iii", "verify", "deployment", "--show-clause-drift"),
                        "Review and explicitly map every changed clause.",
                    ),
                ),
            )
        return CommandResult(
            command="iii verify deployment",
            outcome=Outcome.SUCCESS,
            summary=(
                f"Deployment traceability is complete: {len(backlog.clauses)} decision clauses "
                f"and {sum(len(task.acceptance) for task in backlog.tasks.values())} task criteria."
            ),
            code="III_VERIFY_OK",
            payload={"clauses": len(backlog.clauses), "tasks": len(backlog.tasks)},
            next_actions=(
                NextAction(
                    ("iii", "docs", "check"),
                    "Validate the canonical operating manual and generated references.",
                ),
            ),
        )
    except (BacklogError, OSError, ValueError) as exc:
        return CommandResult(
            command="iii verify deployment",
            outcome=Outcome.FAILED,
            summary="Deployment verification matrix could not be audited.",
            code="III_VERIFY_INVALID",
            payload={"error": str(exc)},
            next_actions=(
                NextAction(("iii", "verify", "deployment", "--json"), "Inspect the structured audit failure."),
            ),
        )


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    json_mode = "--json" in args
    result = run(args)
    print(result.render_json() if json_mode else result.render_human())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

