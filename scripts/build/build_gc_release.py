#!/usr/bin/env python3
"""Build and smoke-test pinned x86_64 GC OCI artifacts from a clean snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.contracts import (  # noqa: E402
    ContractError,
    ContractRegistry,
    SEMVER,
    canonical_json,
    content_identity,
)
from iii_deployment.build import materialize_build_source  # noqa: E402
from iii_deployment.source import (  # noqa: E402
    capture_source_snapshot,
    load_source_policy,
    verify_source_snapshot,
)

BASE_PATTERN = re.compile(
    r"^FROM\s+([^\s]+@sha256:[a-f0-9]{64})(?:\s+AS\s+\S+)?$", re.MULTILINE
)
SKOPEO_IMAGE = (
    "quay.io/skopeo/stable@"
    "sha256:47853bb9fb24202af9110531ebd6e43c5f97701254ca290596640290d17942f4"
)
BUILD_INPUTS = (
    "deployment/gc-application-policy.json",
    "deployment/gc/compose.release.yml",
    "deployment/qgc/key-policy.json",
    "deployment/qgc/managed-settings.json",
    "src/III-Drone-GC/docker/proxy.Dockerfile",
    "src/III-Drone-GC/docker/proxy-requirements.lock",
    "src/III-Drone-GC/frontend/Dockerfile",
    "src/III-Drone-GC/frontend/package-lock.json",
)


def _builder_resource_options(parallel_workers: int | None) -> list[str]:
    if parallel_workers is None:
        return []
    if parallel_workers < 1:
        raise ContractError("parallel worker count must be positive")
    return [
        "--driver-opt",
        f"cpu-quota={parallel_workers * 100_000}",
        "--driver-opt",
        "cpu-period=100000",
    ]


def _validate_qgc_appimage(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Authenticate the pinned x86_64 AppImage and prove it has no update feed."""
    path = path.resolve()
    expected = policy["qgroundcontrol"]
    if path.is_symlink() or not path.is_file():
        raise ContractError("pinned QGroundControl AppImage is missing or unsafe")
    if path.stat().st_size != expected["bytes"] or _sha256(path) != expected["sha256"]:
        raise ContractError(
            "QGroundControl AppImage differs from the pinned size/checksum"
        )
    header = path.read_bytes()[:20]
    if (
        len(header) < 20
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or int.from_bytes(header[18:20], "little") != 62
    ):
        raise ContractError("QGroundControl AppImage is not an x86_64 ELF64 executable")
    if not os.access(path, os.X_OK):
        raise ContractError("QGroundControl AppImage is not executable")
    runtime = _run([str(path), "--appimage-version"], cwd=path.parent)
    if "AppImage runtime version:" not in runtime.stdout + runtime.stderr:
        raise ContractError("QGroundControl AppImage runtime self-check failed")
    update = _run([str(path), "--appimage-updateinfo"], cwd=path.parent)
    if update.stdout.strip() != expected["appimage_update_information"]:
        raise ContractError(
            "QGroundControl AppImage unexpectedly embeds self-update information"
        )
    return {
        "version": expected["version"],
        "appimage": expected["filename"],
        "source_url": expected["source_url"],
        "sha256": expected["sha256"],
        "bytes": expected["bytes"],
        "appimage_update_information": expected["appimage_update_information"],
        "update_owner": expected["update_owner"],
        "runtime_self_check": "passed",
    }


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


