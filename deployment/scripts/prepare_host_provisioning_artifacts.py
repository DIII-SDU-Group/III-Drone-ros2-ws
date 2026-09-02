#!/usr/bin/env python3
"""Inspect or materialize the complete owner-controlled host-provisioning input."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractError, canonical_json  # noqa: E402
from iii_deployment.provisioning_artifacts import (  # noqa: E402
    inspect_materialization,
    materialize,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--enrollment", type=Path, required=True)
    parser.add_argument("--runtime-token", type=Path, required=True)
    parser.add_argument("--ssh-private-key", type=Path, required=True)
    parser.add_argument("--maintenance-ssh-public-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--operator-cidr", required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        inspection = inspect_materialization(
            output_root=args.output,
            workspace_root=args.workspace_root,
            enrollment=args.enrollment,
            runtime_token=args.runtime_token,
            ssh_private_key=args.ssh_private_key,
            maintenance_ssh_public_key=args.maintenance_ssh_public_key,
            known_hosts=args.known_hosts,
            target=args.target,
            operator_cidr=args.operator_cidr,
            python_executable=args.python,
            schema_root=ROOT / "deployment/schemas/v1",
        )
        result = (
            materialize(inspection, operation_id=args.operation_id)
            if args.apply
            else {
                "schema": "iii.host-provisioning-artifact-inspection/v1",
                "operation_id": args.operation_id,
                "mutation_performed": False,
                "inspection": inspection,
                "next": "repeat the exact command with --apply",
            }
        )
        print(canonical_json(result).decode("utf-8"))
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "outcome": "rejected",
                    "code": getattr(exc, "code", "III_HOST_PROVISION_ARTIFACT_ERROR"),
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
