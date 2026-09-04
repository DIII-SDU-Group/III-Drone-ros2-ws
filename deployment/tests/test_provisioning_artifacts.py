from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tarfile
import zipfile

import pytest

from iii_deployment.contracts import ContractRegistry, canonical_json
from iii_deployment.host_provision import load_input
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.provisioning_artifacts import (
    ProvisioningArtifactError,
    RECEIVER_MODULES,
    RECEIVER_PYTHON_VERSION,
    RECEIVER_SITE_PACKAGES,
    _require_receiver_python,
    _extract_receiver_wheels,
    inspect_materialization,
    inspect_receiver_update_materialization,
    materialize,
    materialize_receiver_update,
    write_receiver_requirements,
)
from iii_deployment.receiver.update import verify_receiver_update

WORKSPACE = Path(__file__).parents[2]
SCHEMAS = WORKSPACE / "deployment/schemas/v1"


def test_controller_builder_entrypoint_is_executable() -> None:
    script = WORKSPACE / "deployment/scripts/prepare_host_provisioning_artifacts.py"
    assert os.access(script, os.X_OK)
    update = WORKSPACE / "deployment/scripts/prepare_receiver_update_artifact.py"
    assert os.access(update, os.X_OK)


def test_receiver_artifact_builder_rejects_wrong_python_abi(tmp_path: Path) -> None:
    wrong_python = tmp_path / "python"
    wrong_python.write_text("#!/bin/sh\nprintf '3.10\\n'\n", encoding="ascii")
    wrong_python.chmod(0o700)

    with pytest.raises(
        ProvisioningArtifactError,
        match=r"receiver artifacts require Python 3\.12; observed 3\.10",
    ):
        _require_receiver_python(wrong_python)

    assert RECEIVER_PYTHON_VERSION == (3, 12)


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
        archive.writestr("fixture_runtime/__init__.py", "GENERATION = 1\n")
    return write_receiver_requirements(destination)


def test_receiver_requirements_reject_unknown_or_incomplete_distribution_set(
    tmp_path: Path,
) -> None:
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with zipfile.ZipFile(unknown / "UNKNOWN-0.0.0-py3-none-any.whl", "w") as archive:
        archive.writestr(
            "UNKNOWN-0.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: UNKNOWN\nVersion: 0.0.0\n",
        )
    with pytest.raises(ProvisioningArtifactError, match="identity is invalid"):
        write_receiver_requirements(unknown)

    incomplete = tmp_path / "incomplete"
    _fake_wheelhouse(incomplete)
    with pytest.raises(ProvisioningArtifactError, match="missing required"):
        write_receiver_requirements(
            incomplete, required_distributions={"fixture-runtime", "iii-deployment"}
        )