def _run(
    command: Sequence[str], *, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip() or "command failed"
        raise ContractError(
            f"offboard GC command failed ({' '.join(command[:4])}): {detail}"
        )
    return process


def _pinned_bases(dockerfile: Path) -> list[str]:
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read GC Dockerfile: {exc}") from exc
    from_lines = [
        line for line in text.splitlines() if line.strip().upper().startswith("FROM ")
    ]
    values = BASE_PATTERN.findall(text)
    if not from_lines or len(values) != len(from_lines):
        raise ContractError(
            f"every GC base image must use an exact sha256 digest: {dockerfile}"
        )
    return values


def inspect_oci_archive(path: Path) -> str:
    """Validate an OCI-layout archive containing a Docker v2 runtime manifest."""
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"GC OCI archive is missing or unsafe: {path}")
    try:
        with tarfile.open(path, "r:*") as archive:
            members = archive.getmembers()
            names = [member.name.removeprefix("./") for member in members]
            if len(names) != len(set(names)) or any(
                member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
                for member in members
            ):
                raise ContractError(
                    "GC OCI archive contains duplicate, linked, or special entries"
                )
            required = {"oci-layout", "index.json"}
            if not required.issubset(names):
                raise ContractError("GC OCI archive is missing its OCI layout/index")

            def read(name: str) -> bytes:
                member = next(
                    (item for item in members if item.name.removeprefix("./") == name),
                    None,
                )
                if member is None or not member.isfile():
                    raise ContractError(f"GC OCI archive entry is missing: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ContractError(f"GC OCI archive entry cannot be read: {name}")
                return stream.read()

            for name in names:
                if not name.startswith("blobs/sha256/"):
                    continue
                digest = name.rsplit("/", 1)[-1]
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or hashlib.sha256(read(name)).hexdigest() != digest
                ):
                    raise ContractError(f"GC OCI blob identity mismatch: {name}")
            index = json.loads(read("index.json"))
            manifests = index.get("manifests", [])
            if len(manifests) != 1:
                raise ContractError(
                    "GC OCI archive must contain exactly one image manifest"
                )
            descriptor = manifests[0]
            digest = descriptor.get("digest", "")
            media_type = "application/vnd.docker.distribution.manifest.v2+json"
            if descriptor.get("mediaType") != media_type:
                raise ContractError(
                    "GC OCI archive must retain a Docker schema-v2 runtime manifest"
                )
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ContractError("GC OCI archive manifest digest is invalid")
            manifest = json.loads(
                read("blobs/sha256/" + digest.removeprefix("sha256:"))
            )
            if manifest.get("mediaType") != media_type:
                raise ContractError("GC OCI archive manifest media type differs")
            config_digest = manifest.get("config", {}).get("digest", "")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", config_digest):
                raise ContractError("GC OCI archive image configuration is invalid")
            config = json.loads(
                read("blobs/sha256/" + config_digest.removeprefix("sha256:"))
            )
            platform = {
                "architecture": config.get("architecture"),
                "os": config.get("os"),
            }
            if platform != {"architecture": "amd64", "os": "linux"}:
                raise ContractError(
                    f"GC OCI archive has unexpected platform: {platform!r}"
                )
            return digest
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot inspect GC OCI archive: {exc}") from exc


