"""Pure qualified-release pipeline assembly from retained, pinned inputs."""

from __future__ import annotations

from datetime import datetime
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives import serialization

from .bundle import load_bundle_limits, package_bundle_set, verify_bundle
from .contracts import (
    ContractError,
    ContractRegistry,
    SEMVER,
    canonical_json,
    content_identity,
)
from .qualified_release import (
    NOTES_NAME,
    PROMOTION_NAME,
    PUBLICATION_NAME,
    QUALIFICATION_NAME,
    RECORD_NAME,
    component_asset_name,
    create_release_notes,
    create_release_publication,
    create_release_record,
    write_canonical,
)
from .qualification import REQUIRED_QUALIFICATION_CHECKS
from .mission_catalog import verify_mission_catalog
from .signers import load_private_key, signer_id_for_public_key
from .source import verify_source_snapshot
from .target import load_target_definition


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, *, canonical: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    if canonical and raw != canonical_json(value) + b"\n":
        raise ContractError(f"{path} is not canonical JSON")
    return value


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid UTC timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContractError("pipeline timestamps must include a UTC offset")


def create_qualification_check(
    *,
    check_id: str,
    source_commit: str,
    version: str,
    started_at: str,
    finished_at: str,
    command: Sequence[str],
    log_path: Path,
    outputs: Mapping[str, Path],
    registry: ContractRegistry,
) -> dict[str, Any]:
    _validate_timestamp(started_at)
    _validate_timestamp(finished_at)
    if not command or any(not item for item in command):
        raise ContractError("qualification check command must be non-empty argv")
    if not log_path.is_file() or log_path.is_symlink():
        raise ContractError("qualification check log is missing or unsafe")
    retained = []
    for name in sorted(outputs):
        path = outputs[name]
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"qualification output is missing or unsafe: {name}")
        retained.append(
            {"name": name, "sha256": _sha256(path), "bytes": path.stat().st_size}
        )
    value = {
        "schema_version": "1",
        "check_type": "iii.qualification-check",
        "check_id": check_id,
        "source_commit": source_commit,
        "version": version,
        "outcome": "passed",
        "started_at": started_at,
        "finished_at": finished_at,
        "command": list(command),
        "log_sha256": _sha256(log_path),
        "outputs": retained,
    }
    registry.validate("qualification-check", value)
    return value


def assemble_qualification_evidence(
    *,
    version: str,
    source_commit: str,
    dependency_lock_path: Path,
    check_paths: Mapping[str, Path],
    registry: ContractRegistry,
) -> dict[str, Any]:
    if not SEMVER.fullmatch(version):
        raise ContractError("qualification version is not strict SemVer")
    observed: dict[str, dict[str, Any]] = {}
    for declared_id, path in sorted(check_paths.items()):
        check = _json(path, canonical=True)
        registry.validate("qualification-check", check)
        if check["check_id"] != declared_id:
            raise ContractError(
                f"qualification check filename binding differs for {declared_id}"
            )
        if check["source_commit"] != source_commit or check["version"] != version:
            raise ContractError(
                f"qualification check {declared_id} is bound to another source/version"
            )
        if declared_id in observed:
            raise ContractError(f"duplicate qualification check {declared_id}")
        observed[declared_id] = check
    missing = sorted(REQUIRED_QUALIFICATION_CHECKS - set(observed))
    if missing:
        raise ContractError(
            "qualification checks are incomplete: " + ", ".join(missing)
        )
    value = {
        "schema_version": "1",
        "schema": "iii.qualification-evidence/v1",
        "evidence_type": "qualified-release-preflight",
        "source_commit": source_commit,
        "version": version,
        "dependency_lock_sha256": _sha256(dependency_lock_path),
        "governance_verified": True,
        "required_checks": [
            {
                "id": identifier,
                "status": "passed",
                "evidence_sha256": _sha256(check_paths[identifier]),
            }
            for identifier in sorted(observed)
        ],
        "evidence_complete": True,
    }
    registry.validate("qualification-evidence", value)
    return value


