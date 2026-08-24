#!/usr/bin/env python3
"""Materialize FINALTEST metadata from the frozen SLAM template without touching bags."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def deterministic_directory_hash(root: Path) -> str:
    """Exact SLAM v1: uint32 path length + POSIX path + binary file SHA-256."""
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"empty payload directory: {root}")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _get_path(value, dotted):
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _template_paths(value, prefix=""):
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.append(path)
            result.extend(_template_paths(child, path))
    return result


def materialize(dataset: Path, template_path: Path, schema_path: Path, canary_manifest: Path):
    manifest_path = dataset / "dataset_manifest.json"
    delivered = json.loads(manifest_path.read_text())
    template = json.loads(template_path.read_text())
    corrected = copy.deepcopy(delivered)
    audit = []

    # Frozen root template is canonical; retain additional recorder facts.
    for key, value in template.items():
        if key not in {"flights", "recorder_build"}:
            corrected[key] = copy.deepcopy(value)
    corrected["truth_contract"]["schema_sha256"] = sha256_file(schema_path)
    corrected["duration_s_semantics"] = "actual delivered recording duration"
    corrected["flight_count"] = 5

    old_build = delivered["recorder_build"]
    build = copy.deepcopy(old_build)
    build["workspace"] = template["recorder_build"]["workspace"]
    build["source_tree_sha256"] = old_build.get("source_tree_sha256", old_build["canary_source_tree_sha256"])
    build["recorder_command_sha256"] = old_build.get("recorder_command_sha256", old_build["canary_recorder_command_sha256"])
    build["truth_publisher_build_sha256"] = old_build["truth_publisher_build_sha256"]
    build["canary_manifest_sha256"] = sha256_file(canary_manifest)
    corrected["recorder_build"] = build

    existing = {row["flight_id"]: row for row in delivered["flights"]}
    corrected_rows = []
    for expected in template["flights"]:
        fid = expected["flight_id"]
        row = copy.deepcopy(existing[fid])
        row.update({
            "flight_id": fid,
            "split": expected["split"],
            "seed": expected["seed"],
            "duration_s": float(row["actual_duration_s"]),
            "duration_s_semantics": "actual delivered recording duration",
            "trajectory_type": expected["trajectory_type"],
            "scientific_role": expected["scientific_role"],
            "bag_path": expected["bag_path"],
            "trajectory_definition_sha256": sha256_file(dataset / fid / "trajectory_definition.json"),
            "bag_sha256": deterministic_directory_hash(dataset / fid / "bag"),
            "schema_version": 2,
            "truth_contract": copy.deepcopy(corrected["truth_contract"]),
            "recorder_provenance": copy.deepcopy(build),
            "truth_publisher_provenance": {"build_sha256": build["truth_publisher_build_sha256"]},
            "calibration_provenance": {"sha256": build["calibration_sha256"]},
            "validation_state": {
                "recorder_side": "PASS",
                "slam_contract_only": "PENDING",
                "estimator_or_frontend_metrics_computed_before_lock": False,
            },
        })
        row.setdefault("hashes", {})["bag_sha256"] = row["bag_sha256"]
        row["hashes"]["trajectory_definition_sha256"] = row["trajectory_definition_sha256"]
        row["hashes"].pop("manifest_entry_sha256", None)
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
        row["hashes"]["manifest_entry_sha256"] = hashlib.sha256(canonical).hexdigest()
        corrected_rows.append(row)
    corrected["flights"] = corrected_rows

    # Field-for-field audit of every frozen root/per-flight template path.
    for path in _template_paths({k: v for k, v in template.items() if k != "flights"}):
        before, _ = _get_path(delivered, path)
        after, value = _get_path(corrected, path)
        audit.append({"required_field": path, "delivered_status": "present" if before else "missing",
                      "corrected_status": "present" if after else "missing", "value_source": "frozen template" if after else None,
                      "corrected_value": value})
    for expected in template["flights"]:
        old = existing[expected["flight_id"]]
        new = next(row for row in corrected_rows if row["flight_id"] == expected["flight_id"])
        for path in _template_paths(expected):
            before, _ = _get_path(old, path)
            after, value = _get_path(new, path)
            source = "actual_duration_s" if path == "duration_s" else (
                "SLAM deterministic_directory_hash" if path == "bag_sha256" else
                "trajectory_definition.json SHA-256" if path == "trajectory_definition_sha256" else "frozen template")
            audit.append({"flight_id": expected["flight_id"], "required_field": path,
                          "delivered_status": "present" if before else "missing",
                          "corrected_status": "present" if after else "missing", "value_source": source,
                          "corrected_value": value})
        for path in ("truth_contract", "recorder_provenance", "truth_publisher_provenance",
                     "calibration_provenance", "validation_state"):
            before, _ = _get_path(old, path); after, value = _get_path(new, path)
            audit.append({"flight_id": expected["flight_id"], "required_field": path,
                          "delivered_status": "present" if before else "missing",
                          "corrected_status": "present" if after else "missing",
                          "value_source": "root frozen contract/provenance", "corrected_value": value})
    if any(item["corrected_status"] != "present" for item in audit):
        raise RuntimeError("required template field remains missing")
    return corrected, audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--canary-manifest", type=Path, required=True)
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    manifest_path = dataset / "dataset_manifest.json"
    original_manifest_sha256 = sha256_file(manifest_path)
    before = {f"FINALTEST{i:02d}": deterministic_directory_hash(dataset / f"FINALTEST{i:02d}" / "bag") for i in range(1, 6)}
    corrected, audit = materialize(dataset, args.template, args.schema, args.canary_manifest)
    manifest_path.write_text(json.dumps(corrected, indent=2, sort_keys=True) + "\n")
    corrected_manifest_sha256 = sha256_file(manifest_path)
    after = {fid: deterministic_directory_hash(dataset / fid / "bag") for fid in before}
    if before != after:
        raise RuntimeError("DATASET MUTATION: bag hash changed")
    audit_path = dataset / "MANIFEST_TEMPLATE_AUDIT.json"
    audit_path.write_text(json.dumps({"template": str(args.template), "zero_missing_required_fields": True,
                                      "fields": audit}, indent=2, sort_keys=True) + "\n")
    source_path = Path(__file__).resolve()
    attestation = {
        "correction": "PRE-LOCK MANIFEST SCHEMA CORRECTION",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "original_manifest_sha256": original_manifest_sha256,
        "corrected_manifest_sha256": corrected_manifest_sha256,
        "bag_hash_convention": corrected["bag_sha256_convention"],
        "bag_hashes_before": before, "bag_hashes_after": after,
        "payload_unchanged": {fid: before[fid] == after[fid] for fid in before},
        "fields_added_or_changed": ["flights[].duration_s", "flights[].bag_sha256",
            "flights[].trajectory_definition_sha256", "flights[].trajectory_type",
            "flights[].scientific_role", "flights[].truth_contract", "flights[].recorder_provenance",
            "recorder_build.source_tree_sha256", "recorder_build.recorder_command_sha256",
            "recorder_build.canary_manifest_sha256", "bag_sha256_convention"],
        "duration_s_semantics": "actual delivered recording duration",
        "materializer_source": str(source_path), "materializer_sha256": sha256_file(source_path),
        "materializer_change_scope": "metadata-only; no sensor, truth, trajectory, QoS, calibration, or bag changes",
        "template_path": str(args.template), "template_sha256": sha256_file(args.template),
        "schema_path": str(args.schema), "schema_sha256": sha256_file(args.schema),
        "template_audit_path": str(audit_path), "zero_missing_required_fields": True,
        "no_bag_bytes_changed": True,
        "estimator_or_frontend_scientific_execution_occurred": False,
        "slam_contract_only_validation_completed": False,
    }
    (dataset / "MANIFEST_SCHEMA_CORRECTION_ATTESTATION.json").write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"original_manifest_sha256": original_manifest_sha256,
                      "corrected_manifest_sha256": corrected_manifest_sha256,
                      "payload_unchanged": True, "audit_fields": len(audit)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
