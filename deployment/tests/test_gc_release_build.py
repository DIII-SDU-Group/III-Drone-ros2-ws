import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

from iii_deployment.contracts import ContractError


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_gc_release", ROOT / "scripts/build/build_gc_release.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _oci(path: Path, *, platform: dict | None = None, corrupt_blob: bool = False) -> None:
    manifest = b'{"schemaVersion":2}'
    digest = hashlib.sha256(manifest).hexdigest()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [{
                "digest": f"sha256:{digest}",
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": len(manifest),
                "platform": platform or {"architecture": "amd64", "os": "linux"},
            }],
        }
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", index),
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


def test_gc_oci_inspection_binds_platform_manifest_and_every_blob(tmp_path: Path) -> None:
    archive = tmp_path / "gc.oci"
    _oci(archive)
    assert MODULE.inspect_oci_archive(archive).startswith("sha256:")
    _oci(archive, platform={"architecture": "arm64", "os": "linux"})
    with pytest.raises(ContractError, match="unexpected platform"):
        MODULE.inspect_oci_archive(archive)
    _oci(archive, corrupt_blob=True)
    with pytest.raises(ContractError, match="blob identity"):
        MODULE.inspect_oci_archive(archive)
