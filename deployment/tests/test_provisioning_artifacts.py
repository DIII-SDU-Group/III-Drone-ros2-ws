from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import zipfile

from iii_deployment.contracts import ContractRegistry, canonical_json
from iii_deployment.host_provision import load_input
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.provisioning_artifacts import (
    inspect_materialization,
    materialize,
    write_receiver_requirements,
)
from iii_deployment.receiver.update import verify_receiver_update

WORKSPACE = Path(__file__).parents[2]
SCHEMAS = WORKSPACE / "deployment/schemas/v1"


def test_controller_builder_entrypoint_is_executable() -> None:
    script = WORKSPACE / "deployment/scripts/prepare_host_provisioning_artifacts.py"
    assert os.access(script, os.X_OK)


def _enrollment(path: Path) -> Path:
    ssh_blob = (
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"e" * 32
    )
    signer = b"f" * 32
    value = create_machine_enrollment(
        label="physical-provisioning",
        ssh_public_key="ssh-ed25519 " + base64.b64encode(ssh_blob).decode("ascii"),
        runtime_token="R" * 43,
        field_signer_descriptor={
            "schema_version": "1",
            "descriptor_type": "iii.signer-public",
            "signer_id": hashlib.sha256(signer).hexdigest(),
            "algorithm": "Ed25519",
            "authority": "workstation-field",
            "public_key": base64.b64encode(signer).decode("ascii"),
        },
        registry=ContractRegistry(SCHEMAS),
    )
    path.write_bytes(canonical_json(value) + b"\n")
    path.chmod(0o600)
    return path


def _fake_wheelhouse(destination: Path, **_kwargs) -> list[dict]:
    destination.mkdir(mode=0o700)
    wheel = destination / "fixture_runtime-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "fixture_runtime-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: fixture-runtime\nVersion: 1.0\n",
        )
        archive.writestr(
            "fixture_runtime/_vendor/helper-2.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: vendored-helper\nVersion: 2.0\n",
        )
    return write_receiver_requirements(destination)


def test_materializer_produces_complete_signed_owner_controlled_input(
    tmp_path: Path,
) -> None:
    token = tmp_path / "runtime-token"
    token.write_text("T" * 43 + "\n", encoding="ascii")
    token.chmod(0o600)
    ssh_key = tmp_path / "ssh-key"
    ssh_key.write_text("fixture-private-key\n", encoding="utf-8")
    ssh_key.chmod(0o600)
    known_hosts = tmp_path / "known-hosts"
    known_hosts.write_text("10.42.0.70 fixture-host-key\n", encoding="utf-8")
    python_link = tmp_path / "build-python"
    python_link.symlink_to("/usr/bin/python3")
    output = tmp_path / "host-provision"
    inspection = inspect_materialization(
        output_root=output,
        workspace_root=WORKSPACE,
        enrollment=_enrollment(tmp_path / "enrollment.json"),
        runtime_token=token,
        ssh_private_key=ssh_key,
        known_hosts=known_hosts,
        target="10.42.0.70",
        operator_cidr="10.42.0.0/24",
        python_executable=python_link,
        schema_root=SCHEMAS,
    )
    assert inspection["python_executable"] == str(python_link.absolute())

    record = materialize(
        inspection,
        operation_id="iii-host-artifacts-test",
        wheelhouse_builder=_fake_wheelhouse,
    )

    registry = ContractRegistry(SCHEMAS)
    stored = json.loads((output / "artifact-record.json").read_text())
    registry.validate("host-provisioning-artifacts", stored)
    assert stored["record_id"] == record["record_id"]
    values, source = load_input(output / "inputs.json", schema_root=SCHEMAS)
    assert source == output / "inputs.json"
    assert values["operator_cidr"] == "10.42.0.0/24"
    verify_receiver_update(
        output / "artifacts/receiver-bundle",
        trust=output / "trust/receiver-update-signers.json",
        registry=registry,
    )
    inventory = json.loads((output / "inventory.json").read_text())
    host = inventory["all"]["children"]["aircraft"]["hosts"]["10.42.0.70"]
    assert host["ansible_user"] == "iii-bootstrap"
    assert "StrictHostKeyChecking=yes" in host["ansible_ssh_common_args"]
    assert not (output / "receiver-payload").exists()
    assert all(
        path.stat().st_mode & 0o077 == 0 for path in output.rglob("*") if path.is_file()
    )
