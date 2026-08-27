#!/usr/bin/env python3
"""Audit reversible legacy removal without performing archive mutation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / "deployment/src"))
    from iii_deployment.legacy_retirement import audit, load_policy
    from iii_deployment.verification.matrix import write_json_atomic

    policy = load_policy(root / "deployment/legacy-retirement-policy.json")
    errors = audit(root, policy)
    result = {
        "schema": "iii.legacy-retirement-audit/v1",
        "outcome": "passed" if not errors else "failed",
        "errors": errors,
    }
    if args.output:
        write_json_atomic(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
