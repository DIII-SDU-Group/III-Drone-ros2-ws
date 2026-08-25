#!/usr/bin/env python3
"""Audit live GitHub branches and rulesets without mutation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError  # noqa: E402
from iii_deployment.governance_audit import (  # noqa: E402
    GhClient,
    GitHubAuditError,
    audit_governance,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit the versioned evidence record")
    args = parser.parse_args()
    try:
        report = audit_governance(ROOT, GhClient())
    except (ContractError, GitHubAuditError, OSError) as exc:
        failure = {
            "schema": "iii.github-governance-audit/v1",
            "outcome": "error",
            "code": "GOVERNANCE_AUDIT_UNAVAILABLE",
            "error": str(exc),
        }
        print(json.dumps(failure, sort_keys=True) if args.json else f"ERROR: {exc}")
        return 30
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    elif report["outcome"] == "passed":
        print(
            f"PASS: {report['repositories']} repositories and "
            f"{report['observed_rulesets']} rulesets match policy ({report['audit_id']})."
        )
    else:
        print(f"FAIL: {len(report['findings'])} governance drift finding(s).")
        for finding in report["findings"]:
            print(
                f"- {finding['id']} {finding['repository']}/{finding['subject']}: "
                f"{finding['detail']}"
            )
    return 0 if report["outcome"] == "passed" else 20


if __name__ == "__main__":
    raise SystemExit(main())