def _hash_inputs(root: Path, paths: Sequence[str]) -> str:
    entries: list[tuple[str, str, str | None]] = []
    root = root.resolve()
    for raw in sorted(set(paths)):
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ContractError(f"release metadata input path is unsafe: {raw}")
        candidate = root.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.exists():
            raise ContractError(f"release metadata input is missing or linked: {raw}")
        selected = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in selected:
            if path.is_symlink():
                raise ContractError(
                    f"release metadata input contains a symlink: {path}"
                )
            name = path.relative_to(root).as_posix()
            if path.is_dir():
                entries.append((name, "directory", None))
            elif path.is_file():
                entries.append((name, "file", _sha256(path)))
            else:
                raise ContractError(
                    f"release metadata input contains a special file: {path}"
                )
    if not entries:
        raise ContractError("release metadata input set is empty")
    return content_identity(entries)


def _tree_inventory(root: Path, prefix: str) -> tuple[str, list[tuple[str, str]]]:
    if root.is_symlink() or not root.is_dir():
        raise ContractError(f"release payload is missing or unsafe: {root}")
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"release payload contains a symlink: {path}")
        if path.is_file():
            name = f"{prefix}/{path.relative_to(root).as_posix()}"
            entries.append((name, _sha256(path)))
        elif not path.is_dir():
            raise ContractError(f"release payload contains a special file: {path}")
    if not entries:
        raise ContractError(f"release payload is empty: {root}")
    return content_identity(entries), entries


def _build_tree_identity(root: Path) -> str:
    """Recompute the ARM builder's recorded install-tree identity."""

    entries: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", _sha256(path)))
        elif not path.is_dir():
            raise ContractError(f"build payload contains a special file: {path}")
    return content_identity(entries)