def _build_image(
    *,
    name: str,
    dockerfile: Path,
    output: Path,
    cache: Path,
    source_date_epoch: int,
    tag: str,
    smoke: Sequence[str],
    builder: str | None,
    context: Path,
) -> dict[str, Any]:
    builder_args = [] if builder is None else ["--builder", builder]
    inspect = _run(["docker", "buildx", "inspect", *builder_args, "--bootstrap"])
    supports_local_cache = any(
        line.strip().lower() == "driver: docker-container"
        for line in inspect.stdout.splitlines()
    )
    cache_from = (
        ["--cache-from", f"type=local,src={cache}"]
        if supports_local_cache and (cache / "index.json").is_file()
        else []
    )
    cache_to = (
        ["--cache-to", f"type=local,dest={cache},mode=max"]
        if supports_local_cache
        else []
    )
    common = [
        "docker",
        "buildx",
        "build",
        *builder_args,
        "--platform",
        "linux/amd64",
        "--file",
        str(dockerfile),
        "--build-arg",
        f"SOURCE_DATE_EPOCH={source_date_epoch}",
        "--provenance=false",
        "--sbom=false",
        *cache_from,
    ]
    # One BuildKit solve feeds the smoke-tested daemon image. Skopeo then records
    # that exact image as a Docker schema-v2 manifest inside an OCI layout. Docker
    # cannot import an OCI-media-type manifest while preserving its digest, so the
    # runtime media type is fixed here instead of being converted during install.
    _run(
        [
            *common,
            *cache_to,
            "--output",
            "type=docker",
            "--tag",
            tag,
            ".",
        ],
        cwd=context,
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            smoke[0],
            tag,
            *smoke[1:],
        ]
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            "/var/run/docker.sock:/var/run/docker.sock",
            "--volume",
            f"{output.parent.resolve()}:/output",
            SKOPEO_IMAGE,
            "copy",
            "--preserve-digests",
            "--format",
            "v2s2",
            f"docker-daemon:{tag}",
            f"oci-archive:/output/{output.name}:{tag}",
        ]
    )
    manifest_digest = inspect_oci_archive(output)
    return {
        "name": name,
        "archive": output.name,
        "sha256": _sha256(output),
        "bytes": output.stat().st_size,
        "manifest_digest": manifest_digest,
        "base_images": _pinned_bases(dockerfile),
        "smoke_test": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--test-record", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--qgc-appimage", type=Path, required=True)
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help="Enforce an aggregate CPU ceiling on the isolated BuildKit worker.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    partial = args.output.parent / f".{args.output.name}.partial-{os.getpid()}"
    tags: list[str] = []
    private_builder: str | None = None
    try:
        if not SEMVER.fullmatch(args.version):
            raise ContractError("GC release version is not strict SemVer")
        if args.source_date_epoch < 0:
            raise ContractError("SOURCE_DATE_EPOCH cannot be negative")
        builder_resources = _builder_resource_options(args.parallel_workers)
        if args.output.exists() or args.output.is_symlink() or partial.exists():
            raise ContractError(
                "GC build output or private partial path already exists"
            )
        registry = ContractRegistry(ROOT / "deployment/schemas/v1")
        qgc_policy = _json(ROOT / "deployment/gc-application-policy.json")
        registry.validate("gc-application-policy", qgc_policy)
        snapshot = _json(args.snapshot, canonical=True)
        verify_source_snapshot(snapshot, registry)
        policy = load_source_policy(ROOT / "deployment/source-policy.json", registry)
        current = capture_source_snapshot(ROOT, policy, registry)
        if current["content_identity"] != snapshot["content_identity"]:
            raise ContractError(
                "live source no longer matches the verified source snapshot"
            )
        test_record = _json(args.test_record, canonical=True)
        registry.validate("qualification-check", test_record)
        if (
            test_record["check_id"] != "gc-tests"
            or test_record["source_commit"] != snapshot["workspace_commit"]
            or test_record["version"] != args.version
        ):
            raise ContractError("GC test record differs from this release candidate")
        partial.mkdir(parents=True, mode=0o700)
        build_source = materialize_build_source(
            ROOT, (partial / ".build-source").resolve()
        )
        images_dir = partial / "images"
        images_dir.mkdir()
        args.cache.mkdir(parents=True, exist_ok=True)
        default_builder = _run(["docker", "buildx", "inspect", "--bootstrap"])
        default_is_container = any(
            line.lower().startswith("driver:")
            and line.split(":", 1)[1].strip().lower() == "docker-container"
            for line in default_builder.stdout.splitlines()
        )
        if builder_resources or not default_is_container:
            private_builder = f"iii-gc-qualification-{os.getpid()}"
            _run(
                [
                    "docker",
                    "buildx",
                    "create",
                    "--name",
                    private_builder,
                    "--driver",
                    "docker-container",
                    *builder_resources,
                ]
            )
            _run(
                [
                    "docker",
                    "buildx",
                    "inspect",
                    "--builder",
                    private_builder,
                    "--bootstrap",
                ]
            )
        short = snapshot["content_identity"][:16]
        specs = (
            (
                "frontend",
                build_source / "src/III-Drone-GC/frontend/Dockerfile",
                ["/bin/sh", "-c", "test -s /usr/share/nginx/html/index.html"],
            ),
            (
                "proxy",
                build_source / "src/III-Drone-GC/docker/proxy.Dockerfile",
                [
                    "python",
                    "-c",
                    "from iii_drone_gc.v2_proxy.app import create_app; create_app()",
                ],
            ),
        )
        images = []
        for name, dockerfile, smoke in specs:
            tag = f"iii-gc-{name}-qualification:{short}"
            tags.append(tag)
            images.append(
                _build_image(
                    name=name,
                    dockerfile=dockerfile,
                    output=images_dir / f"{name}.oci",
                    cache=args.cache / name,
                    source_date_epoch=args.source_date_epoch,
                    tag=tag,
                    smoke=smoke,
                    builder=private_builder,
                    context=build_source,
                )
            )
        shutil.rmtree(build_source)
        qgc_dir = partial / "qgc"
        qgc_dir.mkdir()
        qgc_path = qgc_dir / qgc_policy["qgroundcontrol"]["filename"]
        shutil.copyfile(args.qgc_appimage, qgc_path, follow_symlinks=False)
        qgc_path.chmod(0o555)
        qgroundcontrol = _validate_qgc_appimage(qgc_path, qgc_policy)
        qgc_config_dir = qgc_dir / "config"
        qgc_config_dir.mkdir()
        key_policy_source = ROOT / "deployment/qgc/key-policy.json"
        baseline_source = ROOT / "deployment/qgc/managed-settings.json"
        key_policy = _json(key_policy_source, canonical=True)
        baseline = _json(baseline_source, canonical=True)
        registry.validate("qgc-key-policy", key_policy)
        registry.validate("qgc-managed-settings", baseline)
        if key_policy["policy_id"] != content_identity(
            {key: value for key, value in key_policy.items() if key != "policy_id"}
        ):
            raise ContractError("QGroundControl key-policy identity mismatch")
        if (
            baseline["settings_id"]
            != content_identity(
                {key: value for key, value in baseline.items() if key != "settings_id"}
            )
            or baseline["policy_id"] != key_policy["policy_id"]
        ):
            raise ContractError("QGroundControl managed-settings identity mismatch")
        key_policy_path = qgc_config_dir / "key-policy.json"
        baseline_path = qgc_config_dir / "managed-settings.json"
        shutil.copyfile(key_policy_source, key_policy_path, follow_symlinks=False)
        shutil.copyfile(baseline_source, baseline_path, follow_symlinks=False)
        key_policy_path.chmod(0o444)
        baseline_path.chmod(0o444)
        qgroundcontrol["configuration"] = {
            "policy": "qgc/config/key-policy.json",
            "policy_sha256": _sha256(key_policy_path),
            "policy_id": key_policy["policy_id"],
            "baseline": "qgc/config/managed-settings.json",
            "baseline_sha256": _sha256(baseline_path),
            "settings_id": baseline["settings_id"],
        }
        compose_source = ROOT / qgc_policy["application"]["compose_source"]
        compose_path = partial / qgc_policy["application"]["compose_slot_name"]
        shutil.copyfile(compose_source, compose_path, follow_symlinks=False)
        compose_path.chmod(0o444)
        application = {
            "compose": compose_path.name,
            "compose_sha256": _sha256(compose_path),
            "environment": qgc_policy["application"]["environment_slot_name"],
        }
        input_hashes = {path: _sha256(ROOT / path) for path in BUILD_INPUTS}
        record = {
            "schema": "iii.gc-build-record/v1",
            "build_id": "0" * 64,
            "source_identity": snapshot["content_identity"],
            "source_commit": snapshot["workspace_commit"],
            "version": args.version,
            "platform": {"os": "linux", "architecture": "amd64"},
            "inputs_sha256": content_identity(input_hashes),
            "test_record_sha256": _sha256(args.test_record),
            "images": images,
            "qgroundcontrol": qgroundcontrol,
            "application": application,
            "complete": True,
        }
        record["build_id"] = content_identity(
            {key: value for key, value in record.items() if key != "build_id"}
        )
        registry.validate("gc-build-record", record)
        (partial / "build-record.json").write_bytes(canonical_json(record) + b"\n")
        os.replace(partial, args.output)
        result = {
            "schema": "iii.gc-build-result/v1",
            "outcome": "passed",
            "build_id": record["build_id"],
            "output": str(args.output),
        }
        print(
            json.dumps(result, sort_keys=True)
            if args.json
            else f"PASS: {record['build_id']}"
        )
        return 0
    except (ContractError, OSError) as exc:
        shutil.rmtree(partial, ignore_errors=True)
        result = {
            "schema": "iii.gc-build-result/v1",
            "outcome": "failed",
            "error": str(exc),
        }
        print(json.dumps(result, sort_keys=True) if args.json else f"FAIL: {exc}")
        return 30
    finally:
        for tag in tags:
            subprocess.run(
                ["docker", "image", "rm", tag], capture_output=True, check=False
            )
        if private_builder is not None:
            subprocess.run(
                ["docker", "buildx", "rm", private_builder],
                capture_output=True,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
