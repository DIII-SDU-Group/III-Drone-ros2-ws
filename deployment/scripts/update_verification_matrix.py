#!/usr/bin/env python3
"""Regenerate the reviewed deployment verification definition deterministically."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.verification.backlog import parse_backlog
from iii_deployment.verification.matrix import (
    load_policy,
    materialize,
    write_json_atomic,
)


if __name__ == "__main__":
    backlog = parse_backlog(
        ROOT / "codex-backlogs/deployment-infrastructure-redesign.md"
    )
    policy = load_policy(ROOT / "deployment/verification/policy.json")
    write_json_atomic(
        ROOT / "deployment/verification/matrix.json", materialize(backlog, policy)
    )