def _validate_build_records(
    *,
    root: Path,
    version: str,
    snapshot: Mapping[str, Any],
    target: Mapping[str, Any],
    component_roots: Mapping[str, Path],
    build_records: Mapping[str, Path],
    qualification_evidence: Mapping[str, Any],
    registry: ContractRegistry,
) -> dict[str, dict[str, Any]]:
    drone = _json(build_records["drone"], canonical=True)
    gc = _json(build_records["gc"], canonical=True)
    registry.validate("build-record", drone)
    registry.validate("gc-build-record", gc)
    if drone["build_id"] != content_identity(
        {key: value for key, value in drone.items() if key != "build_id"}
    ):
        raise ContractError("ARM64 build-record identity mismatch")
    if gc["build_id"] != content_identity(
        {key: value for key, value in gc.items() if key != "build_id"}
    ):
        raise ContractError("GC build-record identity mismatch")
    source_identity = snapshot["content_identity"]
    if (
        drone["source_identity"] != source_identity
        or gc["source_identity"] != source_identity
    ):
        raise ContractError("build records differ from the qualified source snapshot")
    if drone["components"] != ["drone"]:
        raise ContractError(
            "qualified ARM64 build record must contain only the drone component"
        )
    if drone["target_definition_id"] != target["definition_id"]:
        raise ContractError(
            "ARM64 build record differs from the qualified target definition"
        )
    build_policy = _json(root / "deployment/build-policy.json")
    registry.validate("build-policy", build_policy)
    if drone["policy_sha256"] != content_identity(build_policy):
        raise ContractError(
            "ARM64 build record differs from the qualified build policy"
        )
    install = component_roots["drone"] / build_policy["release_install"]
    if not install.is_dir() or _build_tree_identity(install) != drone["install_sha256"]:
        raise ContractError("ARM64 payload differs from its retained build record")
    if (
        gc["source_commit"] != snapshot["workspace_commit"]
        or gc["version"] != version
        or gc["platform"] != {"os": "linux", "architecture": "amd64"}
    ):
        raise ContractError("GC build record differs from the qualified candidate")
    input_hashes = {
        path: _sha256(root / path)
        for path in (
            "deployment/gc-application-policy.json",
            "deployment/gc/compose.release.yml",
            "deployment/qgc/key-policy.json",
            "deployment/qgc/managed-settings.json",
            "src/III-Drone-GC/docker/proxy.Dockerfile",
            "src/III-Drone-GC/docker/proxy-requirements.lock",
            "src/III-Drone-GC/frontend/Dockerfile",
            "src/III-Drone-GC/frontend/package-lock.json",
        )
    }
    if gc["inputs_sha256"] != content_identity(input_hashes):
        raise ContractError("GC build record differs from the pinned GC build inputs")
    evidence_hashes = {
        item["id"]: item["evidence_sha256"]
        for item in qualification_evidence["required_checks"]
    }
    if gc["test_record_sha256"] != evidence_hashes.get("gc-tests"):
        raise ContractError(
            "GC build record differs from the retained GC test evidence"
        )
    if {image["name"] for image in gc["images"]} != {"frontend", "proxy"}:
        raise ContractError(
            "GC build record must contain exactly the frontend and proxy images"
        )
    for image in gc["images"]:
        archive = component_roots["gc"] / "images" / image["archive"]
        if (
            archive.is_symlink()
            or not archive.is_file()
            or archive.stat().st_size != image["bytes"]
            or _sha256(archive) != image["sha256"]
        ):
            raise ContractError(
                f"GC {image['name']} archive differs from its build record"
            )
    qgc = gc["qgroundcontrol"]
    qgc_path = component_roots["gc"] / "qgc" / qgc["appimage"]
    if (
        qgc_path.is_symlink()
        or not qgc_path.is_file()
        or qgc_path.stat().st_size != qgc["bytes"]
        or _sha256(qgc_path) != qgc["sha256"]
        or qgc["appimage_update_information"] != ""
        or qgc["update_owner"] != "iii-gc-release"
    ):
        raise ContractError("QGroundControl AppImage differs from its GC build record")
    configuration = qgc["configuration"]
    for name, hash_name in (
        ("policy", "policy_sha256"),
        ("baseline", "baseline_sha256"),
    ):
        path = component_roots["gc"] / configuration[name]
        if (
            path.is_symlink()
            or not path.is_file()
            or _sha256(path) != configuration[hash_name]
        ):
            raise ContractError(
                "QGroundControl release configuration differs from its build record"
            )
    key_policy = _json(component_roots["gc"] / configuration["policy"], canonical=True)
    baseline = _json(component_roots["gc"] / configuration["baseline"], canonical=True)
    registry.validate("qgc-key-policy", key_policy)
    registry.validate("qgc-managed-settings", baseline)
    if (
        key_policy["policy_id"] != configuration["policy_id"]
        or baseline["settings_id"] != configuration["settings_id"]
        or baseline["policy_id"] != configuration["policy_id"]
    ):
        raise ContractError("QGroundControl release configuration identity differs")
    compose = component_roots["gc"] / gc["application"]["compose"]
    if (
        compose.is_symlink()
        or not compose.is_file()
        or _sha256(compose) != gc["application"]["compose_sha256"]
    ):
        raise ContractError("GC release compose contract differs from its build record")
    return {"drone": drone, "gc": gc}


def _source_manifest(
    snapshot: Mapping[str, Any],
    snapshot_path: Path,
    provenance_path: Path,
    source_content_identity: str,
) -> dict[str, Any]:
    workspace = snapshot["repositories"][0]
    if workspace["path"] != "." or not snapshot["clean"]:
        raise ContractError("qualified release source snapshot is not clean")
    dirty = [
        item["path"] for item in snapshot["repositories"] if item["state"] != "clean"
    ]
    if dirty:
        raise ContractError(
            "qualified release has dirty governed repositories: " + ", ".join(dirty)
        )
    return {
        "workspace_commit": snapshot["workspace_commit"],
        "branch": "release",
        "clean": True,
        # Promotion evidence deliberately survives mechanical merge commits and
        # lock-only refreshes, while the snapshot identity below binds every
        # exact repository commit and dirty-state assertion used by the build.
        "content_identity": source_content_identity,
        "snapshot_content_identity": snapshot["content_identity"],
        "snapshot_sha256": _sha256(snapshot_path),
        "provenance_report_sha256": _sha256(provenance_path),
        "tracked_patch_sha256": workspace["tracked_patch_sha256"],
        "untracked": workspace["untracked"],
        "submodules": [
            {
                "path": item["path"],
                "commit": item["commit"],
                "state": item["state"],
                "content_identity": item["content_identity"],
            }
            for item in snapshot["repositories"][1:]
        ],
    }


