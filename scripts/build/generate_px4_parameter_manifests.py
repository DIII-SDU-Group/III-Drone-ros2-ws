#!/usr/bin/env python3
"""Generate complete real/sim PX4 manifests from a verified full SITL inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import (  # noqa: E402
    ContractRegistry,
    canonical_json,
    content_identity,
)

MAV_TYPES = {"UINT32", "INT32", "REAL32"}
REAL_REQUIRED = {
    "CA_AIRFRAME",
    "CA_ROTOR_COUNT",
    "CA_ROTOR0_KM",
    "CA_ROTOR0_PX",
    "CA_ROTOR0_PY",
    "CA_ROTOR1_KM",
    "CA_ROTOR1_PX",
    "CA_ROTOR1_PY",
    "CA_ROTOR2_KM",
    "CA_ROTOR2_PX",
    "CA_ROTOR2_PY",
    "CA_ROTOR3_KM",
    "CA_ROTOR3_PX",
    "CA_ROTOR3_PY",
    "COM_RC_IN_MODE",
    "COM_RC_LOSS_T",
    "NAV_RCL_ACT",
    "UXRCE_DDS_DOM_ID",
    "UXRCE_DDS_PTCFG",
    "UXRCE_DDS_RX_TO",
    "UXRCE_DDS_SYNCC",
    "UXRCE_DDS_SYNCT",
    "UXRCE_DDS_TX_TO",
}
CALIBRATION_IDENTITY = (
    re.compile(r"^CAL_"),
    re.compile(r"(^|_)ID($|_)"),
    re.compile(r"^(SYS_AUTOSTART|SYS_AUTOCONFIG|SYS_FAC_CAL_MODE)$"),
    re.compile(r"^(COM_FLIGHT_UUID|SDLOG_UUID|UXRCE_DDS_(AG_IP|CFG|KEY|PRT))$"),
    re.compile(r"^(UAVCAN_|UCAN1_|CANNODE_|MAV_SYS_ID|MAV_COMP_ID)"),
)


def airframe_required(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*param\s+set-default\s+([A-Z][A-Z0-9_]{0,15})\s+", line)
        if match:
            names.add(match.group(1))
    if not names:
        raise ValueError(
            "custom PX4 airframe contains no active set-default parameters"
        )
    return names


def classified(
    source: list[dict[str, Any]], *, profile: str, sim_required: set[str]
) -> list[dict[str, Any]]:
    parameters = []
    required = sim_required if profile == "sim" else REAL_REQUIRED
    for item in source:
        name = item["name"]
        mav_type = item["mav_type"]
        value = item["value"]
        if name in required:
            classification = "release-required"
            enforcement = "exact"
            expected = value
        elif any(pattern.search(name) for pattern in CALIBRATION_IDENTITY):
            classification = "calibration-identity"
            enforcement = "preserve"
            expected = None
        else:
            classification = "operator-tunable"
            enforcement = "exact"
            expected = value
        parameters.append(
            {
                "name": name,
                "mav_type": mav_type,
                "value": expected,
                "classification": classification,
                "enforcement": enforcement,
                "tolerance": 1e-6 if mav_type == "REAL32" else 0,
            }
        )
    if profile == "real" and "UXRCE_DDS_CFG" not in {
        item["name"] for item in parameters
    }:
        parameters.append(
            {
                "name": "UXRCE_DDS_CFG",
                "mav_type": "INT32",
                "value": None,
                "classification": "calibration-identity",
                "enforcement": "preserve",
                "tolerance": 0,
            }
        )
    parameters.sort(key=lambda item: item["name"])
    return parameters


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        type=Path,
        required=True,
        help="canonical iii.px4-parameter-snapshot/v1 decoded from live MAVLink",
    )
    parser.add_argument("--airframe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--firmware-version")
    args = parser.parse_args()
    if args.output.exists() and not args.output.is_dir():
        raise SystemExit("output must be a directory")
    raw = args.inventory.read_bytes()
    value = json.loads(raw)
    registry = ContractRegistry(ROOT / "deployment/schemas/v1")
    registry.validate("px4-parameter-snapshot", value)
    source = value["parameters"]
    expected_snapshot_id = content_identity(
        {
            "profile": value["profile"],
            "target": value["target"],
            "parameter_count": value["parameter_count"],
            "parameters": source,
        }
    )
    if (
        value["profile"] != "sim"
        or value["complete"] is not True
        or value["snapshot_id"] != expected_snapshot_id
        or value["parameter_count"] != len(source)
        or {item["index"] for item in source} != set(range(len(source)))
        or len({item["name"] for item in source}) != len(source)
        or any(item["mav_type"] not in MAV_TYPES for item in source)
        or any(
            isinstance(item["value"], bool)
            or not isinstance(item["value"], (int, float))
            or not math.isfinite(float(item["value"]))
            or (item["mav_type"] != "REAL32" and not isinstance(item["value"], int))
            for item in source
        )
    ):
        raise SystemExit(
            "inventory is not a complete identity-valid decoded PX4 snapshot"
        )
    commit = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={ROOT / 'PX4-Autopilot'}",
            "-C",
            str(ROOT / "PX4-Autopilot"),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    firmware_version = args.firmware_version or value["target"]["firmware_version"]
    if value["target"]["firmware_version"] != firmware_version or not commit.startswith(
        value["target"]["firmware_commit"]
    ):
        raise SystemExit("reference snapshot firmware identity differs from PX4 source")
    sim_required = airframe_required(args.airframe)
    missing_required = sorted(
        (sim_required | REAL_REQUIRED) - {item["name"] for item in source}
    )
    if missing_required:
        raise SystemExit(
            "reference inventory misses required keys: " + ", ".join(missing_required)
        )
    source_sha = content_identity(
        {
            "reference_snapshot_id": value["snapshot_id"],
            "reference_snapshot_sha256": hashlib.sha256(
                canonical_json(value) + b"\n"
            ).hexdigest(),
            "airframe_sha256": hashlib.sha256(args.airframe.read_bytes()).hexdigest(),
            "classification_contract": "iii.px4-parameter-classification/v1",
            "px4_commit": commit,
        }
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "reference-sitl-snapshot.json").write_bytes(
        canonical_json(value) + b"\n"
    )
    for profile in ("real", "sim"):
        parameters = classified(source, profile=profile, sim_required=sim_required)
        manifest = {
            "schema": "iii.px4-parameter-manifest/v1",
            "manifest_id": "0" * 64,
            "profile": profile,
            "firmware": {
                "family": "PX4",
                "compatible_range": ">=1.16.1,<1.17.0",
                "reference_version": firmware_version,
                "reference_commit": commit,
            },
            "inventory": {
                "complete": True,
                "parameter_count": len(parameters),
                "source": "px4-sitl-reference",
                "source_sha256": source_sha,
            },
            "parameters": parameters,
        }
        manifest["manifest_id"] = content_identity(
            {key: item for key, item in manifest.items() if key != "manifest_id"}
        )
        registry.validate("px4-parameter-manifest", manifest)
        (args.output / f"{profile}.json").write_bytes(canonical_json(manifest) + b"\n")
        print(profile, manifest["manifest_id"], len(parameters))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
