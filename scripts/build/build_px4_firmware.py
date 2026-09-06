#!/usr/bin/env python3
"""Build or reuse the exact PX4 firmware companion for an III release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractRegistry, canonical_json  # noqa: E402
from iii_deployment.px4_release import build_firmware, load_dds_contract, load_firmware_spec  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".cache/iii/px4")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    spec = load_firmware_spec(ROOT / "deployment/px4/firmware.json", registry)
    dds = load_dds_contract(ROOT / "deployment/px4/dds-topics.json", registry)
    artifact, record = build_firmware(
        source_root=ROOT / "PX4-Autopilot",
        spec=spec,
        dds=dds,
        cache_root=args.cache_root,
    )
    registry.validate("px4-firmware-build", record)
    args.output.mkdir(parents=True, exist_ok=True)
    firmware = args.output / artifact.name
    firmware.write_bytes(artifact.read_bytes())
    (args.output / "px4-firmware-build.json").write_bytes(canonical_json(record) + b"\n")
    print(json.dumps({"artifact": str(firmware), "record": record}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
