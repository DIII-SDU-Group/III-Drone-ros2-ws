#!/usr/bin/env python3
"""Trusted qualified-release pipeline entry point used by GitHub Actions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
)  # noqa: E402
from iii_deployment.github_publication import (  # noqa: E402
    GhReleasePublisher,
    publish_failed_attempt,
    publish_qualified_release,
)
from iii_deployment.governance import (  # noqa: E402
    governed_source_identity,
    required_evidence,
    validate_attestation_binding,
    validate_waivers,
    verify_attestation,
)
from iii_deployment.qualification import inspect_qualification  # noqa: E402
from iii_deployment.qualified_release import (  # noqa: E402
    create_qualification_attempt,
    write_canonical,
)
from iii_deployment.release_pipeline import (  # noqa: E402
    assemble_qualification_evidence,
    assemble_release_manifest,
    assemble_signed_release,
    create_qualification_check,
    git_change_summary,
)
from iii_deployment.signers import load_trusted_signers  # noqa: E402
from iii_deployment.source import (  # noqa: E402
    capture_source_snapshot,
    load_source_policy,
    provenance_markdown,
)

REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")


def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def _mapping(values: list[str] | None, *, label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values or []:
        name, separator, raw = value.partition("=")
        if not separator or not name or not raw or name in result:
            raise ContractError(f"{label} must be unique NAME=PATH values")
        result[name] = Path(raw).resolve()
    return result


def _run_json(command: list[str]) -> Any:
    process = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if process.returncode:
        raise ContractError(process.stderr.strip() or f"{' '.join(command)} failed")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{' '.join(command)} returned invalid JSON") from exc


def eligibility(args: argparse.Namespace) -> dict[str, Any]:
    if args.event_ref != f"refs/tags/{args.version}":
        raise ContractError(
            "qualification workflow ref is not the exact requested version tag"
        )
    report = inspect_qualification(
        ROOT,
        version=args.version,
        evidence_path=Path("/__qualification_evidence_not_assembled__"),
        mode="build",
        release_ref=args.release_ref,
        require_evidence=False,
    ).require_verified()
    policy = load_source_policy(ROOT / "deployment/source-policy.json", REGISTRY)
    snapshot = capture_source_snapshot(ROOT, policy, REGISTRY)
    if not snapshot["clean"] or snapshot["workspace_commit"] != report.source_commit:
        raise ContractError(
            "captured source differs from the clean qualified tag preflight"
        )
    args.output.mkdir(parents=True, exist_ok=False)
    write_canonical(args.output / "source-snapshot.json", snapshot)
    (args.output / "source-provenance.md").write_text(
        provenance_markdown(snapshot), encoding="utf-8"
    )
    changes = git_change_summary(ROOT, args.version, REGISTRY)
    write_canonical(args.output / "release-change-summary.json", changes)
    write_canonical(args.output / "qualification-preflight.json", report.to_dict())
    return {
        "source_commit": report.source_commit,
        "source_identity": governed_source_identity(ROOT),
        "snapshot_identity": snapshot["content_identity"],
        "output": str(args.output),
    }


def fetch_promotion(args: argparse.Namespace) -> dict[str, Any]:
    pulls = _run_json(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{args.repository}/commits/{args.source_commit}/pulls",
            "-H",
            "Accept: application/vnd.github+json",
        ]
    )
    matches = [
        item
        for item in pulls
        if item.get("base", {}).get("ref") == "release"
        and item.get("head", {}).get("ref") == "main"
        and item.get("merged_at")
        and item.get("merge_commit_sha") == args.source_commit
    ]
    if len(matches) != 1:
        raise ContractError(
            f"expected one authenticated merged main -> release PR for tag commit; found {len(matches)}"
        )
    number = int(matches[0]["number"])
    comments = _run_json(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{args.repository}/issues/{number}/comments?per_page=100",
        ]
    )
    if comments and isinstance(comments[0], list):
        comments = [item for page in comments for item in page]
    marker = "<!-- iii-promotion-evidence-v1 -->"
    candidates = [item for item in comments if marker in (item.get("body") or "")]
    if len(candidates) != 1:
        raise ContractError(
            f"expected exactly one promotion evidence comment; found {len(candidates)}"
        )
    fenced = re.search(r"```json\s*([\s\S]*?)```", candidates[0]["body"])
    if fenced is None:
        raise ContractError("promotion evidence comment has no JSON object")
    try:
        attestation = json.loads(fenced.group(1))
    except json.JSONDecodeError as exc:
        raise ContractError("promotion evidence comment contains invalid JSON") from exc
    if not isinstance(attestation, dict):
        raise ContractError("promotion evidence transport is not an object")
    write_canonical(args.output, attestation)
    return {"pull_request": number, "attestation": str(args.output)}


def promotion_check(args: argparse.Namespace) -> dict[str, Any]:
    attestation = _json(args.attestation)
    impact = _json(ROOT / "deployment/governance/change-impact-policy.json")
    trusted = _json(args.trusted_signers).get("signers", {})
    if not isinstance(trusted, dict):
        raise ContractError("promotion signer trust must contain a signer mapping")
    verify_attestation(attestation, registry=REGISTRY, trusted_signers=trusted)
    validate_attestation_binding(
        attestation,
        source_identity=governed_source_identity(ROOT),
        impact_policy=impact,
    )
    changes = _json(args.change_summary)
    reasons = required_evidence(impact, changes["changed_paths"])
    category_status = {item["id"]: item["status"] for item in attestation["categories"]}
    validate_waivers(impact, reasons, category_status, attestation["waivers"])
    return {
        "attestation_id": attestation["attestation_id"],
        "required_categories": reasons,
    }


def check_record(args: argparse.Namespace) -> dict[str, Any]:
    try:
        command = json.loads(args.command_json)
    except json.JSONDecodeError as exc:
        raise ContractError("check command JSON is invalid") from exc
    if not isinstance(command, list) or any(
        not isinstance(value, str) for value in command
    ):
        raise ContractError("check command JSON must be an argv string array")
    record = create_qualification_check(
        check_id=args.check_id,
        source_commit=args.source_commit,
        version=args.version,
        started_at=args.started_at,
        finished_at=args.finished_at,
        command=command,
        log_path=args.log,
        outputs=_mapping(args.output_file, label="check output"),
        registry=REGISTRY,
    )
    write_canonical(args.output, record)
    return {"check_id": args.check_id, "record": str(args.output)}


def evidence(args: argparse.Namespace) -> dict[str, Any]:
    value = assemble_qualification_evidence(
        version=args.version,
        source_commit=args.source_commit,
        dependency_lock_path=args.dependency_lock,
        check_paths=_mapping(args.check, label="qualification check"),
        registry=REGISTRY,
    )
    write_canonical(args.output, value)
    return {"evidence": str(args.output), "checks": len(value["required_checks"])}


def manifest(args: argparse.Namespace) -> dict[str, Any]:
    value = assemble_release_manifest(
        root=ROOT,
        version=args.version,
        source_snapshot_path=args.source_snapshot,
        provenance_path=args.provenance,
        qualification_evidence_path=args.qualification_evidence,
        metadata_path=args.metadata,
        target_definition_path=args.target_definition,
        operational_policy_path=args.policy,
        documentation_root=ROOT,
        documentation_manifest_path=ROOT / "deployment/documentation-manifest.json",
        documentation_policy_path=ROOT / "deployment/documentation-policy.json",
        component_roots=_mapping(args.component, label="component"),
        build_records=_mapping(args.build_record, label="build record"),
        private_key_path=args.private_key,
        builder_id=args.builder_id,
        built_at=args.built_at,
        source_date_epoch=args.source_date_epoch,
        source_content_identity=governed_source_identity(ROOT),
        registry=REGISTRY,
    )
    write_canonical(args.output, value)
    return {"manifest": str(args.output), "release_id": value["release_id"]}


def sign_release(args: argparse.Namespace) -> dict[str, Any]:
    manifest_value = _json(args.manifest)
    metadata_value = _json(args.metadata)
    trust_value = _json(args.promotion_trusted_signers).get("signers", {})
    if not isinstance(trust_value, dict):
        raise ContractError("promotion signer trust must contain a signer mapping")
    paths = assemble_signed_release(
        root=ROOT,
        manifest=manifest_value,
        manifest_path=args.manifest,
        component_roots=_mapping(args.component, label="component"),
        build_records=_mapping(args.build_record, label="build record"),
        check_paths=_mapping(args.check, label="qualification check"),
        qualification_evidence_path=args.qualification_evidence,
        promotion_attestation_path=args.promotion_attestation,
        promotion_trusted_signers=trust_value,
        impact_policy=_json(ROOT / "deployment/governance/change-impact-policy.json"),
        metadata=metadata_value,
        change_summary=_json(args.change_summary),
        documentation_root=ROOT,
        documentation_manifest_path=ROOT / "deployment/documentation-manifest.json",
        documentation_policy_path=ROOT / "deployment/documentation-policy.json",
        private_key_path=args.private_key,
        output=args.output,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        created_at=args.created_at,
        registry=REGISTRY,
    )
    publish_assets = args.output / "publication-assets"
    publish_assets.mkdir(mode=0o700)
    for name, path in sorted(paths.items()):
        target = publish_assets / name
        try:
            target.hardlink_to(path)
        except OSError:
            shutil.copyfile(path, target)
    asset_map = {name: name for name in sorted(paths)}
    write_canonical(
        args.asset_map,
        {"schema": "iii.qualified-release-assets/v1", "assets": asset_map},
    )
    return {"release_id": manifest_value["release_id"], "assets": len(paths)}


def publish(args: argparse.Namespace) -> dict[str, Any]:
    if args.asset_directory.is_symlink() or not args.asset_directory.is_dir():
        raise ContractError("qualified release asset directory is missing or unsafe")
    entries = sorted(args.asset_directory.iterdir())
    if not entries or any(path.is_symlink() or not path.is_file() for path in entries):
        raise ContractError("qualified release asset directory contains unsafe entries")
    trust = load_trusted_signers(args.bundle_trust, REGISTRY)
    outcome = publish_qualified_release(
        GhReleasePublisher(args.repository),
        version=args.version,
        source_commit=args.source_commit,
        asset_paths={path.name: path for path in entries},
        bundle_trust=trust,
        registry=REGISTRY,
    )
    return {"publication": outcome, "version": args.version}


def failure_record(args: argparse.Namespace) -> dict[str, Any]:
    record = create_qualification_attempt(
        version=args.version,
        source_commit=args.source_commit,
        recorded_at=args.recorded_at,
        failure_stage=args.failure_stage,
        findings=({"id": args.finding_id, "detail": args.detail},),
        log_sha256=hashlib.sha256(args.log.read_bytes()).hexdigest(),
        registry=REGISTRY,
    )
    write_canonical(args.output, record)
    return {"attempt_id": record["attempt_id"], "attempt": str(args.output)}


def publish_failure(args: argparse.Namespace) -> dict[str, Any]:
    outcome = publish_failed_attempt(
        GhReleasePublisher(args.repository),
        attempt_path=args.attempt,
        log_path=args.log,
        registry=REGISTRY,
    )
    return {"publication": outcome, "attempt": str(args.attempt)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("eligibility")
    item.add_argument("--version", required=True)
    item.add_argument("--event-ref", required=True)
    item.add_argument("--release-ref", default="refs/remotes/origin/release")
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=eligibility)

    item = sub.add_parser("fetch-promotion")
    item.add_argument("--repository", required=True)
    item.add_argument("--source-commit", required=True)
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=fetch_promotion)

    item = sub.add_parser("promotion-check")
    item.add_argument("--attestation", type=Path, required=True)
    item.add_argument("--source-snapshot", type=Path, required=True)
    item.add_argument("--change-summary", type=Path, required=True)
    item.add_argument("--trusted-signers", type=Path, required=True)
    item.set_defaults(handler=promotion_check)

    item = sub.add_parser("check-record")
    item.add_argument("--check-id", required=True)
    item.add_argument("--source-commit", required=True)
    item.add_argument("--version", required=True)
    item.add_argument("--started-at", required=True)
    item.add_argument("--finished-at", required=True)
    item.add_argument("--command-json", required=True)
    item.add_argument("--log", type=Path, required=True)
    item.add_argument("--output-file", action="append")
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=check_record)

    item = sub.add_parser("evidence")
    item.add_argument("--version", required=True)
    item.add_argument("--source-commit", required=True)
    item.add_argument("--dependency-lock", type=Path, required=True)
    item.add_argument("--check", action="append", required=True)
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=evidence)

    item = sub.add_parser("manifest")
    item.add_argument("--version", required=True)
    item.add_argument("--source-snapshot", type=Path, required=True)
    item.add_argument("--provenance", type=Path, required=True)
    item.add_argument("--qualification-evidence", type=Path, required=True)
    item.add_argument(
        "--metadata", type=Path, default=ROOT / "deployment/release-metadata.json"
    )
    item.add_argument(
        "--target-definition",
        type=Path,
        default=ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json",
    )
    item.add_argument(
        "--policy", type=Path, default=ROOT / "deployment/operational-policy.json"
    )
    item.add_argument("--component", action="append", required=True)
    item.add_argument("--build-record", action="append", required=True)
    item.add_argument("--private-key", type=Path, required=True)
    item.add_argument("--builder-id", required=True)
    item.add_argument("--built-at", required=True)
    item.add_argument("--source-date-epoch", type=int, required=True)
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=manifest)

    item = sub.add_parser("sign")
    item.add_argument("--manifest", type=Path, required=True)
    item.add_argument(
        "--metadata", type=Path, default=ROOT / "deployment/release-metadata.json"
    )
    item.add_argument("--component", action="append", required=True)
    item.add_argument("--build-record", action="append", required=True)
    item.add_argument("--check", action="append", required=True)
    item.add_argument("--qualification-evidence", type=Path, required=True)
    item.add_argument("--promotion-attestation", type=Path, required=True)
    item.add_argument("--promotion-trusted-signers", type=Path, required=True)
    item.add_argument("--change-summary", type=Path, required=True)
    item.add_argument("--private-key", type=Path, required=True)
    item.add_argument("--output", type=Path, required=True)
    item.add_argument("--asset-map", type=Path, required=True)
    item.add_argument("--repository", required=True)
    item.add_argument("--run-id", required=True)
    item.add_argument("--run-attempt", type=int, required=True)
    item.add_argument("--created-at", required=True)
    item.set_defaults(handler=sign_release)

    item = sub.add_parser("publish")
    item.add_argument("--version", required=True)
    item.add_argument("--source-commit", required=True)
    item.add_argument("--asset-directory", type=Path, required=True)
    item.add_argument("--bundle-trust", type=Path, required=True)
    item.add_argument("--repository", required=True)
    item.set_defaults(handler=publish)

    item = sub.add_parser("failure-record")
    item.add_argument("--version", required=True)
    item.add_argument("--source-commit", required=True)
    item.add_argument("--recorded-at", default=_now())
    item.add_argument("--failure-stage", required=True)
    item.add_argument("--finding-id", required=True)
    item.add_argument("--detail", required=True)
    item.add_argument("--log", type=Path, required=True)
    item.add_argument("--output", type=Path, required=True)
    item.set_defaults(handler=failure_record)

    item = sub.add_parser("publish-failure")
    item.add_argument("--attempt", type=Path, required=True)
    item.add_argument("--log", type=Path, required=True)
    item.add_argument("--repository", required=True)
    item.set_defaults(handler=publish_failure)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
        print(
            json.dumps(
                {
                    "schema": "iii.qualified-release-pipeline-result/v1",
                    "outcome": "passed",
                    **result,
                },
                sort_keys=True,
            )
        )
        return 0
    except (ContractError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema": "iii.qualified-release-pipeline-result/v1",
                    "outcome": "rejected",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
