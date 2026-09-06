#!/usr/bin/env python3
"""Inspect or materialize a signed receiver A/B self-update artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError, canonical_json  # noqa: E402
from iii_deployment.provisioning_artifacts import (  # noqa: E402
    inspect_receiver_update_materialization,
    materialize_receiver_update,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provisioning-artifacts", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--operation-id", required=True)
    wheel_source = parser.add_mutually_exclusive_group(required=True)
    wheel_source.add_argument(
        "--python",
        type=Path,
        help="build the receiver wheelhouse from the current workspace",
    )
    wheel_source.add_argument(
        "--reuse-provisioning-wheelhouse",
        action="store_true",
        help="explicitly re-sign the unchanged wheelhouse from host provisioning",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        inspection = inspect_receiver_update_materialization(
            output_root=args.output,
            provisioning_root=args.provisioning_artifacts,
            workspace_root=args.workspace_root,
            generation=args.generation,
            version=args.version,
            schema_root=ROOT / "deployment/schemas/v1",
            python_executable=args.python,
        )
        result = (
            materialize_receiver_update(inspection, operation_id=args.operation_id)
            if args.apply
            else {
                "schema": "iii.receiver-update-artifact-inspection/v1",
                "operation_id": args.operation_id,
                "mutation_performed": False,
                "inspection": inspection,
                "next": "repeat the exact command with --apply",
            }
        )
        print(canonical_json(result).decode("utf-8"))
        return 0
    except (ContractError, OSError) as exc:
        print(
            json.dumps(
                {
                    "outcome": "rejected",
                    "code": getattr(exc, "code", "III_RECEIVER_UPDATE_ARTIFACT_ERROR"),
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