def assemble_release_manifest(
    *,
    root: Path,
    version: str,
    source_snapshot_path: Path,
    provenance_path: Path,
    qualification_evidence_path: Path,
    metadata_path: Path,
    target_definition_path: Path,
    operational_policy_path: Path,
    component_roots: Mapping[str, Path],
    build_records: Mapping[str, Path],
    private_key_path: Path,
    builder_id: str,
    built_at: str,
    source_date_epoch: int,
    source_content_identity: str,
    registry: ContractRegistry,
) -> dict[str, Any]:
    if not SEMVER.fullmatch(version):
        raise ContractError("qualified release version is not strict SemVer")
    _validate_timestamp(built_at)
    if source_date_epoch < 0:
        raise ContractError("source-date epoch cannot be negative")
    snapshot = _json(source_snapshot_path, canonical=True)
    verify_source_snapshot(snapshot, registry)
    evidence = _json(qualification_evidence_path, canonical=True)
    registry.validate("qualification-evidence", evidence)
    if (
        evidence["source_commit"] != snapshot["workspace_commit"]
        or evidence["version"] != version
    ):
        raise ContractError("qualification evidence differs from release candidate")
    metadata = _json(metadata_path)
    registry.validate("release-metadata", metadata)
    target = load_target_definition(target_definition_path, registry)
    policy = _json(operational_policy_path)
    registry.validate("operational-policy", policy)
    if set(component_roots) != {"drone", "gc"} or set(build_records) != {"drone", "gc"}:
        raise ContractError(
            "qualified release requires exact drone and GC build inputs"
        )
    retained_builds = _validate_build_records(
        root=root,
        version=version,
        snapshot=snapshot,
        target=target,
        component_roots=component_roots,
        build_records=build_records,
        qualification_evidence=evidence,
        registry=registry,
    )
    payload_identities: dict[str, str] = {}
    payload_entries: list[tuple[str, str]] = []
    for component in ("drone", "gc"):
        payload_identities[component], entries = _tree_inventory(
            component_roots[component], component
        )
        payload_entries.extend(entries)
        if (
            build_records[component].is_symlink()
            or not build_records[component].is_file()
        ):
            raise ContractError(f"{component} build record is missing or unsafe")
    signer_id = signer_id_for_public_key(
        load_private_key(private_key_path).public_key()
    )
    inputs = metadata["input_paths"]
    for profile in ("real", "sim"):
        if len(inputs[f"px4_{profile}"]) != 1:
            raise ContractError(
                f"PX4 {profile} release input must contain exactly one manifest"
            )
    if len(inputs["px4_reference"]) != 1:
        raise ContractError(
            "PX4 release input must contain exactly one reference snapshot"
        )
    reference_path = root / inputs["px4_reference"][0]
    px4_reference = _json(reference_path, canonical=True)
    registry.validate("px4-parameter-snapshot", px4_reference)
    expected_reference_id = content_identity(
        {
            "profile": px4_reference["profile"],
            "target": px4_reference["target"],
            "parameter_count": px4_reference["parameter_count"],
            "parameters": px4_reference["parameters"],
        }
    )
    if (
        px4_reference["profile"] != "sim"
        or px4_reference["complete"] is not True
        or px4_reference["snapshot_id"] != expected_reference_id
    ):
        raise ContractError("PX4 reference snapshot is incomplete or has changed")
    px4_manifests: dict[str, dict[str, Any]] = {}
    for profile in ("real", "sim"):
        document = _json(root / inputs[f"px4_{profile}"][0], canonical=True)
        registry.validate("px4-parameter-manifest", document)
        expected_manifest_id = content_identity(
            {key: value for key, value in document.items() if key != "manifest_id"}
        )
        if (
            document["profile"] != profile
            or document["manifest_id"] != expected_manifest_id
            or document["firmware"]["reference_version"]
            != px4_reference["target"]["firmware_version"]
            or not document["firmware"]["reference_commit"].startswith(
                px4_reference["target"]["firmware_commit"]
            )
            or document["firmware"]["compatible_range"]
            != metadata["px4"]["firmware_range"]
        ):
            raise ContractError(
                f"PX4 {profile} manifest is not bound to the reference snapshot"
            )
        px4_manifests[profile] = document
    if not re.fullmatch(r"[a-f0-9]{64}", source_content_identity):
        raise ContractError("governed source-content identity is invalid")
    source = _source_manifest(
        snapshot, source_snapshot_path, provenance_path, source_content_identity
    )
    mission_catalog = verify_mission_catalog(
        component_roots["drone"]
        / _json(root / "deployment/build-policy.json")["release_install"]
        / "iii_drone_mission/share/iii_drone_mission/mission_catalog",
        expected_scope="qualified",
    )
    for profile in metadata["profiles"]:
        descriptor = mission_catalog["profiles"].get(profile["id"])
        if descriptor is None:
            raise ContractError(
                f"release profile is absent from the qualified mission catalog: {profile['id']}"
            )
        if (
            profile["status"] == "commissioned"
            and descriptor.get("default_entry_id") != profile["default_mission"]
        ):
            raise ContractError(
                f"release profile default differs from the qualified mission catalog: {profile['id']}"
            )
    manifest: dict[str, Any] = {
        "schema_version": "1",
        "manifest_type": "release",
        "release_id": "0" * 64,
        "release_class": "qualified",
        "version": version,
        "components": ["drone", "gc"],
        "component_targets": metadata["component_targets"],
        "compatibility": metadata["compatibility"],
        "source": source,
        "target": {
            "definition_id": target["definition_id"],
            "target_id": target["target"]["target_id"],
            "os": target["target"]["os"],
            "os_version": target["target"]["os_version"],
            "architecture": target["target"]["architecture"],
            "python_abi": target["target"]["python"]["abi"],
            "host_baseline": target["host_baseline"]["contract_id"],
            "host_unit_contract": target["host_baseline"]["unit_contract_id"],
            "ros": target["target"]["ros"]["distro"],
        },
        "toolchain": {
            "builder_digest": target["images"]["builder"]["platform_digest"],
            "compiler": f"{target['toolchain']['target_triple']}-g++ {target['toolchain']['compiler_version']}",
            "sysroot_sha256": target["sysroot"]["content_id"],
        },
        "dependency_lock": {
            "sha256": snapshot["dependency_lock_sha256"],
            "verified": True,
        },
        "packages": [
            {
                "name": f"iii_{component}_release",
                "version": version.removeprefix("v"),
                "content_sha256": payload_identities[component],
            }
            for component in ("drone", "gc")
        ],
        "checksums": {
            **{name: digest for name, digest in payload_entries},
            **{
                f"records/{component}.json": _sha256(build_records[component])
                for component in ("drone", "gc")
            },
        },
        "configuration": {
            **metadata["configuration"],
            "manifest_sha256": _hash_inputs(root, inputs["configuration"]),
        },
        "px4": {
            "manifests": {
                "real": _hash_inputs(root, inputs["px4_real"]),
                "sim": _hash_inputs(root, inputs["px4_sim"]),
            },
            "manifest_ids": {
                profile: px4_manifests[profile]["manifest_id"]
                for profile in ("real", "sim")
            },
            "reference_snapshot_id": px4_reference["snapshot_id"],
            "reference_snapshot_sha256": _sha256(reference_path),
            "firmware_range": metadata["px4"]["firmware_range"],
            "interface_sha256": _hash_inputs(root, inputs["px4_interface"]),
        },
        "qgc": {
            "managed_settings_sha256": _hash_inputs(
                root, inputs["qgc_managed_settings"]
            ),
            "compatible_versions": metadata["qgc"]["compatible_versions"],
            "selected_version": retained_builds["gc"]["qgroundcontrol"]["version"],
            "appimage_sha256": retained_builds["gc"]["qgroundcontrol"]["sha256"],
            "update_owner": retained_builds["gc"]["qgroundcontrol"]["update_owner"],
        },
        "profiles": metadata["profiles"],
        "mission_catalog": {
            "schema_version": metadata["mission_catalog"]["schema_version"],
            "scope": mission_catalog["scope"],
            "catalog_hash": mission_catalog["catalog_hash"],
            "catalog_sha256": mission_catalog["catalog_sha256"],
            "source_state_sha256": mission_catalog["source_state_sha256"],
        },
        "qualification": {
            "explicit_action": True,
            "tag_on_release": True,
            "tests_complete": True,
            "evidence_sha256": _sha256(qualification_evidence_path),
            "evidence_complete": True,
        },
        "signing": {
            "algorithm": "Ed25519",
            "signer_id": signer_id,
            "authority": "ci-qualified",
        },
        "operational_policy": {
            "schema_version": str(policy["schema_version"]),
            "sha256": content_identity(policy),
        },
        "build": {
            "builder_id": builder_id,
            "built_at": built_at,
            "source_date_epoch": source_date_epoch,
        },
    }
    manifest["release_id"] = content_identity(
        {key: item for key, item in manifest.items() if key != "release_id"}
    )
    registry.validate("release-manifest", manifest)
    return manifest


