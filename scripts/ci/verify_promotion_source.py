#!/usr/bin/env python3
"""Required source/base, mechanical-diff, and signed-evidence promotion gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.governance import (
    governed_source_identity, load_json, required_evidence, validate_attestation_binding,
    validate_mechanical_diff, validate_pr_source, validate_waivers, verify_attestation,
)


def _git_lines(*arguments: str) -> list[str]:
    process = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False)
    if process.returncode:
        raise ContractError(process.stderr.strip() or f"git {' '.join(arguments)} failed")
    return [line for line in process.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repository-kind", choices=("workspace", "submodule"), default="workspace")
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha", default="HEAD")
    parser.add_argument("--develop-ref", default="origin/develop")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--trusted-signers", type=Path)
    parser.add_argument("--phase", choices=("source", "evidence"), default="source")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        branch = load_json(ROOT / "deployment/governance/branch-policy.json", "iii.branch-policy/v1")
        impact = load_json(ROOT / "deployment/governance/change-impact-policy.json", "iii.change-impact-policy/v1")
        validate_pr_source(branch, repository_kind=args.repository_kind, base=args.base, head=args.head)
        changed = _git_lines("diff", "--name-only", f"{args.base_sha}...{args.head_sha}") if args.base_sha else []
        mechanical_changed: list[str] = []
        if args.base == "main":
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", args.develop_ref, args.head_sha],
                cwd=ROOT, check=False,
            )
            if ancestor.returncode != 0:
                raise ContractError("main promotion was not cut from the current develop candidate")
            mechanical_changed = _git_lines(
                "diff", "--name-only", f"{args.develop_ref}...{args.head_sha}"
            )
            validate_mechanical_diff(branch, mechanical_changed)
        reasons = required_evidence(impact, changed)
        if args.phase == "evidence":
            if args.base not in {"main", "release"}:
                raise ContractError("promotion evidence phase applies only to main or release")
            if not args.attestation or not args.trusted_signers:
                raise ContractError("stable promotion requires signed local evidence and trusted signers")
            attestation = json.loads(args.attestation.read_text(encoding="utf-8"))
            signers = json.loads(args.trusted_signers.read_text(encoding="utf-8"))["signers"]
            registry = ContractRegistry(ROOT / "deployment/schemas/v1")
            verify_attestation(attestation, registry=registry, trusted_signers=signers)
            validate_attestation_binding(
                attestation, source_identity=governed_source_identity(ROOT), impact_policy=impact
            )
            category_status = {item["id"]: item["status"] for item in attestation["categories"]}
            validate_waivers(impact, reasons, category_status, attestation["waivers"])
        result = {"schema": "iii.promotion-source-result/v1", "outcome": "pass", "phase": args.phase, "base": args.base, "head": args.head, "changed_paths": changed, "mechanical_changed_paths": mechanical_changed, "required_evidence": reasons}
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: {args.head} -> {args.base}")
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        result = {"schema": "iii.promotion-source-result/v1", "outcome": "rejected", "phase": args.phase, "error": str(exc), "base": args.base, "head": args.head}
        print(json.dumps(result, sort_keys=True) if args.json else f"REJECTED: {exc}")
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
