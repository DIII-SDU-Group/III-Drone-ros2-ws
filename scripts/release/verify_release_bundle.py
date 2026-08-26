#!/usr/bin/env python3
"""Inspect, fully verify, or atomically extract one release component."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.bundle import (  # noqa: E402
    extract_bundle,
    inspect_bundle,
    load_bundle_limits,
    verify_bundle,
)
from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "verify", "extract"))
    parser.add_argument("--bundle", type=Path, required=True, help="Component directory, never an inferred filename")
    parser.add_argument("--trusted-signers", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--policy", type=Path, default=ROOT / "deployment" / "operational-policy.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
    try:
        limits = load_bundle_limits(args.policy)
        if args.mode == "inspect":
            verified = inspect_bundle(
                args.bundle, args.trusted_signers, registry=registry, host_limits=limits
            )
        elif args.mode == "verify":
            verified = verify_bundle(
                args.bundle, args.trusted_signers, registry=registry, host_limits=limits
            )
        else:
            if args.destination is None:
                raise ContractError("extract mode requires --destination")
            verified = extract_bundle(
                args.bundle,
                args.destination,
                args.trusted_signers,
                registry=registry,
                host_limits=limits,
            )
        result = {
            "schema": "iii.release-bundle-verification-result/v1",
            "outcome": "passed",
            "mode": args.mode,
            "release_id": verified.release_manifest["release_id"],
            "release_class": verified.release_manifest["release_class"],
            "component": verified.bundle_manifest["component"],
            "signer_id": verified.signature["signer_id"],
            "archive_sha256": verified.archive_sha256,
            "compressed_bytes": verified.compressed_bytes,
            "content": verified.bundle_manifest["content"],
            "limits": verified.bundle_manifest["limits"],
            "destination": str(args.destination.absolute()) if args.destination else None,
        }
        print(json.dumps(result, sort_keys=True) if args.json else (
            f"PASS: {result['release_id']} {result['component']} "
            f"{result['compressed_bytes']} compressed bytes"
        ))
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.release-bundle-verification-result/v1", "outcome": "rejected", "mode": args.mode, "error": str(exc)}
        print(json.dumps(result, sort_keys=True) if args.json else f"REJECTED: {exc}", file=sys.stderr)
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
