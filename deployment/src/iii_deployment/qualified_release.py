"""Qualified-release attempt, notes, publication, and immutable asset contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shlex
from typing import Any, Mapping, Sequence

from .bundle import VerifiedBundle
from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .governance import validate_attestation_binding, verify_attestation
from .qualification import REQUIRED_QUALIFICATION_CHECKS
from .signers import load_private_key, signer_id_for_public_key, trusted_public_key, verify


PUBLICATION_NAME = "release-publication.json"
NOTES_NAME = "release-notes.json"
QUALIFICATION_NAME = "qualification-evidence.json"
PROMOTION_NAME = "promotion-attestation.json"
RECORD_NAME = "release-record.json"
PUBLICATION_DOMAIN = b"iii.qualified-release-publication/v1\0"


def _document_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_qualification_attempt(
    *,
    version: str,
    source_commit: str,
    recorded_at: str,
    failure_stage: str,
    findings: Sequence[Mapping[str, str]],
    log_sha256: str,
    registry: ContractRegistry,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "1",
        "attempt_type": "iii.qualification-attempt",
        "attempt_id": "0" * 64,
        "version": version,
        "source_commit": source_commit,
        "tag_ref": f"refs/tags/{version}",
        "outcome": "failed",
        "recorded_at": recorded_at,
        "failure_stage": failure_stage,
        "findings": [dict(finding) for finding in findings],
        "log_sha256": log_sha256,
    }
    value["attempt_id"] = content_identity(
        {key: item for key, item in value.items() if key != "attempt_id"}
    )
    registry.validate("qualification-attempt", value)
    return value


def verify_qualification_attempt(
    attempt: Mapping[str, Any], registry: ContractRegistry
) -> None:
    registry.validate("qualification-attempt", attempt)
    expected = content_identity(
        {key: item for key, item in attempt.items() if key != "attempt_id"}
    )
    if attempt["attempt_id"] != expected:
        raise ContractError("qualification attempt identity mismatch")


def create_release_record(
    *,
    version: str,
    release_id: str,
    source_commit: str,
    repository: str,
    run_id: str,
    run_attempt: int,
    build_inputs: Mapping[str, str],
    check_paths: Mapping[str, Path],
    artifact_paths: Mapping[str, Path],
    signer_id: str,
    created_at: str,
    registry: ContractRegistry,
) -> dict[str, Any]:
    def retained(name: str, path: Path) -> dict[str, Any]:
        return {
            "name": name,
            "sha256": _document_hash(path),
            "bytes": path.stat().st_size,
        }

    checks = [retained(name, check_paths[name]) for name in sorted(check_paths)]
    artifacts = [retained(name, artifact_paths[name]) for name in sorted(artifact_paths)]
    value: dict[str, Any] = {
        "schema_version": "1",
        "record_type": "iii.qualified-release-record",
        "record_id": "0" * 64,
        "version": version,
        "release_id": release_id,
        "source_commit": source_commit,
        "workflow": {
            "repository": repository,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "ref": f"refs/tags/{version}",
        },
        "build_inputs": dict(sorted(build_inputs.items())),
        "checks": checks,
        "artifacts": artifacts,
        "signer_id": signer_id,
        "created_at": created_at,
    }
    value["record_id"] = content_identity(
        {key: item for key, item in value.items() if key != "record_id"}
    )
    registry.validate("release-record", value)
    return value


def verify_release_record(record: Mapping[str, Any], registry: ContractRegistry) -> None:
    registry.validate("release-record", record)
    expected = content_identity(
        {key: item for key, item in record.items() if key != "record_id"}
    )
    if record["record_id"] != expected:
        raise ContractError("release record identity mismatch")


def _commands_markdown(commands: Sequence[Sequence[str]]) -> list[str]:
    return [f"- `{shlex.join(tuple(command))}`" for command in commands]


def create_release_notes(
    manifest: Mapping[str, Any],
    qualification_evidence: Mapping[str, Any],
    promotion_attestation: Mapping[str, Any],
    *,
    change_summary: Mapping[str, Any],
    operator_changes: Sequence[str],
    expected_downtime_s: int,
    pre_deploy_commands: Sequence[Sequence[str]],
    post_deploy_commands: Sequence[Sequence[str]],
    annotation: str | None,
    registry: ContractRegistry,
) -> dict[str, Any]:
    registry.validate("release-change-summary", change_summary)
    if (
        change_summary["version"] != manifest["version"]
        or change_summary["source_commit"] != manifest["source"]["workspace_commit"]
    ):
        raise ContractError("release change summary differs from release candidate")
    evidence = [dict(item) for item in qualification_evidence["required_checks"]]
    waivers = [dict(item) for item in promotion_attestation.get("waivers", [])]
    lines = [
        f"# {manifest['version']} deployment notes",
        "",
        f"Release ID: `{manifest['release_id']}`",
        f"Components: {', '.join(manifest['components'])}",
        f"Expected deployment downtime: {expected_downtime_s} seconds",
        "",
        "## Operator-visible changes",
        "",
        *([f"- {item}" for item in operator_changes] or ["- No operator-visible change declared by the verified impact graph."]),
        "",
        "## Machine-derived component and contract changes",
        "",
        *[
            f"- {category.replace('_', ' ')}: "
            + (", ".join(paths) if paths else "no changed paths")
            for category, paths in change_summary["categories"].items()
        ],
        "",
        "## Compatibility",
        "",
        f"- API ranges: `{json.dumps(manifest['compatibility']['api_ranges'], sort_keys=True)}`",
        f"- Schema ranges: `{json.dumps(manifest['compatibility']['schema_ranges'], sort_keys=True)}`",
        f"- PX4 firmware: `{manifest['px4']['firmware_range']}`",
        f"- QGC versions: `{', '.join(manifest['qgc']['compatible_versions'])}`",
        "",
        "## Evidence and waivers",
        "",
        *[f"- {item['id']}: {item['status']} (`{item['evidence_sha256']}`)" for item in evidence],
        *([f"- WAIVER {item['category']}: {item['rationale']}" for item in waivers] or ["- No waivers."]),
        "",
        "## Before deployment",
        "",
        *_commands_markdown(pre_deploy_commands),
        "",
        "## After deployment",
        "",
        *_commands_markdown(post_deploy_commands),
    ]
    if annotation:
        lines.extend(["", "## Maintainer annotation", "", annotation])
    value: dict[str, Any] = {
        "schema_version": "1",
        "notes_type": "iii.release-notes",
        "notes_id": "0" * 64,
        "version": manifest["version"],
        "release_id": manifest["release_id"],
        "components": list(manifest["components"]),
        "compatibility": manifest["compatibility"],
        "profiles": manifest["profiles"],
        "px4": manifest["px4"],
        "qgc": manifest["qgc"],
        "evidence": evidence,
        "waivers": waivers,
        "change_summary": dict(change_summary),
        "operator_changes": list(operator_changes),
        "expected_downtime_s": expected_downtime_s,
        "pre_deploy_commands": [list(command) for command in pre_deploy_commands],
        "post_deploy_commands": [list(command) for command in post_deploy_commands],
        "annotation": annotation,
        "markdown": "\n".join(lines).rstrip() + "\n",
    }
    value["notes_id"] = content_identity(
        {key: item for key, item in value.items() if key != "notes_id"}
    )
    registry.validate("release-notes", value)
    return value


def verify_release_notes(notes: Mapping[str, Any], registry: ContractRegistry) -> None:
    registry.validate("release-notes", notes)
    expected = content_identity(
        {key: item for key, item in notes.items() if key != "notes_id"}
    )
    if notes["notes_id"] != expected:
        raise ContractError("release notes identity mismatch")


def component_asset_name(release_id: str, component: str, filename: str) -> str:
    if component not in {"drone", "gc"}:
        raise ContractError("unknown release component")
    if filename not in {
        "bundle.tar.zst",
        "bundle.manifest.json",
        "release-manifest.json",
        "bundle.sha256",
        "bundle.sig.json",
    }:
        raise ContractError("unknown release component asset")
    return f"{release_id}.{component}.{filename}"


def create_release_publication(
    *,
    drone: VerifiedBundle,
    gc: VerifiedBundle,
    qualification_evidence_path: Path,
    promotion_attestation_path: Path,
    release_notes_path: Path,
    release_record_path: Path,
    promotion_trusted_signers: Mapping[str, str],
    impact_policy: Mapping[str, Any],
    private_key_path: Path,
    created_at: str,
    registry: ContractRegistry,
) -> dict[str, Any]:
    if drone.bundle_manifest["component"] != "drone" or gc.bundle_manifest["component"] != "gc":
        raise ContractError("qualified publication requires one drone and one GC component")
    if drone.release_manifest != gc.release_manifest:
        raise ContractError("paired publication release manifests disagree")
    if drone.bundle_manifest["component_payloads"] != gc.bundle_manifest["component_payloads"]:
        raise ContractError("paired publication payload identities disagree")
    if drone.bundle_manifest["compatibility_sha256"] != gc.bundle_manifest["compatibility_sha256"]:
        raise ContractError("paired publication compatibility identities disagree")
    release = drone.release_manifest
    if release["release_class"] != "qualified" or release["version"] is None:
        raise ContractError("only qualified SemVer releases can be published")
    qualification = json.loads(qualification_evidence_path.read_text(encoding="utf-8"))
    promotion = json.loads(promotion_attestation_path.read_text(encoding="utf-8"))
    notes = json.loads(release_notes_path.read_text(encoding="utf-8"))
    record = json.loads(release_record_path.read_text(encoding="utf-8"))
    registry.validate("qualification-evidence", qualification)
    registry.validate("promotion-evidence", promotion)
    verify_attestation(
        promotion,
        registry=registry,
        trusted_signers=promotion_trusted_signers,
    )
    validate_attestation_binding(
        promotion,
        source_identity=release["source"]["content_identity"],
        impact_policy=impact_policy,
    )
    verify_release_notes(notes, registry)
    verify_release_record(record, registry)
    if qualification["source_commit"] != release["source"]["workspace_commit"]:
        raise ContractError("qualification evidence source differs from release")
    if qualification["version"] != release["version"]:
        raise ContractError("qualification evidence version differs from release")
    retained_checks = {item["id"] for item in qualification["required_checks"]}
    missing_checks = sorted(REQUIRED_QUALIFICATION_CHECKS - retained_checks)
    if missing_checks:
        raise ContractError(
            "qualification evidence is missing required checks: "
            + ", ".join(missing_checks)
        )
    if notes["release_id"] != release["release_id"] or notes["version"] != release["version"]:
        raise ContractError("release notes identity differs from release")
    if (
        record["release_id"] != release["release_id"]
        or record["version"] != release["version"]
        or record["source_commit"] != release["source"]["workspace_commit"]
        or record["signer_id"] != release["signing"]["signer_id"]
    ):
        raise ContractError("release record identity differs from release")

    def component(value: VerifiedBundle) -> dict[str, Any]:
        return {
            "archive_sha256": value.archive_sha256,
            "bundle_manifest_sha256": _document_hash(value.paths.bundle_manifest),
            "signature_sha256": _document_hash(value.paths.signature),
            "compressed_bytes": value.compressed_bytes,
        }

    publication: dict[str, Any] = {
        "schema_version": "1",
        "publication_type": "iii.qualified-release",
        "publication_id": "0" * 64,
        "version": release["version"],
        "release_id": release["release_id"],
        "source_commit": release["source"]["workspace_commit"],
        "release_manifest_sha256": drone.bundle_manifest["release_manifest_sha256"],
        "qualification_evidence_sha256": _document_hash(qualification_evidence_path),
        "promotion_attestation_sha256": _document_hash(promotion_attestation_path),
        "release_notes_sha256": _document_hash(release_notes_path),
        "release_record_sha256": _document_hash(release_record_path),
        "components": {"drone": component(drone), "gc": component(gc)},
        "signer_id": release["signing"]["signer_id"],
        "signature_algorithm": "Ed25519",
        "created_at": created_at,
    }
    key = load_private_key(private_key_path)
    if signer_id_for_public_key(key.public_key()) != publication["signer_id"]:
        raise ContractError("publication signer differs from release signer")
    publication["publication_id"] = content_identity(
        {key: item for key, item in publication.items() if key != "publication_id"}
    )
    publication["signature"] = base64.b64encode(
        key.sign(
            PUBLICATION_DOMAIN
            + canonical_json(
                {key: item for key, item in publication.items() if key != "signature"}
            )
        )
    ).decode("ascii")
    registry.validate("release-publication", publication)
    return publication


def verify_release_publication(
    publication: Mapping[str, Any],
    trusted_signers: Mapping[str, Any],
    registry: ContractRegistry,
) -> None:
    registry.validate("release-publication", publication)
    expected = content_identity(
        {
            key: item
            for key, item in publication.items()
            if key not in {"publication_id", "signature"}
        }
    )
    if publication["publication_id"] != expected:
        raise ContractError("qualified publication identity mismatch")
    public = trusted_public_key(
        trusted_signers, publication["signer_id"], "ci-qualified"
    )
    verify(
        public,
        publication["signature"],
        PUBLICATION_DOMAIN
        + canonical_json(
            {key: item for key, item in publication.items() if key != "signature"}
        ),
    )


def compare_immutable_assets(
    expected: Mapping[str, str], existing: Mapping[str, str] | None
) -> str:
    """Return create/no-op, refusing a version whose immutable bytes differ."""
    if existing is None:
        return "create"
    if dict(existing) == dict(expected):
        return "no-op"
    missing = sorted(set(expected) - set(existing))
    extra = sorted(set(existing) - set(expected))
    changed = sorted(
        key for key in set(expected) & set(existing) if expected[key] != existing[key]
    )
    raise ContractError(
        "qualified version already exists with different immutable assets"
        f"; missing={missing}; extra={extra}; changed={changed}"
    )


def write_canonical(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(canonical_json(value) + b"\n")
