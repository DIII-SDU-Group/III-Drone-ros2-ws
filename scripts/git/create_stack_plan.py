#!/usr/bin/env python3
"""Retain or revalidate the exact automation plan used by create_stack_prs.sh."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.automation import (  # noqa: E402
    OperationStore,
    load_automation_contract,
)
from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402
from iii_deployment.governance import load_json  # noqa: E402
from iii_deployment.stack_plan import build_stack_plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, choices=("develop", "main"))
    parser.add_argument("--feature", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--target", action="append", default=[])
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
        plan = build_stack_plan(
            root=ROOT,
            targets=args.target,
            base=args.base,
            feature=args.feature,
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
        print(f"STACK PLAN REJECTED: {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
