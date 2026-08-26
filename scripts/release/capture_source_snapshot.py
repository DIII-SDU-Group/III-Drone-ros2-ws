#!/usr/bin/env python3
"""Capture deterministic III source provenance for release construction."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json  # noqa: E402
from iii_deployment.source import (  # noqa: E402
    capture_source_snapshot,
    load_source_policy,
    provenance_markdown,
    validate_component_selection,
)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=ROOT / "deployment/source-policy.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--component", choices=("drone", "gc"), action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    try:
        policy = load_source_policy(args.policy, registry)
        snapshot = capture_source_snapshot(args.workspace, policy, registry)
        if args.component:
            validate_component_selection(snapshot["impact"], args.component)
        _atomic_write(args.output, canonical_json(snapshot) + b"\n")
        _atomic_write(args.report, provenance_markdown(snapshot).encode())
        result = {
            "schema": "iii.source-snapshot-result/v1",
            "outcome": "passed",
            "content_identity": snapshot["content_identity"],
            "clean": snapshot["clean"],
            "components": snapshot["impact"]["components"],
            "snapshot": str(args.output),
            "report": str(args.report),
        }
        print(json.dumps(result, sort_keys=True) if args.json else (
            f"PASS: {result['content_identity']} ({'clean' if result['clean'] else 'dirty'}), "
            f"components={','.join(result['components']) or 'none'}"
        ))
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.source-snapshot-result/v1", "outcome": "failed", "error": str(exc)}
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
