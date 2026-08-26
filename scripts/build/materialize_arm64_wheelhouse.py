#!/usr/bin/env python3
"""Materialize the exact committed ARM64 wheel lock in a clean wheelhouse."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.build import run_offboard_command  # noqa: E402
from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402
from iii_deployment.target import load_target_definition  # noqa: E402
from iii_deployment.wheels import load_wheel_lock, verify_wheelhouse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "deployment/python-wheel-lock.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        registry = ContractRegistry(ROOT / "deployment/schemas/v1")
        target = load_target_definition(
            ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json", registry
        )
        lock = load_wheel_lock(
            args.lock, ROOT / "deployment/python/requirements.in", target, registry
        )
        if args.wheelhouse.exists() and any(args.wheelhouse.iterdir()):
            raise ContractError("wheelhouse must be absent or empty")
        args.wheelhouse.mkdir(parents=True, exist_ok=True)
        resolver = target["images"]["wheel_resolver"]
        image = f"{resolver['reference']}@{resolver['index_digest']}"
        requirements = [f"{wheel['name']}=={wheel['version']}" for wheel in lock["wheels"]]
        run_offboard_command([
            "docker", "run", "--rm", "--platform", resolver["platform"],
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-v", f"{args.wheelhouse.resolve()}:/wheelhouse", image,
            "python", "-m", "pip", "download", "--disable-pip-version-check",
            "--dest", "/wheelhouse", "--no-deps",
            "--platform", "manylinux_2_39_aarch64",
            "--platform", "manylinux_2_17_aarch64",
            "--platform", "manylinux2014_aarch64",
            "--platform", "linux_aarch64",
            "--implementation", "cp", "--python-version", "3.12", "--abi", "cp312",
            "--only-binary=:all:", *requirements,
        ], cwd=ROOT)
        verify_wheelhouse(args.wheelhouse, lock)
        result = {
            "schema": "iii.python-wheelhouse-materialization/v1", "outcome": "passed",
            "wheels": len(lock["wheels"]), "wheelhouse": str(args.wheelhouse),
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: materialized {len(lock['wheels'])} wheels")
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.python-wheelhouse-materialization/v1", "outcome": "failed", "error": str(exc)}
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
