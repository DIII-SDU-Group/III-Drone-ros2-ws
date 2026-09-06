from __future__ import annotations

import base64

import pytest

from iii_deployment.seed_mount import SeedMountError, decode_payload


def test_seed_transfer_requires_exact_names_and_decodes_without_disk_residue() -> None:
    files = {
        "user-data": b"#cloud-config\n{}\n",
        "meta-data": b"{}\n",
        "network-config": b'{"version":2}\n',
    }
    payload = {
        "schema": "iii.nocloud-seed-transfer/v1",
        "files": {
            name: base64.b64encode(content).decode("ascii")
            for name, content in files.items()
        },
    }
    assert decode_payload(payload) == files
    payload["files"]["unexpected"] = "eA=="
    with pytest.raises(SeedMountError, match="exactly three"):
        decode_payload(payload)


def test_seed_transfer_rejects_invalid_base64_and_empty_files() -> None:
    payload = {
        "schema": "iii.nocloud-seed-transfer/v1",
        "files": {
            "user-data": "not base64!",
            "meta-data": "eA==",
            "network-config": "eA==",
        },
    }
    with pytest.raises(SeedMountError, match="invalid base64"):
        decode_payload(payload)
    payload["files"]["user-data"] = ""
    with pytest.raises(SeedMountError, match="size"):
        decode_payload(payload)
