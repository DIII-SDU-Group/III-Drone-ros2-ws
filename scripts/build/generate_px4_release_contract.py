#!/usr/bin/env python3
"""Generate or verify the exact release-owned PX4 firmware inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import ContractRegistry, canonical_json, content_identity  # noqa: E402
from iii_deployment.px4_release import normalized_dds_topics, validate_release_inputs  # noqa: E402


def document(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--version", default="1.16.1")
    args = parser.parse_args()
    px4 = ROOT / "PX4-Autopilot"
    commit = subprocess.run(
        ["git", "-c", f"safe.directory={px4}", "-C", str(px4), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dds = normalized_dds_topics(
        px4 / "src/modules/uxrce_dds_client/dds_topics.yaml",
        firmware_commit=commit,
    )
    network = document(ROOT / "deployment/px4/network-baseline.json")
    parameters = document(ROOT / "deployment/px4/real.json")
    body = {
        "schema": "iii.px4-firmware-spec/v1",
        "family": "PX4",
        "version": args.version,
        "git_commit": commit,
        "advertised_commit": commit[:10],
        "board": {
            "target": "px4_fmu-v6x_multicopter",
            "board_id": 53,
            "architecture": "PX4_FMU_V6X",
        },
        "source": {
            "submodule": "PX4-Autopilot",
            "dds_topics": "src/modules/uxrce_dds_client/dds_topics.yaml",
        },
        "build": {
            "command": ["make", "px4_fmu-v6x_multicopter"],
            "artifact": "px4_fmu-v6x_multicopter.px4",
        },
        "dds_topics_id": dds["contract_id"],
        "network_baseline_id": network["baseline_id"],
        "parameter_manifest_id": parameters["manifest_id"],
    }
    spec = {"spec_id": content_identity(body), **body}
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    registry.validate("px4-dds-topics", dds)
    registry.validate("px4-firmware-spec", spec)
    validate_release_inputs(
        spec=spec,
        dds=dds,
        network=network,
        parameters=parameters,
        registry=registry,
    )
    expected = {
        ROOT / "deployment/px4/dds-topics.json": canonical_json(dds) + b"\n",
        ROOT / "deployment/px4/firmware.json": canonical_json(spec) + b"\n",
    }
    if args.check:
        stale = [str(path.relative_to(ROOT)) for path, raw in expected.items() if not path.is_file() or path.read_bytes() != raw]
        if stale:
            raise SystemExit("stale PX4 release contract: " + ", ".join(stale))
    else:
        for path, raw in expected.items():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(path)
    print(spec["spec_id"], dds["contract_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
