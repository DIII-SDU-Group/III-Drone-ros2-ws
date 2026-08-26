#!/usr/bin/env python3
"""Resolve and lock release-owned CPython 3.12 ARM64 wheels offboard."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.build import run_offboard_command  # noqa: E402
from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json  # noqa: E402
from iii_deployment.target import load_target_definition  # noqa: E402
from iii_deployment.wheels import create_wheel_lock, verify_wheel_lock  # noqa: E402


IMPORTS = ["fastapi", "httpx", "pydantic", "serial", "uvicorn", "websockets", "yaml", "zeroconf"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=ROOT / "deployment/python/requirements.in")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "deployment/python-wheel-lock.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        registry = ContractRegistry(ROOT / "deployment/schemas/v1")
        target = load_target_definition(
            ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json", registry
        )
        resolver = target["images"]["wheel_resolver"]
        image = f"{resolver['reference']}@{resolver['index_digest']}"
        if args.wheelhouse.exists() and any(args.wheelhouse.iterdir()):
            raise ContractError("wheelhouse must be absent or empty for deterministic resolution")
        args.wheelhouse.mkdir(parents=True, exist_ok=True)
        uid_gid = f"{os.getuid()}:{os.getgid()}"
        common = [
            "docker", "run", "--rm", "--platform", resolver["platform"],
            "--user", uid_gid, "-e", "HOME=/tmp", image,
        ]
        version_result = run_offboard_command(
            [*common, "python", "-m", "pip", "--version"], cwd=ROOT
        )
        match = re.match(r"pip ([0-9]+(?:\.[0-9]+){1,2}) ", version_result.stdout)
        if not match:
            raise ContractError("cannot determine pinned resolver pip version")
        run_offboard_command([
            *common[:-1],
            "-v", f"{args.requirements.resolve()}:/input/requirements.in:ro",
            "-v", f"{args.wheelhouse.resolve()}:/wheelhouse",
            image,
            "python", "-m", "pip", "download", "--disable-pip-version-check",
            "--dest", "/wheelhouse", "--requirement", "/input/requirements.in",
            "--platform", "manylinux_2_39_aarch64",
            "--platform", "manylinux_2_17_aarch64",
            "--platform", "manylinux2014_aarch64",
            "--platform", "linux_aarch64",
            "--implementation", "cp", "--python-version", "3.12", "--abi", "cp312",
            "--only-binary=:all:",
        ], cwd=ROOT)
        lock = create_wheel_lock(
            args.wheelhouse, args.requirements, resolver, match.group(1), IMPORTS
        )
        verify_wheel_lock(lock, args.requirements, target, registry)
        temporary = args.lock.with_name(f".{args.lock.name}.partial-{os.getpid()}")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(canonical_json(lock) + b"\n")
        os.replace(temporary, args.lock)
        result = {
            "schema": "iii.python-wheel-resolution/v1", "outcome": "passed",
            "wheels": len(lock["wheels"]), "lock": str(args.lock),
            "requirements_sha256": lock["requirements_sha256"],
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: locked {len(lock['wheels'])} wheels")
        return 0
    except (ContractError, OSError) as exc:
        result = {"schema": "iii.python-wheel-resolution/v1", "outcome": "failed", "error": str(exc)}
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
