#!/usr/bin/env python3
"""Approve the exact current maintained-document manifest after source review."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument(
        "--approve",
        action="store_true",
        help="confirm that every maintained document was reviewed against all checks",
    )
    args = parser.parse_args()
    if not args.approve:
        parser.error("--approve is required; migration review is not inferred")

    root = args.root.resolve()
    sys.path.insert(0, str(root / "deployment" / "src"))
    from iii_deployment.verification.documentation import (
        materialize_review,
        read_manifest,
    )
    from iii_deployment.verification.matrix import write_json_atomic

    manifest = read_manifest(root / "deployment" / "documentation-manifest.json")
    write_json_atomic(args.output, materialize_review(manifest, reviewer=args.reviewer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