def assemble_signed_release(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    component_roots: Mapping[str, Path],
    build_records: Mapping[str, Path],
    check_paths: Mapping[str, Path],
    qualification_evidence_path: Path,
    promotion_attestation_path: Path,
    promotion_trusted_signers: Mapping[str, str],
    impact_policy: Mapping[str, Any],
    metadata: Mapping[str, Any],
    change_summary: Mapping[str, Any],
    private_key_path: Path,
    output: Path,
    repository: str,
    run_id: str,
    run_attempt: int,
    created_at: str,
    registry: ContractRegistry,
) -> dict[str, Path]:
    if output.exists() or output.is_symlink():
        raise ContractError("signed release output already exists")
    output.mkdir(parents=True, mode=0o700)
    try:
        write_canonical(manifest_path, manifest)
        bundle_root = output / f"{manifest['release_id']}.iii-release-v1"
        paths = package_bundle_set(
            manifest_path,
            component_roots,
            private_key_path,
            bundle_root,
            registry=registry,
            host_limits=load_bundle_limits(root / "deployment/operational-policy.json"),
        )
        notes = create_release_notes(
            manifest,
            _json(qualification_evidence_path, canonical=True),
            _json(promotion_attestation_path, canonical=True),
            change_summary=change_summary,
            operator_changes=change_summary["operator_changes"],
            expected_downtime_s=metadata["operator"]["expected_downtime_s"],
            pre_deploy_commands=metadata["operator"]["pre_deploy_commands"],
            post_deploy_commands=metadata["operator"]["post_deploy_commands"],
            annotation=None,
            registry=registry,
        )
        notes_path = output / NOTES_NAME
        write_canonical(notes_path, notes)
        named_bundle_paths = {
            component_asset_name(manifest["release_id"], component, path.name): path
            for component, value in paths.items()
            for path in sorted(value.directory.iterdir(), key=lambda item: item.name)
        }
        artifact_paths = {
            QUALIFICATION_NAME: qualification_evidence_path,
            PROMOTION_NAME: promotion_attestation_path,
            NOTES_NAME: notes_path,
            **named_bundle_paths,
        }
        key = load_private_key(private_key_path)
        record = create_release_record(
            version=manifest["version"],
            release_id=manifest["release_id"],
            source_commit=manifest["source"]["workspace_commit"],
            repository=repository,
            run_id=run_id,
            run_attempt=run_attempt,
            build_inputs={
                "manifest": hashlib.sha256(
                    canonical_json(manifest) + b"\n"
                ).hexdigest(),
                **{
                    f"{component}_build_record": _sha256(path)
                    for component, path in build_records.items()
                },
            },
            check_paths=check_paths,
            artifact_paths=artifact_paths,
            signer_id=signer_id_for_public_key(key.public_key()),
            created_at=created_at,
            registry=registry,
        )
        record_path = output / RECORD_NAME
        write_canonical(record_path, record)
        public = key.public_key()
        signer_id = signer_id_for_public_key(public)
        trust = {
            "schema_version": "1",
            "store_type": "iii.trusted-signers",
            "signers": [
                {
                    "signer_id": signer_id,
                    "authority": "ci-qualified",
                    "algorithm": "Ed25519",
                    "public_key": base64.b64encode(
                        public.public_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw,
                        )
                    ).decode("ascii"),
                    "state": "active",
                }
            ],
        }
        # Bundle verification is performed with the caller-provisioned trust in
        # the workflow; inspection here supplies the verified structural inputs
        # needed for the signed publication without inventing another trust root.
        from .bundle import inspect_bundle

        verified = {
            component: inspect_bundle(
                value.directory,
                trust,
                registry=registry,
                host_limits=load_bundle_limits(
                    root / "deployment/operational-policy.json"
                ),
            )
            for component, value in paths.items()
        }
        publication = create_release_publication(
            drone=verified["drone"],
            gc=verified["gc"],
            qualification_evidence_path=qualification_evidence_path,
            promotion_attestation_path=promotion_attestation_path,
            release_notes_path=notes_path,
            release_record_path=record_path,
            promotion_trusted_signers=promotion_trusted_signers,
            impact_policy=impact_policy,
            private_key_path=private_key_path,
            created_at=created_at,
            registry=registry,
        )
        publication_path = output / PUBLICATION_NAME
        write_canonical(publication_path, publication)
        return {
            PUBLICATION_NAME: publication_path,
            RECORD_NAME: record_path,
            **artifact_paths,
        }
    except Exception:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        raise


