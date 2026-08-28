"""Materialize owner-controlled inputs for first-aircraft host provisioning."""

from __future__ import annotations

from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import stat
import subprocess
import uuid
import zipfile
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractError, ContractRegistry, canonical_json, content_identity
from .host_provision import load_input
from .identity import load_machine_enrollment
from .receiver.update import package_receiver_update, verify_receiver_update
from .signers import generate_signer, validate_trusted_signers

RECORD_SCHEMA = "iii.host-provisioning-artifacts/v1"
LOCAL_PROJECTS = (
    "src/III-Drone-Contracts",
    "src/III-Drone-Configuration",
    "tools/III-Drone-CLI",
    "deployment",
)
COMPATIBILITY = {
    "activation_health_evidence_schemas": ["iii.activation-health/v1"],
    "activation_health_transaction_schemas": ["iii.activation-health-transaction/v1"],
    "activation_selector_schemas": ["iii.activation-selector/v1"],
    "activation_transaction_schemas": ["iii.activation-transaction/v1"],
    "audit_schemas": ["iii.receiver-audit/v1"],
    "bootstrap_protocols": ["1"],
    "cli_protocols": ["1"],
    "configuration_checkpoint_schemas": ["iii.configuration-checkpoint/v1"],
    "journal_schemas": ["iii.receiver-operation-journal/v1"],
    "release_manifest_schema_versions": ["1"],
    "request_protocols": ["1"],
    "upload_activity_schemas": ["iii.bundle-upload-activity/v1"],
    "upload_manifest_schemas": ["iii.bundle-upload/v1"],
}


