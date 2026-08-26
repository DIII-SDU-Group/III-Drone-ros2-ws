#!/usr/bin/env python3
"""Create one atomic deterministic drone/GC release bundle set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.bundle import load_bundle_limits, package_bundle_set  # noqa: E402
from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402


def _component(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or name not in {"drone", "gc"} or not raw_path:
        raise argparse.ArgumentTypeError("component must be drone=PATH or gc=PATH")
    return name, Path(raw_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--component", type=_component, action="append", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=ROOT / "deployment" / "operational-policy.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
    try:
        for label, path in (("private signer key", args.private_key), ("bundle output", args.output)):
            if path.absolute().is_relative_to(ROOT.resolve()):
                raise ContractError(f"{label} must remain outside the workspace")
        try:
            release_identity = json.loads(
                args.release_manifest.read_text(encoding="utf-8")
            )["release_id"]
        except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot read release identity: {exc}") from exc
        expected_name = f"{release_identity}.iii-release-v1"
        if args.output.name != expected_name:
            raise ContractError(
                f"bundle output directory must be named {expected_name}; identity is still verified from signed content"
            )
        component_roots = dict(args.component)
        if len(component_roots) != len(args.component):
            raise ContractError("component payload roots must be declared exactly once")
        paths = package_bundle_set(
            args.release_manifest,
            component_roots,
            args.private_key,
            args.output,
            registry=registry,
            host_limits=load_bundle_limits(args.policy),
        )
        result = {
            "schema": "iii.release-bundle-package-result/v1",
            "outcome": "passed",
            "output": str(args.output.absolute()),
            "components": {
                component: {
                    "directory": str(value.directory),
                    "archive": str(value.archive),
                }
                for component, value in paths.items()
            },
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: {result['output']}")
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.release-bundle-package-result/v1", "outcome": "rejected", "error": str(exc)}
        print(json.dumps(result, sort_keys=True) if args.json else f"REJECTED: {exc}", file=sys.stderr)
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