def git_change_summary(
    root: Path, version: str, registry: ContractRegistry
) -> dict[str, Any]:
    if not SEMVER.fullmatch(version):
        raise ContractError("change summary version is not strict SemVer")
    tags = subprocess.run(
        ["git", "tag", "--merged", "HEAD", "--list", "v*", "--sort=-version:refname"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tags.returncode:
        raise ContractError(
            tags.stderr.strip() or "cannot enumerate prior release tags"
        )
    candidates = [
        tag
        for tag in tags.stdout.splitlines()
        if SEMVER.fullmatch(tag) and tag != version
    ]
    base_version = candidates[0] if candidates else None
    base = (
        base_version
        or subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", f"{base}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    log = subprocess.run(
        ["git", "log", "--format=%H%x09%s", f"{base_commit}..{source_commit}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    commits = [
        {"sha": line.split("\t", 1)[0], "subject": line.split("\t", 1)[1]}
        for line in log
        if "\t" in line
    ]
    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{base_commit}..{source_commit}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    patterns = {
        "drone": (
            "src/III-Drone-Core/",
            "src/III-Drone-Configuration/",
            "src/III-Drone-Interfaces/",
            "src/III-Drone-Mission/",
            "src/III-Drone-Runtime/",
            "src/III-Drone-Supervision/",
            "src/iwr6843aop-ROS2-pkg/",
        ),
        "gc": ("src/III-Drone-GC/", "tools/III-Drone-CLI/"),
        "missions": (
            "src/III-Drone-Mission/mission_specification/",
            "src/III-Drone-Mission/behavior_trees/",
        ),
        "configuration": ("src/III-Drone-Configuration/config/",),
        "px4": ("PX4-Autopilot/", "deployment/px4/"),
        "qgc": ("deployment/qgc/", "src/III-Drone-GC/config/"),
        "host_provisioning": ("deployment/", "ansible/", "setup/", ".github/"),
        "documentation": ("docs/", "README.md", "AGENTS.md"),
    }
    categories = {
        name: sorted(
            path
            for path in changed
            if any(path == prefix or path.startswith(prefix) for prefix in prefixes)
        )
        for name, prefixes in patterns.items()
    }
    changes = [f"{item['sha'][:12]} {item['subject']}" for item in commits]
    if not changes:
        changes = [
            "No commits after the baseline tag; the root baseline itself is being qualified."
        ]
    value = {
        "schema_version": "1",
        "summary_type": "iii.release-change-summary",
        "version": version,
        "base_version": base_version,
        "base_commit": base_commit,
        "source_commit": source_commit,
        "commits": commits,
        "changed_paths": sorted(set(changed)),
        "categories": categories,
        "operator_changes": changes,
    }
    registry.validate("release-change-summary", value)
    return value
