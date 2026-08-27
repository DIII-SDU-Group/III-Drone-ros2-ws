import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest
import yaml

from iii_deployment.contracts import ContractError

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_gc_release", ROOT / "scripts/build/build_gc_release.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _oci(
    path: Path, *, platform: dict | None = None, corrupt_blob: bool = False
) -> None:
    platform = platform or {"architecture": "amd64", "os": "linux"}
    config = json.dumps(platform, sort_keys=True).encode()
    config_digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": f"sha256:{config_digest}",
                "size": len(config),
            },
            "layers": [],
        },
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(manifest).hexdigest()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": f"sha256:{digest}",
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "size": len(manifest),
                }
            ],
        }
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", index),
            (f"blobs/sha256/{config_digest}", config),
            (f"blobs/sha256/{digest}", b"tampered" if corrupt_blob else manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_gc_dockerfiles_pin_every_base_by_digest() -> None:
    proxy = ROOT / "src/III-Drone-GC/docker/proxy.Dockerfile"
    frontend = ROOT / "src/III-Drone-GC/frontend/Dockerfile"
    assert len(MODULE._pinned_bases(proxy)) == 1
    assert len(MODULE._pinned_bases(frontend)) == 2
    assert "@sha256:" in MODULE.SKOPEO_IMAGE
    assert ":latest" not in MODULE.SKOPEO_IMAGE


def test_gc_oci_inspection_binds_platform_manifest_and_every_blob(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "gc.oci"
    _oci(archive)
    assert MODULE.inspect_oci_archive(archive).startswith("sha256:")
    _oci(archive, platform={"architecture": "arm64", "os": "linux"})
    with pytest.raises(ContractError, match="unexpected platform"):
        MODULE.inspect_oci_archive(archive)
    _oci(archive, corrupt_blob=True)
    with pytest.raises(ContractError, match="blob identity"):
        MODULE.inspect_oci_archive(archive)


def test_gc_oci_inspection_rejects_oci_media_type_that_docker_would_convert(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "gc.oci"
    _oci(archive)
    with tarfile.open(archive, "r") as source:
        members = [(member, source.extractfile(member).read()) for member in source]
    with tarfile.open(archive, "w") as target:
        for member, data in members:
            if member.name == "index.json":
                index = json.loads(data)
                index["manifests"][0][
                    "mediaType"
                ] = "application/vnd.oci.image.manifest.v1+json"
                data = json.dumps(index).encode()
                member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ContractError, match="Docker schema-v2"):
        MODULE.inspect_oci_archive(archive)


def test_qgc_appimage_is_exact_x86_64_and_has_no_embedded_update_feed(
    monkeypatch, tmp_path: Path
) -> None:
    policy = json.loads((ROOT / "deployment/gc-application-policy.json").read_text())
    appimage = tmp_path / "QGroundControl.AppImage"
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    body = bytes(header) + b"x" * 16
    appimage.write_bytes(body)
    appimage.chmod(0o555)
    policy["qgroundcontrol"]["bytes"] = len(body)
    policy["qgroundcontrol"]["sha256"] = hashlib.sha256(body).hexdigest()

    def run(argv, **_kwargs):
        option = argv[-1]
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    ""
                    if option == "--appimage-updateinfo"
                    else "AppImage runtime version: pinned\n"
                ),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(MODULE, "_run", run)
    record = MODULE._validate_qgc_appimage(appimage, policy)
    assert record["runtime_self_check"] == "passed"
    assert record["appimage_update_information"] == ""

    def self_updating(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[-1] == "--appimage-updateinfo":
            result.stdout = "gh-releases-zsync|mutable"
        return result

    monkeypatch.setattr(MODULE, "_run", self_updating)
    with pytest.raises(ContractError, match="self-update"):
        MODULE._validate_qgc_appimage(appimage, policy)


def test_release_compose_denies_host_authority_and_mounts_only_read_only_drain_control() -> (
    None
):
    compose = yaml.safe_load(
        (ROOT / "deployment/gc/compose.release.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert set(services) == {"frontend", "proxy"}
    forbidden_fragments = (
        "/var/run/docker.sock",
        "/run/podman",
        "/home/",
        "/root",
        ".iii",
        "QGroundControl",
        "signer",
        "ansible",
        "builder",
    )
    all_mounts = []
    for name, service in services.items():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        mounts = service.get("volumes", [])
        all_mounts.extend(mounts)
        rendered = json.dumps(service, sort_keys=True)
        for forbidden in forbidden_fragments:
            assert (
                forbidden not in rendered
            ), f"{name} exposes forbidden host authority: {forbidden}"
    assert all_mounts == [
        "${III_GC_CONTROL_DIR:?III_GC_CONTROL_DIR is required}:/run/iii-gc/control:ro"
    ]
    assert services["proxy"]["environment"]["III_GC_MAINTENANCE_DRAIN_FILE"] == (
        "/run/iii-gc/control/drain.json"
    )
