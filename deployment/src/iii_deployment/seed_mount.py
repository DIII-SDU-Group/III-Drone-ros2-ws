"""Private-mount-namespace helper for writing NoCloud boot media.

This module is an implementation detail of :mod:`iii_deployment.host_imaging`.
Seed bytes arrive on stdin, never argv or a temporary host file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping


ALLOWED_NAMES = {"user-data", "meta-data", "network-config"}
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class SeedMountError(RuntimeError):
    pass


def decode_payload(value: Mapping[str, Any]) -> dict[str, bytes]:
    if value.get("schema") != "iii.nocloud-seed-transfer/v1":
        raise SeedMountError("unsupported seed transfer payload")
    encoded = value.get("files")
    if not isinstance(encoded, dict) or set(encoded) != ALLOWED_NAMES:
        raise SeedMountError("seed transfer must contain exactly three NoCloud files")
    files: dict[str, bytes] = {}
    for name, content in encoded.items():
        if not isinstance(content, str):
            raise SeedMountError("seed transfer content must be base64 text")
        try:
            files[name] = base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise SeedMountError("seed transfer contains invalid base64") from exc
        if not files[name] or len(files[name]) > MAX_PAYLOAD_BYTES:
            raise SeedMountError("seed file size is outside the safe limit")
    return files


def install(partition: Path, files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    metadata = partition.stat()
    if not stat.S_ISBLK(metadata.st_mode):
        raise SeedMountError("seed target is not a block-device partition")
    with tempfile.TemporaryDirectory(prefix="iii-seed-") as temporary:
        mountpoint = Path(temporary)
        subprocess.run(
            [
                "mount",
                "--options",
                "rw,nosuid,nodev,noexec",
                str(partition),
                str(mountpoint),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            evidence: list[dict[str, Any]] = []
            for name, content in sorted(files.items()):
                if name not in ALLOWED_NAMES:
                    raise SeedMountError("unexpected NoCloud seed filename")
                destination = mountpoint / name
                if destination.is_symlink():
                    raise SeedMountError(
                        f"refusing symbolic-link seed destination {name}"
                    )
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    view = memoryview(content)
                    while view:
                        count = os.write(descriptor, view)
                        if count <= 0:
                            raise SeedMountError("short write while installing seed")
                        view = view[count:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                observed = destination.read_bytes()
                if observed != content:
                    raise SeedMountError(f"on-media seed readback failed for {name}")
                evidence.append(
                    {
                        "path": name,
                        "sha256": hashlib.sha256(observed).hexdigest(),
                        "size": len(observed),
                    }
                )
            descriptor = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return evidence
        finally:
            subprocess.run(
                ["umount", str(mountpoint)],
                check=True,
                capture_output=True,
                text=True,
            )


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/dev/"):
        raise SystemExit("usage: python -m iii_deployment.seed_mount /dev/PARTITION")
    raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise SystemExit("seed transfer payload exceeds the safe limit")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise SeedMountError("seed transfer must be one JSON object")
        evidence = install(Path(sys.argv[1]), decode_payload(value))
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.CalledProcessError,
        SeedMountError,
    ) as exc:
        raise SystemExit(f"NoCloud seed installation failed: {exc}") from exc
    sys.stdout.write(json.dumps({"files": evidence}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