def test_materializer_produces_complete_signed_owner_controlled_input(
    tmp_path: Path,
) -> None:
    token = tmp_path / "runtime-token"
    token.write_text("T" * 43 + "\n", encoding="ascii")
    token.chmod(0o600)
    ssh_key = tmp_path / "ssh-key"
    ssh_key.write_text("fixture-private-key\n", encoding="utf-8")
    ssh_key.chmod(0o600)
    maintenance_key = tmp_path / "maintenance-key.pub"
    maintenance_key.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "b" * 43 + " iii\n",
        encoding="ascii",
    )
    maintenance_key.chmod(0o600)
    known_hosts = tmp_path / "known-hosts"
    known_hosts.write_text("10.42.0.70 fixture-host-key\n", encoding="utf-8")
    target_python = shutil.which("python3.12")
    if target_python is None:
        pytest.skip("receiver artifact materialization requires a Python 3.12 builder")
    python_link = tmp_path / "build-python"
    python_link.symlink_to(target_python)
    output = tmp_path / "host-provision"
    inspection = inspect_materialization(
        output_root=output,
        workspace_root=WORKSPACE,
        enrollment=_enrollment(tmp_path / "enrollment.json"),
        runtime_token=token,
        ssh_private_key=ssh_key,
        maintenance_ssh_public_key=maintenance_key,
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
    assert values["maintenance_ssh_public_key"].startswith("ssh-ed25519 ")
    assert (
        stored["maintenance_ssh_client_id"]
        == hashlib.sha256(
            " ".join(maintenance_key.read_text(encoding="ascii").split()[:2]).encode(
                "ascii"
            )
        ).hexdigest()
    )
    assert (output / "access/maintenance-ssh-ed25519.pub").read_text().count(" ") == 1
    verified = verify_receiver_update(
        output / "artifacts/receiver-bundle",
        trust=output / "trust/receiver-update-signers.json",
        registry=registry,
    )
    indexed = {item["path"] for item in verified.manifest["content"]}
    assert f"{RECEIVER_SITE_PACKAGES}/fixture_runtime/__init__.py" in indexed
    with tarfile.open(
        output / "artifacts/receiver-bundle/receiver-update.tar"
    ) as archive:
        for command, module in RECEIVER_MODULES.items():
            launcher = archive.extractfile(f"bin/{command}")
            assert launcher is not None
            launcher_text = launcher.read().decode("utf-8")
            assert (
                "/opt/iii/receiver/selectors/current/lib/python3.12/site-packages"
                in launcher_text
            )
            assert "PYTHONDONTWRITEBYTECODE=1" in launcher_text
            assert f"-B -S -m {module}" in launcher_text
            assert "/opt/iii/receiver/bootstrap/bin" not in launcher_text
    inventory = json.loads((output / "inventory.json").read_text())
    host = inventory["all"]["children"]["aircraft"]["hosts"]["10.42.0.70"]
    assert host["ansible_user"] == "iii-bootstrap"
    assert "StrictHostKeyChecking=yes" in host["ansible_ssh_common_args"]
    assert not (output / "receiver-payload").exists()
    assert all(
        path.stat().st_mode & 0o077 == 0 for path in output.rglob("*") if path.is_file()
    )

    update_output = tmp_path / "receiver-update-generation-2"
    update_inspection = inspect_receiver_update_materialization(
        output_root=update_output,
        provisioning_root=output,
        workspace_root=WORKSPACE,
        generation=2,
        version="v1.0.1",
        schema_root=SCHEMAS,
    )
    update_record = materialize_receiver_update(
        update_inspection, operation_id="iii-receiver-update-artifact-test"
    )
    stored_update = json.loads((update_output / "artifact-record.json").read_text())
    registry.validate("receiver-update-artifact", stored_update)
    assert stored_update["record_id"] == update_record["record_id"]
    assert stored_update["source_provisioning_record_id"] == stored["record_id"]
    assert stored_update["source_receiver_id"] == stored["receiver_id"]
    assert stored_update["generation"] == 2
    assert stored_update["signer_id"] == stored["signer_ids"]["receiver_update"]
    updated = verify_receiver_update(
        update_output / "bundle",
        trust=output / "trust/receiver-update-signers.json",
        registry=registry,
    )
    assert updated.manifest["receiver_id"] == stored_update["receiver_id"]
    assert updated.manifest["generation"] == 2
    assert all(
        path.stat().st_mode & 0o077 == 0
        for path in update_output.rglob("*")
        if path.is_file()
    )

    wheel = next((output / "artifacts/receiver-wheelhouse").glob("*.whl"))
    original = wheel.read_bytes()
    wheel.write_bytes(original + b"tamper")
    with pytest.raises(ProvisioningArtifactError, match="wheel changed"):
        inspect_receiver_update_materialization(
            output_root=tmp_path / "tampered-update",
            provisioning_root=output,
            workspace_root=WORKSPACE,
            generation=3,
            version="v1.0.2",
            schema_root=SCHEMAS,
        )
    wheel.write_bytes(original)
    wheel.chmod(0o600)

    with pytest.raises(ProvisioningArtifactError, match="newer than"):
        inspect_receiver_update_materialization(
            output_root=tmp_path / "old-generation-update",
            provisioning_root=output,
            workspace_root=WORKSPACE,
            generation=1,
            version="v1.0.0",
            schema_root=SCHEMAS,
        )


@pytest.mark.parametrize("hostile", ["../escape.py", "/absolute.py"])
def test_receiver_wheel_expansion_rejects_escaping_paths(tmp_path, hostile):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    with zipfile.ZipFile(wheelhouse / "hostile.whl", "w") as archive:
        archive.writestr(hostile, "unsafe\n")

    with pytest.raises(ProvisioningArtifactError, match="unsafe|escapes"):
        _extract_receiver_wheels(wheelhouse, tmp_path / "site-packages")


def test_receiver_wheel_expansion_rejects_symbolic_links(tmp_path):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    linked = zipfile.ZipInfo("linked.py")
    linked.create_system = 3
    linked.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheelhouse / "hostile.whl", "w") as archive:
        archive.writestr(linked, "outside.py")

    with pytest.raises(ProvisioningArtifactError, match="link or special"):
        _extract_receiver_wheels(wheelhouse, tmp_path / "site-packages")
