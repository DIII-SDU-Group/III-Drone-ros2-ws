from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct

import pytest

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.receiver.access import AccessManager
from iii_deployment.receiver.host_finalize import finalize_host
from iii_deployment.signers import generate_signer

BASELINE_ID = "a" * 64
RECEIVER_ID = "b" * 64
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"
SSH_WIRE_KEY = (
    struct.pack(">I", len(b"ssh-ed25519"))
    + b"ssh-ed25519"
    + struct.pack(">I", 32)
    + b"k" * 32
)
SSH_PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(SSH_WIRE_KEY).decode("ascii")
CLIENT_ID = hashlib.sha256(SSH_PUBLIC_KEY.encode("ascii")).hexdigest()


def _write(path: Path, value: object | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_bytes(canonical_json(value) + b"\n")


def _root(tmp_path: Path) -> Path:
    runtime_uid = os.getuid() or 1100
    runtime_gid = os.getgid() or 1100
    health = {
        "schema": "iii.host-baseline-report/v1",
        "state": "converged",
        "baseline_id": BASELINE_ID,
        "target_definition_id": "d" * 64,
        "shared_target_profile_id": "e" * 64,
        "logical_target": "drone",
        "profile": "real",
        "receiver": {"receiver_id": RECEIVER_ID, "generation": 1},
    }
    readiness = {
        "schema": "iii.receiver-readiness/v1",
        "receiver_id": RECEIVER_ID,
        "generation": 1,
        "socket_open": True,
        "self_tests_passed": True,
    }
    signer_descriptor = generate_signer(
        tmp_path / "field-key.pem",
        tmp_path / "field-public.json",
        authority="workstation-field",
        registry=ContractRegistry(SCHEMAS),
    )
    enrollment = create_machine_enrollment(
        label="bootstrap-controller",
        ssh_public_key=SSH_PUBLIC_KEY,
        runtime_token="R" * 43,
        field_signer_descriptor=signer_descriptor,
        registry=ContractRegistry(SCHEMAS),
    )
    _write(tmp_path / "var/lib/iii/deployment/host-baseline-report.json", health)
    _write(tmp_path / "run/iii/receiver-readiness.json", readiness)
    _write(
        tmp_path / "etc/iii/deployment-receiver.json",
        {
            "schema": "iii.receiver-config/v1",
            "receiver_generation": 1,
            "logical_target": "drone",
            "profile": "real",
            "runtime_uid": runtime_uid,
            "runtime_gid": runtime_gid,
        },
    )
    AccessManager(
        state_path=tmp_path / "var/lib/iii/deployment/access-state.json",
        authorized_keys_path=tmp_path / "home/iii/.ssh/authorized_keys",
        registry=ContractRegistry(SCHEMAS),
        runtime_verifiers_path=(
            tmp_path / "var/lib/iii/deployment/runtime-api-client-verifiers.json"
        ),
        field_signers_path=(
            tmp_path / "var/lib/iii/deployment/workstation-field-signers.json"
        ),
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
    ).bootstrap([enrollment])
    slots = tmp_path / "opt/iii/receiver/slots"
    gateway = slots / "a/bin/iii-deployment-ssh-gateway"
    gateway.parent.mkdir(parents=True)
    gateway.write_text("#!/bin/sh\nexit 0\n")
    gateway.chmod(0o555)
    selectors = tmp_path / "opt/iii/receiver/selectors"
    selectors.mkdir(parents=True)
    (selectors / "current").symlink_to("../slots/a")
    (selectors / "fallback").symlink_to("../slots/a")
    system_gateway = tmp_path / "usr/bin/iii-deployment-ssh-gateway"
    system_gateway.parent.mkdir(parents=True)
    system_gateway.symlink_to(
        "/opt/iii/receiver/selectors/current/bin/iii-deployment-ssh-gateway"
    )
    _write(tmp_path / "etc/netplan/50-cloud-init.yaml", b"network:\n  version: 2\n")
    _write(tmp_path / "boot/firmware/user-data", b"secret\n")
    _write(tmp_path / "var/lib/cloud/instances/iid/user-data.txt", b"secret\n")
    _write(
        tmp_path / "etc/sudoers.d/90-cloud-init-users",
        b"iii-bootstrap ALL=(ALL) NOPASSWD:ALL\n",
    )
    return tmp_path


def test_finalize_preserves_network_revokes_bootstrap_and_is_resumable(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    present = {"value": True}
    calls: list[tuple[str, ...]] = []

    def run(argv: object) -> None:
        command = tuple(argv)  # type: ignore[arg-type]
        calls.append(command)
        if command[0].endswith("userdel"):
            present["value"] = False

    result = finalize_host(
        baseline_id=BASELINE_ID,
        root=root,
        run=run,
        user_exists=lambda _name: present["value"],
    )

    assert result["state"] == "provisioned"
    assert result["commissioned"] is False
    assert (
        root / "etc/netplan/90-iii-operator.yaml"
    ).read_text() == "network:\n  version: 2\n"
    assert not (root / "etc/netplan/50-cloud-init.yaml").exists()
    assert not (root / "boot/firmware/user-data").exists()
    assert not (root / "var/lib/cloud/instances").exists()
    assert not (root / "etc/sudoers.d/90-cloud-init-users").exists()
    assert (root / "etc/cloud/cloud-init.disabled").is_file()
    assert any(command[0].endswith("userdel") for command in calls)
    report = json.loads(
        (root / "var/lib/iii/deployment/host-provisioning-report.json").read_text()
    )
    assert report["report_id"] == result["report_id"]

    repeated = finalize_host(
        baseline_id=BASELINE_ID,
        root=root,
        run=run,
        user_exists=lambda _name: False,
    )
    assert repeated == result


def test_finalize_refuses_missing_permanent_network_before_sanitization(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / "etc/netplan/50-cloud-init.yaml").unlink()

    with pytest.raises(ContractError, match="no cloud-init network"):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=lambda _argv: None,
            user_exists=lambda _name: True,
        )

    assert (root / "boot/firmware/user-data").is_file()


def test_finalize_refuses_pending_access_and_preserves_bootstrap(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path = root / "var/lib/iii/deployment/access-state.json"
    access = json.loads(path.read_text())
    access["clients"][CLIENT_ID]["state"] = "pending"
    access["access_id"] = content_identity(
        {key: value for key, value in access.items() if key != "access_id"}
    )
    _write(path, access)

    with pytest.raises(ContractError, match="absent, pending, or revoked"):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=lambda _argv: None,
            user_exists=lambda _name: True,
        )

    assert (root / "boot/firmware/user-data").is_file()


def test_finalize_refuses_unreadable_authorized_keys_ownership_before_revocation(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    config_path = root / "etc/iii/deployment-receiver.json"
    config = json.loads(config_path.read_text())
    config["runtime_uid"] = os.getuid() + 1
    _write(config_path, config)

    with pytest.raises(ContractError, match="ownership is not SSH-readable"):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=lambda _argv: None,
            user_exists=lambda _name: True,
        )

    assert (root / "boot/firmware/user-data").is_file()


def test_finalize_refuses_runtime_inaccessible_gateway_before_revocation(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    slot = root / "opt/iii/receiver/slots/a"
    original_mode = slot.stat().st_mode & 0o777
    slot.chmod(0o000)

    try:
        with pytest.raises(ContractError, match="gateway is not runtime-executable"):
            finalize_host(
                baseline_id=BASELINE_ID,
                root=root,
                run=lambda _argv: None,
                user_exists=lambda _name: True,
            )
    finally:
        slot.chmod(original_mode)

    assert (root / "boot/firmware/user-data").is_file()


@pytest.mark.parametrize(
    ("relative", "field", "value", "message"),
    [
        (
            "runtime-api-client-verifiers.json",
            "generation",
            99,
            "Runtime API machine verifier projection",
        ),
        (
            "workstation-field-signers.json",
            "store_type",
            "iii.tampered-signers",
            "field signer trust projection",
        ),
    ],
)
def test_finalize_refuses_access_projection_drift_before_sanitization(
    tmp_path: Path,
    relative: str,
    field: str,
    value: object,
    message: str,
) -> None:
    root = _root(tmp_path)
    path = root / "var/lib/iii/deployment" / relative
    projection = json.loads(path.read_text())
    projection[field] = value
    _write(path, projection)

    with pytest.raises(ContractError, match=message):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=lambda _argv: None,
            user_exists=lambda _name: True,
        )

    assert (root / "boot/firmware/user-data").is_file()


def test_finalize_refuses_selector_escape(tmp_path: Path) -> None:
    root = _root(tmp_path)
    selector = root / "opt/iii/receiver/selectors/current"
    selector.unlink()
    selector.symlink_to("/tmp")

    with pytest.raises(ContractError, match="selector escapes"):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=lambda _argv: None,
            user_exists=lambda _name: True,
        )


def test_finalize_accepts_userdel_warning_only_after_account_is_absent(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    present = {"value": True}

    def removed_with_warning(argv: object) -> None:
        command = tuple(argv)  # type: ignore[arg-type]
        if command[0].endswith("userdel"):
            present["value"] = False
            raise subprocess.CalledProcessError(8, command)

    result = finalize_host(
        baseline_id=BASELINE_ID,
        root=root,
        run=removed_with_warning,
        user_exists=lambda _name: present["value"],
    )
    assert result["bootstrap_user_removed"] is True


def test_finalize_refuses_userdel_failure_when_account_survives(tmp_path: Path) -> None:
    root = _root(tmp_path)

    def failed_removal(argv: object) -> None:
        command = tuple(argv)  # type: ignore[arg-type]
        if command[0].endswith("userdel"):
            raise subprocess.CalledProcessError(8, command)

    with pytest.raises(subprocess.CalledProcessError):
        finalize_host(
            baseline_id=BASELINE_ID,
            root=root,
            run=failed_removal,
            user_exists=lambda _name: True,
        )