class ProvisioningArtifactError(ContractError):
    code = "III_HOST_PROVISION_ARTIFACT_ERROR"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _owner_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise ProvisioningArtifactError(f"{label} must be a real regular file")
    metadata = resolved.stat()
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ProvisioningArtifactError(f"{label} must be owned by the invoking user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProvisioningArtifactError(f"{label} permissions must be owner-only")
    return resolved


def _require_ignored(path: Path) -> None:
    for parent in (path.parent, *path.parents):
        if not (parent / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(parent), "check-ignore", "--quiet", "--", str(path)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        if result.returncode == 1:
            raise ProvisioningArtifactError(
                "provisioning artifact output must be Git-ignored"
            )
        raise ProvisioningArtifactError("cannot authenticate output Git-ignore state")


def inspect_materialization(
    *,
    output_root: Path,
    workspace_root: Path,
    enrollment: Path,
    runtime_token: Path,
    ssh_private_key: Path,
    known_hosts: Path,
    target: str,
    operator_cidr: str,
    python_executable: Path,
    schema_root: Path,
) -> dict[str, Any]:
    output = output_root.expanduser().absolute()
    _require_ignored(output)
    if output.exists() or output.is_symlink():
        raise ProvisioningArtifactError("provisioning artifact output already exists")
    workspace = workspace_root.expanduser().resolve()
    for relative in (*LOCAL_PROJECTS, "deployment/portable-state-policy.json"):
        if not (workspace / relative).exists():
            raise ProvisioningArtifactError(
                f"required workspace source is missing: {relative}"
            )
    registry = ContractRegistry(schema_root)
    enrollment_path = _owner_file(enrollment, label="machine enrollment")
    enrollment_value = load_machine_enrollment(enrollment_path, registry)
    token_path = _owner_file(runtime_token, label="runtime API token")
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ProvisioningArtifactError(
            f"cannot read runtime API token: {exc}"
        ) from exc
    if len(token) < 16 or any(character.isspace() for character in token):
        raise ProvisioningArtifactError(
            "runtime API token is not one safe non-empty line"
        )
    ssh_key = _owner_file(ssh_private_key, label="bootstrap SSH private key")
    host_keys = known_hosts.expanduser().resolve()
    if known_hosts.is_symlink() or not host_keys.is_file():
        raise ProvisioningArtifactError("known-hosts evidence must be a regular file")
    # Preserve a selected virtual-environment entry point. Resolving its symlink
    # would silently switch builds back to the system interpreter and toolchain.
    python = python_executable.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ProvisioningArtifactError("Python build executable is unavailable")
    try:
        address = ipaddress.ip_address(target)
        network = ipaddress.ip_network(operator_cidr, strict=True)
    except ValueError as exc:
        raise ProvisioningArtifactError(f"invalid direct-link address: {exc}") from exc
    if address.version != 4 or network.version != 4 or address not in network:
        raise ProvisioningArtifactError("target must be inside the operator IPv4 CIDR")
    return {
        "output_root": str(output),
        "workspace_root": str(workspace),
        "enrollment": str(enrollment_path),
        "enrollment_id": enrollment_value["enrollment_id"],
        "runtime_token": str(token_path),
        "ssh_private_key": str(ssh_key),
        "known_hosts": str(host_keys),
        "target": str(address),
        "operator_cidr": str(network),
        "python_executable": str(python),
        "schema_root": str(schema_root.resolve()),
    }


def _run(argv: Sequence[str]) -> None:
    try:
        subprocess.run(
            list(argv),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stdout or "").strip()[-2000:]
        raise ProvisioningArtifactError(
            f"artifact command failed ({' '.join(argv[:4])}): {detail}"
        ) from exc


def build_receiver_wheelhouse(
    destination: Path, *, python_executable: Path, workspace_root: Path
) -> list[dict[str, Any]]:
    local = destination.parent / "local-wheels"
    local.mkdir(mode=0o700)
    _run(
        [
            str(python_executable),
            "-m",
            "pip",
            "wheel",
            "--use-pep517",
            "--wheel-dir",
            str(local),
            "--no-deps",
            *(str(workspace_root / relative) for relative in LOCAL_PROJECTS),
        ]
    )
    local_wheels = sorted(local.glob("*.whl"), key=lambda item: item.name)
    if len(local_wheels) != len(LOCAL_PROJECTS):
        raise ProvisioningArtifactError("local receiver wheel build is incomplete")
    destination.mkdir(mode=0o700)
    _run(
        [
            str(python_executable),
            "-m",
            "pip",
            "download",
            "--dest",
            str(destination),
            "--find-links",
            str(local),
            "--constraint",
            str(workspace_root / "deployment/ansible/gc-runtime-requirements.in"),
            "--platform",
            "manylinux2014_aarch64",
            "--python-version",
            "312",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--only-binary=:all:",
            *(str(path) for path in local_wheels),
        ]
    )
    shutil.rmtree(local)
    return write_receiver_requirements(destination)


def write_receiver_requirements(wheelhouse: Path) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    rows: list[tuple[str, str]] = []
    for wheel in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name):
        with zipfile.ZipFile(wheel) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(names) != 1:
                raise ProvisioningArtifactError(
                    f"wheel metadata is ambiguous: {wheel.name}"
                )
            metadata = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version:
            raise ProvisioningArtifactError(
                f"wheel identity is incomplete: {wheel.name}"
            )
        digest = _sha256(wheel)
        rows.append(
            (
                name.lower().replace("_", "-"),
                f"{name}=={version} --hash=sha256:{digest}",
            )
        )
        evidence.append(
            {"filename": wheel.name, "sha256": digest, "size": wheel.stat().st_size}
        )
    if not rows:
        raise ProvisioningArtifactError("receiver wheelhouse is empty")
    requirements = wheelhouse / "receiver-requirements.txt"
    requirements.write_text(
        "\n".join(row for _name, row in sorted(rows)) + "\n", encoding="utf-8"
    )
    requirements.chmod(0o600)
    return evidence


def _trust(
    root: Path, name: str, authority: str, registry: ContractRegistry
) -> tuple[Path, Path, str]:
    private_key = root / "private" / f"{name}.pem"
    descriptor_path = root / "public" / f"{name}.json"
    descriptor = generate_signer(
        private_key,
        descriptor_path,
        authority=authority,
        registry=registry,
    )
    store = validate_trusted_signers(
        {
            "schema_version": "1",
            "store_type": "iii.trusted-signers",
            "signers": [
                {
                    "signer_id": descriptor["signer_id"],
                    "algorithm": "Ed25519",
                    "authority": authority,
                    "public_key": descriptor["public_key"],
                    "state": "active",
                }
            ],
        },
        registry,
    )
    trust_path = root.parent / "trust" / f"{name}-signers.json"
    trust_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    trust_path.write_bytes(canonical_json(store) + b"\n")
    trust_path.chmod(0o600)
    return private_key, trust_path, descriptor["signer_id"]


def _receiver_payload(root: Path, workspace: Path, schema_root: Path) -> Path:
    payload = root / "receiver-payload"
    binary_root = payload / "bin"
    binary_root.mkdir(parents=True, mode=0o755)
    for command in (
        "iii-deployment-receiver",
        "iii-deployment-ssh-gateway",
        "iii-deploymentctl",
    ):
        path = binary_root / command
        path.write_text(
            f'#!/bin/sh\nexec /opt/iii/receiver/bootstrap/bin/{command} "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    shutil.copytree(schema_root, payload / "share/iii-deployment/schemas/v1")
    policy = payload / "share/iii-deployment/policy"
    policy.mkdir(parents=True)
    shutil.copy2(
        workspace / "deployment/portable-state-policy.json",
        policy / "portable-state-policy.json",
    )
    return payload


def materialize(
    inspection: Mapping[str, Any],
    *,
    operation_id: str,
    wheelhouse_builder: Callable[..., list[dict[str, Any]]] = build_receiver_wheelhouse,
) -> dict[str, Any]:
    output = Path(str(inspection["output_root"]))
    partial = output.with_name(f".{output.name}.partial-{uuid.uuid4().hex}")
    if partial.exists() or partial.is_symlink():
        raise ProvisioningArtifactError("provisioning artifact partial already exists")
    partial.mkdir(parents=True, mode=0o700)
    try:
        registry = ContractRegistry(Path(str(inspection["schema_root"])))
        wheelhouse = partial / "artifacts/receiver-wheelhouse"
        wheelhouse.parent.mkdir(parents=True, mode=0o700)
        wheel_evidence = wheelhouse_builder(
            wheelhouse,
            python_executable=Path(str(inspection["python_executable"])),
            workspace_root=Path(str(inspection["workspace_root"])),
        )
        signing = partial / "signing"
        signing.mkdir(mode=0o700)
        receiver_key, receiver_trust, receiver_signer = _trust(
            signing, "receiver-update", "receiver-update", registry
        )
        _bundle_key, bundle_trust, bundle_signer = _trust(
            signing, "bundle", "ci-qualified", registry
        )
        _status_key, status_trust, status_signer = _trust(
            signing, "release-status", "release-status", registry
        )
        payload = _receiver_payload(
            partial,
            Path(str(inspection["workspace_root"])),
            Path(str(inspection["schema_root"])),
        )
        bundle = partial / "artifacts/receiver-bundle"
        manifest = package_receiver_update(
            payload,
            bundle,
            generation=1,
            version="v1.0.0",
            compatibility=COMPATIBILITY,
            private_key_path=receiver_key,
            registry=registry,
        )
        verify_receiver_update(bundle, trust=receiver_trust, registry=registry)
        shutil.rmtree(payload)
        enrollment = partial / "access/provisioning-enrollment.json"
        enrollment.parent.mkdir(parents=True, mode=0o700)
        shutil.copy2(Path(str(inspection["enrollment"])), enrollment)
        enrollment.chmod(0o600)
        token = (
            Path(str(inspection["runtime_token"])).read_text(encoding="ascii").strip()
        )
        secret = partial / "secrets/runtime-api.env"
        secret.parent.mkdir(parents=True, mode=0o700)
        secret.write_text(
            f"III_RUNTIME_API_BROWSER_PASSWORD={token}\n", encoding="ascii"
        )
        secret.chmod(0o600)
        inputs = {
            "schema": "iii.host-provisioning-input/v1",
            "target_class": "raspberry-pi-5-noble-arm64",
            "logical_target": "drone",
            "profile": "real",
            "operator_cidr": inspection["operator_cidr"],
            "receiver_bundle_source": "artifacts/receiver-bundle",
            "receiver_wheelhouse_source": "artifacts/receiver-wheelhouse",
            "bundle_trust_source": "trust/bundle-signers.json",
            "release_status_trust_source": "trust/release-status-signers.json",
            "receiver_update_trust_source": "trust/receiver-update-signers.json",
            "operator_enrollment_source": "access/provisioning-enrollment.json",
            "runtime_api_secret_source": "secrets/runtime-api.env",
            "offline": False,
        }
        input_path = partial / "inputs.json"
        input_path.write_bytes(canonical_json(inputs) + b"\n")
        input_path.chmod(0o600)
        inventory = {
            "all": {
                "children": {
                    "aircraft": {
                        "hosts": {
                            inspection["target"]: {
                                "ansible_user": "iii-bootstrap",
                                "ansible_ssh_private_key_file": inspection[
                                    "ssh_private_key"
                                ],
                                "ansible_ssh_common_args": (
                                    "-o BatchMode=yes -o StrictHostKeyChecking=yes "
                                    f"-o UserKnownHostsFile={inspection['known_hosts']}"
                                ),
                            }
                        }
                    }
                }
            }
        }
        inventory_path = partial / "inventory.json"
        inventory_path.write_bytes(canonical_json(inventory) + b"\n")
        inventory_path.chmod(0o600)
        for path in partial.rglob("*"):
            if path.is_symlink():
                raise ProvisioningArtifactError(
                    f"materialized provisioning artifact is linked: {path}"
                )
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
            else:
                raise ProvisioningArtifactError(
                    f"materialized provisioning artifact has unsafe type: {path}"
                )
        load_input(
            partial / "inputs.json", schema_root=Path(str(inspection["schema_root"]))
        )
        record: dict[str, Any] = {
            "schema": RECORD_SCHEMA,
            "record_id": "0" * 64,
            "operation_id": operation_id,
            "recorded_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "target": inspection["target"],
            "operator_cidr": inspection["operator_cidr"],
            "enrollment_id": inspection["enrollment_id"],
            "receiver_id": manifest["receiver_id"],
            "signer_ids": {
                "bundle": bundle_signer,
                "release_status": status_signer,
                "receiver_update": receiver_signer,
            },
            "wheels": wheel_evidence,
            "inputs_sha256": _sha256(partial / "inputs.json"),
            "inventory_sha256": _sha256(partial / "inventory.json"),
        }
        record["record_id"] = content_identity(
            {key: value for key, value in record.items() if key != "record_id"}
        )
        registry.validate("host-provisioning-artifacts", record)
        record_path = partial / "artifact-record.json"
        record_path.write_bytes(canonical_json(record) + b"\n")
        record_path.chmod(0o600)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(partial, output)
        return {
            **record,
            "output_root": str(output),
            "record_path": str(output / "artifact-record.json"),
        }
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
