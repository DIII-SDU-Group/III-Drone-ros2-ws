from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest
import zstandard

from iii_deployment.bundle import (
    ARCHIVE_NAME,
    BundlePaths,
    _stream_archive,
    _signature_message,
    extract_bundle,
    inspect_bundle,
    load_bundle_limits,
    package_bundle_set,
    validate_release_metadata,
    verify_bundle,
)
from iii_deployment.contracts import ContractError, ContractRegistry, canonical_json
from iii_deployment.signers import (
    add_trusted_signer,
    generate_signer,
    load_private_key,
    load_trusted_signers,
    revoke_trusted_signer,
    signer_proof,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment" / "schemas" / "v1")
LIMITS = load_bundle_limits(ROOT / "deployment" / "operational-policy.json")
FIXTURE = ROOT / "deployment" / "tests" / "fixtures" / "release_manifest.json"


@dataclass
class BundleCase:
    key: Path
    public: Path
    store: Path
    manifest: dict
    manifest_path: Path
    roots: dict[str, Path]
    output: Path
    paths: dict[str, BundlePaths]


def _case(tmp_path: Path, *, release_class: str = "qualified") -> BundleCase:
    key = tmp_path / "keys" / "release.pem"
    public = tmp_path / "keys" / "release.public.json"
    authority = "ci-qualified" if release_class == "qualified" else "workstation-field"
    descriptor = generate_signer(key, public, authority=authority, registry=REGISTRY)
    store = tmp_path / "trust" / "trusted.json"
    add_trusted_signer(store, public, signer_proof(key), REGISTRY)
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest["release_class"] = release_class
    manifest["signing"] = {
        "algorithm": "Ed25519",
        "signer_id": descriptor["signer_id"],
        "authority": authority,
    }
    if release_class == "field-development":
        manifest["version"] = None
        manifest["source"]["branch"] = "deployment-infrastructure-redesign"
        manifest["qualification"].update(
            explicit_action=False,
            tag_on_release=False,
            tests_complete=False,
            evidence_complete=False,
        )
    manifest_path = tmp_path / "release-input.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    roots: dict[str, Path] = {}
    for component in manifest["components"]:
        payload = tmp_path / f"{component}-payload"
        (payload / "share" / "empty").mkdir(parents=True)
        (payload / "share" / "asset.txt").write_text(component + "\n", encoding="utf-8")
        (payload / "bin").mkdir()
        executable = payload / "bin" / "iii-run"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o751)
        roots[component] = payload
    output = tmp_path / "release-set"
    paths = package_bundle_set(
        manifest_path,
        roots,
        key,
        output,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    return BundleCase(key, public, store, manifest, manifest_path, roots, output, paths)


def _write_zstd(path: Path, uncompressed: bytes) -> None:
    with path.open("wb") as raw:
        compressor = zstandard.ZstdCompressor(
            level=19, write_checksum=True, write_content_size=False, threads=0
        )
        with compressor.stream_writer(raw, closefd=False) as stream:
            stream.write(uncompressed)


def _read_zstd(path: Path) -> bytes:
    with path.open("rb") as raw:
        with zstandard.ZstdDecompressor().stream_reader(
            raw, read_across_frames=True
        ) as stream:
            return stream.read()


def _rewrite_archive(
    paths: BundlePaths,
    key: Path,
    mutate,
) -> None:
    source = io.BytesIO(_read_zstd(paths.archive))
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(fileobj=source, mode="r:") as archive:
        for member in archive.getmembers():
            stream = archive.extractfile(member) if member.isfile() else None
            members.append((member, b"" if stream is None else stream.read()))
    mutate(members)
    encoded = io.BytesIO()
    with tarfile.open(
        fileobj=encoded, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        for member, data in members:
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
    _write_zstd(paths.archive, encoded.getvalue())
    _resign_archive(paths, key)


def _resign_archive(paths: BundlePaths, key: Path) -> None:
    signature = json.loads(paths.signature.read_text(encoding="utf-8"))
    signature["archive_sha256"] = hashlib.sha256(paths.archive.read_bytes()).hexdigest()
    private = load_private_key(key)
    signature["signature"] = base64.b64encode(
        private.sign(_signature_message(signature))
    ).decode("ascii")
    paths.signature.write_bytes(canonical_json(signature) + b"\n")
    paths.checksum.write_text(
        f"{signature['archive_sha256']}  {ARCHIVE_NAME}\n", encoding="ascii"
    )


def test_paired_bundles_are_deterministic_independent_and_round_trip(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    second = tmp_path / "second-release-set"
    repeated = package_bundle_set(
        case.manifest_path,
        case.roots,
        case.key,
        second,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    shared_payloads = None
    shared_compatibility = None
    shared_catalog = None
    for component, paths in case.paths.items():
        assert paths.archive.read_bytes() == repeated[component].archive.read_bytes()
        assert (
            paths.signature.read_bytes() == repeated[component].signature.read_bytes()
        )
        verified = verify_bundle(
            paths.directory, case.store, registry=REGISTRY, host_limits=LIMITS
        )
        assert verified.compressed_bytes == paths.archive.stat().st_size
        assert verified.bundle_manifest["release_id"] == case.manifest["release_id"]
        if shared_payloads is None:
            shared_payloads = verified.bundle_manifest["component_payloads"]
            shared_compatibility = verified.bundle_manifest["compatibility_sha256"]
            shared_catalog = verified.bundle_manifest["mission_catalog_hash"]
        assert verified.bundle_manifest["component_payloads"] == shared_payloads
        assert verified.bundle_manifest["compatibility_sha256"] == shared_compatibility
        assert verified.bundle_manifest["mission_catalog_hash"] == shared_catalog
        assert shared_catalog == case.manifest["mission_catalog"]["catalog_hash"]
        destination = tmp_path / f"installed-{component}"
        extract_bundle(
            paths.directory,
            destination,
            case.store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )
        assert (
            destination / "payload" / "share" / "asset.txt"
        ).read_text() == component + "\n"
        assert (destination / "payload" / "share" / "empty").is_dir()
        assert (
            destination / "payload" / "bin" / "iii-run"
        ).stat().st_mode & 0o777 == 0o755
        assert not any(
            (destination / "payload" / name).exists()
            for name in ("src", "build", ".git")
        )


def test_qualified_and_field_releases_share_the_same_format(tmp_path: Path) -> None:
    qualified = _case(tmp_path / "qualified")
    field = _case(tmp_path / "field", release_class="field-development")
    for case in (qualified, field):
        verified = verify_bundle(
            case.paths["drone"].directory,
            case.store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )
        assert verified.bundle_manifest["format"] == {
            "name": "iii.tar.zst",
            "version": 1,
            "tar": "ustar",
            "zstd_level": 19,
            "zstd_checksum": True,
        }
    assert qualified.manifest["signing"]["authority"] == "ci-qualified"
    assert field.manifest["signing"]["authority"] == "workstation-field"


def test_release_metadata_binds_px4_qgc_and_extensible_profiles(tmp_path: Path) -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validate_release_metadata(manifest, REGISTRY)
    assert set(manifest["px4"]["manifests"]) == {"real", "sim"}
    manifest["px4"]["manifests"]["future_lab"] = "3" * 64
    manifest["profiles"].append(
        {
            "id": "future_lab",
            "status": "not_commissioned",
            "bootable": False,
            "parameter_profile": "future_lab",
            "capabilities": [],
            "default_mission": "inspection-production",
            "health": {
                "schema": "iii.activation-health-policy/v1",
                "required_hardware_roles": [],
                "optional_hardware_roles": [],
                "required_services": [],
                "optional_services": [],
                "required_managed_nodes": {},
                "optional_managed_nodes": {},
                "required_systemd_units": [
                    "iii-runtime-api.service",
                    "iii-system-daemon.service",
                ],
            },
        }
    )
    validate_release_metadata(manifest, REGISTRY)
    manifest["profiles"][-1]["bootable"] = True
    with pytest.raises(ContractError, match="fail closed"):
        validate_release_metadata(manifest, REGISTRY)


def test_qualified_release_cannot_bind_field_catalog_but_field_release_can(
    tmp_path: Path,
) -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest["mission_catalog"]["scope"] = "field"
    with pytest.raises(ContractError, match="qualified mission catalog"):
        validate_release_metadata(manifest, REGISTRY)
    manifest["release_class"] = "field-development"
    manifest["version"] = None
    manifest["signing"]["authority"] = "workstation-field"
    validate_release_metadata(manifest, REGISTRY)


def test_px4_and_qgc_changes_change_signed_compatibility_identity(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    original = inspect_bundle(
        case.paths["drone"].directory,
        case.store,
        registry=REGISTRY,
        host_limits=LIMITS,
    ).bundle_manifest["compatibility_sha256"]
    identities = {original}
    for index, mutation in enumerate(("px4", "qgc"), start=1):
        manifest = json.loads(case.manifest_path.read_text(encoding="utf-8"))
        if mutation == "px4":
            manifest["px4"]["manifests"]["real"] = "4" * 64
        else:
            manifest["qgc"]["managed_settings_sha256"] = "5" * 64
        candidate = tmp_path / f"release-{index}.json"
        candidate.write_text(json.dumps(manifest), encoding="utf-8")
        paths = package_bundle_set(
            candidate,
            case.roots,
            case.key,
            tmp_path / f"release-set-{index}",
            registry=REGISTRY,
            host_limits=LIMITS,
        )
        identities.add(
            inspect_bundle(
                paths["drone"].directory,
                case.store,
                registry=REGISTRY,
                host_limits=LIMITS,
            ).bundle_manifest["compatibility_sha256"]
        )
    assert len(identities) == 3


@pytest.mark.parametrize("condition", ["unsigned", "unknown", "invalid", "corrupt"])
def test_unsigned_unknown_invalid_and_corrupt_bundles_are_rejected(
    tmp_path: Path, condition: str
) -> None:
    case = _case(tmp_path)
    paths = case.paths["drone"]
    trust: Path | dict = case.store
    if condition == "unsigned":
        paths.signature.unlink()
    elif condition == "unknown":
        trust = {"signers": []}
    elif condition == "invalid":
        signature = json.loads(paths.signature.read_text(encoding="utf-8"))
        signature["signature"] = (
            "A" if signature["signature"][0] != "A" else "B"
        ) + signature["signature"][1:]
        paths.signature.write_bytes(canonical_json(signature) + b"\n")
    else:
        data = bytearray(paths.archive.read_bytes())
        data[len(data) // 2] ^= 1
        paths.archive.write_bytes(data)
    with pytest.raises(ContractError):
        verify_bundle(paths.directory, trust, registry=REGISTRY, host_limits=LIMITS)


def test_signed_archive_content_disagreement_is_rejected_and_staging_removed(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    paths = case.paths["drone"]

    def mutate_content(members):
        for index, (member, data) in enumerate(members):
            if member.name.endswith("asset.txt"):
                members[index] = (member, bytes([data[0] ^ 1]) + data[1:])
                return

    _rewrite_archive(paths, case.key, mutate_content)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(ContractError, match="content disagrees"):
        extract_bundle(
            paths.directory,
            destination,
            case.store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".must-not-exist.*"))


@pytest.mark.parametrize(
    "attack",
    [
        "traversal",
        "absolute",
        "backslash",
        "symlink",
        "special",
        "extra",
        "duplicate",
        "unsorted",
    ],
)
def test_signed_unsafe_archive_entries_are_rejected(
    tmp_path: Path, attack: str
) -> None:
    case = _case(tmp_path)
    paths = case.paths["drone"]

    def mutate(members):
        selected = next(
            index
            for index, (member, _) in enumerate(members)
            if member.name.startswith("payload/")
        )
        member, data = members[selected]
        if attack == "traversal":
            member.name = "payload/../escape"
        elif attack == "absolute":
            member.name = "/payload/escape"
        elif attack == "backslash":
            member.name = "payload\\escape"
        elif attack == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../../escape"
            member.size = 0
            data = b""
        elif attack == "special":
            member.type = tarfile.FIFOTYPE
            member.size = 0
            data = b""
        elif attack == "extra":
            duplicate = tarfile.TarInfo("payload/unsigned-extra")
            duplicate.mode = 0o644
            duplicate.uid = duplicate.gid = duplicate.mtime = 0
            duplicate.size = 1
            members.append((duplicate, b"x"))
        elif attack == "duplicate":
            members.append((member, data))
        else:
            members[0], members[1] = members[1], members[0]
        members[selected] = (member, data)

    _rewrite_archive(paths, case.key, mutate)
    with pytest.raises(ContractError):
        verify_bundle(
            paths.directory, case.store, registry=REGISTRY, host_limits=LIMITS
        )


def test_signer_rotation_requires_proof_and_preserves_final_authority(
    tmp_path: Path,
) -> None:
    first_key = tmp_path / "first.pem"
    first_public = tmp_path / "first.json"
    first = generate_signer(
        first_key, first_public, authority="ci-qualified", registry=REGISTRY
    )
    store = tmp_path / "trusted.json"
    invalid_proof = signer_proof(first_key)
    invalid_proof["proof"] = "A" * 86 + "=="
    with pytest.raises(ContractError, match="proof"):
        add_trusted_signer(store, first_public, invalid_proof, REGISTRY)
    add_trusted_signer(store, first_public, signer_proof(first_key), REGISTRY)
    with pytest.raises(ContractError, match="final active"):
        revoke_trusted_signer(store, first["signer_id"], REGISTRY)
    second_key = tmp_path / "second.pem"
    second_public = tmp_path / "second.json"
    second = generate_signer(
        second_key, second_public, authority="ci-qualified", registry=REGISTRY
    )
    add_trusted_signer(store, second_public, signer_proof(second_key), REGISTRY)
    rotated = revoke_trusted_signer(store, first["signer_id"], REGISTRY)
    states = {item["signer_id"]: item["state"] for item in rotated["signers"]}
    assert states == {first["signer_id"]: "revoked", second["signer_id"]: "active"}
    assert load_trusted_signers(store, REGISTRY) == rotated
    assert first_key.read_text(encoding="ascii").startswith(
        "-----BEGIN PRIVATE KEY-----"
    )
    assert "PRIVATE" not in first_public.read_text(encoding="utf-8")


def test_bundle_signed_by_revoked_key_is_rejected(tmp_path: Path) -> None:
    case = _case(tmp_path)
    second_key = tmp_path / "second.pem"
    second_public = tmp_path / "second.json"
    generate_signer(
        second_key, second_public, authority="ci-qualified", registry=REGISTRY
    )
    add_trusted_signer(case.store, second_public, signer_proof(second_key), REGISTRY)
    revoke_trusted_signer(case.store, case.manifest["signing"]["signer_id"], REGISTRY)
    with pytest.raises(ContractError, match="revoked"):
        inspect_bundle(
            case.paths["drone"].directory,
            case.store,
            registry=REGISTRY,
            host_limits=LIMITS,
        )


def test_private_key_storage_and_trust_store_symlinks_fail_closed(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ContractError, match="outside the repository"):
        generate_signer(
            repository / "key.pem",
            tmp_path / "public.json",
            authority="ci-qualified",
            registry=REGISTRY,
            forbidden_roots=(repository,),
        )
    case = _case(tmp_path / "case")
    case.key.chmod(0o644)
    with pytest.raises(ContractError, match="user-only"):
        load_private_key(case.key)
    dangling = tmp_path / "dangling-trust"
    dangling.symlink_to(tmp_path / "missing")
    with pytest.raises(ContractError, match="symlink"):
        load_trusted_signers(dangling, REGISTRY)


@pytest.mark.parametrize(
    ("limit", "value", "expected"),
    [
        ("entries", 3, "entry"),
        ("unpacked_bytes", 1, "byte"),
        ("maximum_path_bytes", 12, "path"),
        ("maximum_path_depth", 2, "depth"),
    ],
)
def test_packaging_enforces_all_host_ceilings(
    tmp_path: Path, limit: str, value: int, expected: str
) -> None:
    case = _case(tmp_path / "baseline")
    stricter = dict(LIMITS)
    stricter[limit] = value
    with pytest.raises(ContractError, match=expected):
        package_bundle_set(
            case.manifest_path,
            case.roots,
            case.key,
            tmp_path / "rejected",
            registry=REGISTRY,
            host_limits=stricter,
        )
    assert not (tmp_path / "rejected").exists()
    assert not list(tmp_path.glob(".rejected.*"))


def test_inspection_enforces_host_ceiling_before_staging(tmp_path: Path) -> None:
    case = _case(tmp_path)
    destination = tmp_path / "destination"
    stricter = dict(LIMITS)
    stricter["unpacked_bytes"] = 1
    with pytest.raises(ContractError, match="host ceiling"):
        extract_bundle(
            case.paths["drone"].directory,
            destination,
            case.store,
            registry=REGISTRY,
            host_limits=stricter,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".destination.*"))


def test_archive_identity_is_rechecked_on_the_open_stream(tmp_path: Path) -> None:
    case = _case(tmp_path)
    verified = inspect_bundle(
        case.paths["drone"].directory,
        case.store,
        registry=REGISTRY,
        host_limits=LIMITS,
    )
    with case.paths["drone"].archive.open("ab") as stream:
        stream.write(b"changed-after-inspection")
    with pytest.raises(ContractError, match="changed after detached verification"):
        _stream_archive(verified, None)


def test_noncanonical_zstd_frame_is_rejected_even_when_resigned(tmp_path: Path) -> None:
    case = _case(tmp_path)
    paths = case.paths["drone"]
    uncompressed = _read_zstd(paths.archive)
    paths.archive.write_bytes(
        zstandard.ZstdCompressor(level=19, write_checksum=False).compress(uncompressed)
    )
    _resign_archive(paths, case.key)
    with pytest.raises(ContractError, match="frame parameters"):
        inspect_bundle(
            paths.directory, case.store, registry=REGISTRY, host_limits=LIMITS
        )


@pytest.mark.parametrize("unsafe", ["src", "build", ".git"])
def test_source_and_build_trees_are_not_packageable(
    tmp_path: Path, unsafe: str
) -> None:
    case = _case(tmp_path / "baseline")
    (case.roots["drone"] / unsafe).mkdir()
    with pytest.raises(ContractError, match="source/build"):
        package_bundle_set(
            case.manifest_path,
            case.roots,
            case.key,
            tmp_path / "rejected",
            registry=REGISTRY,
            host_limits=LIMITS,
        )


def test_payload_links_are_rejected_before_archive_creation(tmp_path: Path) -> None:
    case = _case(tmp_path / "baseline")
    (case.roots["drone"] / "linked").symlink_to("share/asset.txt")
    with pytest.raises(ContractError, match="link or special"):
        package_bundle_set(
            case.manifest_path,
            case.roots,
            case.key,
            tmp_path / "rejected",
            registry=REGISTRY,
            host_limits=LIMITS,
        )


def test_component_filename_and_directory_shape_are_not_trusted(tmp_path: Path) -> None:
    case = _case(tmp_path)
    component = case.paths["drone"].directory
    (component / "run-me.sh").write_text("exit 0\n")
    with pytest.raises(ContractError, match="missing, extra"):
        inspect_bundle(component, case.store, registry=REGISTRY, host_limits=LIMITS)


def test_packager_and_verifier_operator_entrypoints(tmp_path: Path) -> None:
    case = _case(tmp_path / "library")
    output = tmp_path / f"{case.manifest['release_id']}.iii-release-v1"
    package = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release" / "package_release_bundles.py"),
            "--release-manifest",
            str(case.manifest_path),
            "--component",
            f"drone={case.roots['drone']}",
            "--component",
            f"gc={case.roots['gc']}",
            "--private-key",
            str(case.key),
            "--output",
            str(output),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert package.returncode == 0, package.stderr
    assert json.loads(package.stdout)["outcome"] == "passed"
    destination = tmp_path / "script-extracted"
    verify_process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release" / "verify_release_bundle.py"),
            "extract",
            "--bundle",
            str(output / "drone"),
            "--trusted-signers",
            str(case.store),
            "--destination",
            str(destination),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verify_process.returncode == 0, verify_process.stderr
    report = json.loads(verify_process.stdout)
    assert report["outcome"] == "passed"
    assert report["component"] == "drone"
    assert report["content"]
    assert report["compressed_bytes"] > 0
    assert (destination / "payload" / "share" / "asset.txt").read_text() == "drone\n"


def test_signer_operator_entrypoint_never_emits_private_material(
    tmp_path: Path,
) -> None:
    script = ROOT / "scripts" / "release" / "manage_release_signers.py"
    key = tmp_path / "private.pem"
    public = tmp_path / "public.json"
    store = tmp_path / "trusted.json"

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(script), "--json", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result

    generated = run(
        "generate",
        "--authority",
        "ci-qualified",
        "--private-key",
        str(key),
        "--public-descriptor",
        str(public),
    )
    proof = run("prove", "--private-key", str(key))
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(proof.stdout, encoding="utf-8")
    run(
        "add",
        "--store",
        str(store),
        "--public-descriptor",
        str(public),
        "--proof",
        str(proof_path),
    )
    listed = run("list", "--store", str(store))
    output = (
        generated.stdout
        + proof.stdout
        + listed.stdout
        + public.read_text(encoding="utf-8")
    )
    assert "BEGIN PRIVATE KEY" not in output
    assert json.loads(listed.stdout)["signers"][0]["state"] == "active"
