#!/usr/bin/env python3
"""Trusted shared feature/develop/main source gate for editable III repositories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


POLICY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(POLICY_ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError
from iii_deployment.governance import load_json, validate_pr_source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--head-sha", default="HEAD")
    args = parser.parse_args()
    try:
        policy = load_json(POLICY_ROOT / "deployment/governance/branch-policy.json", "iii.branch-policy/v1")
        validate_pr_source(policy, repository_kind="submodule", base=args.base, head=args.head)
        if args.base == "main":
            process = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "origin/develop", args.head_sha],
                cwd=args.repository_root, check=False,
            )
            if process.returncode:
                raise ContractError("submodule main promotion is not develop-derived")
        print(json.dumps({
            "schema": "iii.submodule-promotion-source-result/v1", "outcome": "pass",
            "base": args.base, "head": args.head,
        }, sort_keys=True))
        return 0
    except ContractError as exc:
        print(json.dumps({
            "schema": "iii.submodule-promotion-source-result/v1", "outcome": "rejected",
            "base": args.base, "head": args.head, "error": str(exc),
        }, sort_keys=True))
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
