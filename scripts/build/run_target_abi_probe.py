#!/usr/bin/env python3
"""Build and execute the canonical ARM64 ABI probe in pinned OCI images."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment" / "src"))

from iii_deployment.contracts import ContractError, ContractRegistry  # noqa: E402
from iii_deployment.target import load_target_definition, verify_target_probe  # noqa: E402


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target-definition",
        type=Path,
        default=ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    try:
        definition = load_target_definition(args.target_definition, registry)
        image = f"iii-target-abi-probe:{definition['definition_id'][:16]}"
        if not args.skip_build:
            build = _run([
                "docker", "buildx", "build", "--load", "--target", "abi-probe",
                "--platform", "linux/arm64", "--tag", image, "--file", "Dockerfile.cc", ".",
            ])
            if build.returncode:
                raise ContractError(build.stderr.strip() or build.stdout.strip() or "ABI probe image build failed")
        execution = _run(["docker", "run", "--rm", "--platform", "linux/arm64", image])
        if execution.returncode:
            raise ContractError(execution.stderr.strip() or execution.stdout.strip() or "ABI probe failed")
        probe = json.loads(execution.stdout)
        verify_target_probe(definition, probe, registry)
        report = {
            "schema": "iii.target-abi-probe-result/v1",
            "outcome": "passed",
            "definition_id": definition["definition_id"],
            "probe": probe,
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")) if args.json else (
            f"PASS: {probe['target_id']} {probe['architecture']} ROS {probe['ros']} "
            f"{probe['python_abi']} glibc {probe['libc_version']} GCC {probe['compiler_version']}"
        ))
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        report = {"schema": "iii.target-abi-probe-result/v1", "outcome": "failed", "error": str(exc)}
        print(json.dumps(report, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
