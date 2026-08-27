#!/usr/bin/env python3
"""Retain or revalidate the exact workspace main-to-release PR plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.automation import OperationStore, load_automation_contract
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.governance import load_json
from iii_deployment.release_pr_plan import build_release_pr_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    store = OperationStore(args.state_root, registry)
    try:
        existing = store.load_plan(args.operation_id)
        if args.verify_existing and existing is None:
            raise ContractError(
                "apply requires the retained dry-run plan; run without --yes first"
            )
        plan = build_release_pr_plan(
            root=ROOT,
            operation_id=args.operation_id,
            created_at=existing["created_at"] if existing else None,
            policy=load_json(
                ROOT / "deployment/governance/branch-policy.json",
                "iii.branch-policy/v1",
            ),
            contract=load_automation_contract(
                ROOT / "deployment/automation-contract.json"
            ),
            registry=registry,
        )
        if existing is not None and existing["plan_id"] != plan["plan_id"]:
            raise ContractError(
                "authenticated refs differ from the retained plan; replan with a new operation ID"
            )
        store.save_plan(plan)
        print(json.dumps(plan, sort_keys=True))
        return 0
    except (ContractError, OSError) as exc:
        print(f"RELEASE PR PLAN REJECTED: {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
