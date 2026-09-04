#!/usr/bin/env python3
"""Assemble one signed-ready field-development release manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import (  # noqa: E402
    ContractError,
    ContractRegistry,
    canonical_json,
)
from iii_deployment.release_pipeline import assemble_release_manifest  # noqa: E402


def _mapping(values: list[str], *, label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw = value.partition("=")
        if not separator or name not in {"drone", "gc"} or not raw or name in result:
            raise ContractError(f"{label} must contain unique drone=PATH and gc=PATH")
        result[name] = Path(raw).resolve()
    if set(result) != {"drone", "gc"}:
        raise ContractError(f"{label} requires both drone and gc inputs")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--component", action="append", default=[], required=True)
    parser.add_argument("--build-record", action="append", default=[], required=True)
    parser.add_argument("--gc-test-record", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--px4-build-record", type=Path, required=True)
    parser.add_argument("--px4-firmware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--builder-id", default="workstation-field")
    parser.add_argument("--built-at", default=None)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ContractError("field release manifest output already exists")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ContractError("source snapshot must contain one object")
        manifest = assemble_release_manifest(
            root=ROOT,
            version=None,
            source_snapshot_path=args.snapshot,
            provenance_path=args.provenance,
            qualification_evidence_path=None,
            metadata_path=ROOT / "deployment/release-metadata.json",
            target_definition_path=(
                ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json"
            ),
            operational_policy_path=ROOT / "deployment/operational-policy.json",
            documentation_root=ROOT,
            documentation_manifest_path=ROOT
            / "deployment/documentation-manifest.json",
            documentation_policy_path=ROOT / "deployment/documentation-policy.json",
            component_roots=_mapping(args.component, label="component"),
            build_records=_mapping(args.build_record, label="build record"),
            private_key_path=args.private_key,
            builder_id=args.builder_id,
            built_at=args.built_at or _now(),
            source_date_epoch=args.source_date_epoch,
            source_content_identity=snapshot.get("content_identity", ""),
            px4_build_record_path=args.px4_build_record,
            px4_firmware_path=args.px4_firmware,
            registry=ContractRegistry(ROOT / "deployment/schemas/v1"),
            release_class="field-development",
            gc_test_record_path=args.gc_test_record,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(manifest) + b"\n")
        result = {
            "schema": "iii.field-release-manifest-result/v1",
            "outcome": "passed",
            "release_id": manifest["release_id"],
            "output": str(args.output),
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"PASS: {manifest['release_id']}")
        return 0
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        result = {
            "schema": "iii.field-release-manifest-result/v1",
            "outcome": "failed",
            "error": str(exc),
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30


if __name__ == "__main__":
    raise SystemExit(main())
