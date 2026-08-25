#!/usr/bin/env python3
"""Regenerate the reviewed maintained-document inventory deterministically."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root / "deployment" / "src"))
    from iii_deployment.verification.documentation import load_policy, materialize_manifest
    from iii_deployment.verification.matrix import write_json_atomic

    policy = load_policy(root / "deployment" / "documentation-policy.json")
    write_json_atomic(args.output, materialize_manifest(root, policy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

