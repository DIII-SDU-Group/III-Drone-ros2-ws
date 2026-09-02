from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import pytest

from iii_deployment.contracts import ContractRegistry, canonical_json
from iii_deployment.host_provision import (
    HostProvisionError,
    _run_ansible,
    build_plan,
)
from iii_deployment.identity import create_machine_enrollment
from iii_deployment.provisioning_artifacts import _receiver_payload
from iii_deployment.receiver.update import package_receiver_update
from iii_deployment.signers import generate_signer

pytestmark = pytest.mark.target
SCHEMAS = Path(__file__).parents[1] / "schemas/v1"
WORKSPACE = Path(__file__).parents[2]
ANSIBLE_ROOT = WORKSPACE / "deployment/ansible"
CLI_ROOT = WORKSPACE / "tools/III-Drone-CLI"
RECEIVER_WHEEL_PROJECTS = (
    ("III-Drone-Contracts", WORKSPACE / "src/III-Drone-Contracts"),
    ("III-Drone-Configuration", WORKSPACE / "src/III-Drone-Configuration"),
    ("III-Drone-CLI", CLI_ROOT),
    ("deployment", WORKSPACE / "deployment"),
)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, text=True, **kwargs)


def _trust(root: Path, name: str, authority: str) -> tuple[Path, Path]:
    key = root / f"{name}.pem"
    descriptor_path = root / f"{name}-public.json"
    descriptor = generate_signer(
        key,
        descriptor_path,
        authority=authority,
        registry=ContractRegistry(SCHEMAS),
    )
    store = {
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
    }
    trust = root / f"{name}-trust.json"
    trust.write_bytes(canonical_json(store) + b"\n")
    trust.chmod(0o600)
    return key, trust


def _wheel_requirements(wheelhouse: Path) -> None:
    rows = []
    for wheel in sorted(wheelhouse.glob("*.whl"), key=lambda item: item.name):
        with zipfile.ZipFile(wheel) as archive:
            metadata_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            metadata = archive.read(metadata_name).decode("utf-8")
        fields = {}
        for line in metadata.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields.setdefault(key, value)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        rows.append(f"{fields['Name']}=={fields['Version']} --hash=sha256:{digest}")
    (wheelhouse / "receiver-requirements.txt").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )


def _receiver_artifacts(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    source = root / "wheel-source"
    ignored = shutil.ignore_patterns(
        ".git", ".pytest_cache", "__pycache__", "*.egg-info", "*.pyc"
    )
    for name, project_root in RECEIVER_WHEEL_PROJECTS:
        shutil.copytree(project_root, source / name, ignore=ignored)
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--volume",
            f"{source}:/src",
            "--volume",
            f"{wheelhouse}:/out",
            "python:3.12.3-slim-bookworm",
            "python",
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            "/out",
            *(f"/src/{name}" for name, _project_root in RECEIVER_WHEEL_PROJECTS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _wheel_requirements(wheelhouse)

    receiver_key, receiver_trust = _trust(root, "receiver", "receiver-update")
    _bundle_key, bundle_trust = _trust(root, "bundle", "ci-qualified")
    _status_key, status_trust = _trust(root, "status", "release-status")
    payload = _receiver_payload(root, WORKSPACE, SCHEMAS, wheelhouse)
    compatibility = {
        "bootstrap_protocols": ["1"],
        "cli_protocols": ["1"],
        "request_protocols": ["1"],
        "release_manifest_schema_versions": ["1"],
        "journal_schemas": ["iii.receiver-operation-journal/v1"],
        "audit_schemas": ["iii.receiver-audit/v1"],
        "activation_transaction_schemas": ["iii.activation-transaction/v1"],
        "activation_selector_schemas": ["iii.activation-selector/v1"],
        "activation_health_transaction_schemas": [
            "iii.activation-health-transaction/v1"
        ],
        "activation_health_evidence_schemas": ["iii.activation-health/v1"],
        "upload_manifest_schemas": ["iii.bundle-upload/v1"],
        "upload_activity_schemas": ["iii.bundle-upload-activity/v1"],
        "configuration_checkpoint_schemas": ["iii.configuration-checkpoint/v1"],
    }
    bundle = root / "receiver-bundle"
    package_receiver_update(
        payload,
        bundle,
        generation=1,
        version="v1.0.0",
        compatibility=compatibility,
        private_key_path=receiver_key,
        registry=ContractRegistry(SCHEMAS),
    )
    return bundle, wheelhouse, bundle_trust, status_trust, receiver_trust


def test_receiver_wheel_sources_cover_local_distribution_graph() -> None:
    assert tuple(name for name, _root in RECEIVER_WHEEL_PROJECTS) == (
        "III-Drone-Contracts",
        "III-Drone-Configuration",
        "III-Drone-CLI",
        "deployment",
    )
    for _name, project_root in RECEIVER_WHEEL_PROJECTS:
        assert (project_root / "setup.py").is_file() or (
            project_root / "pyproject.toml"
        ).is_file()


def test_receiver_payload_policy_source_is_regular_file() -> None:
    policy = WORKSPACE / "deployment/portable-state-policy.json"
    assert policy.is_file()
    assert not policy.is_symlink()


def _wait_for_ssh(port: str, key: Path) -> None:
    for _attempt in range(60):
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=1",
                "-i",
                str(key),
                "-p",
                port,
                "iii-bootstrap@127.0.0.1",
                "true",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise AssertionError("target-equivalent container SSH did not become ready")


def _wait_for_systemd(container: str) -> None:
    for _attempt in range(60):
        result = subprocess.run(
            ["docker", "exec", container, "test", "-S", "/run/systemd/private"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise AssertionError("target-equivalent container systemd bus did not become ready")


@pytest.mark.skipif(
    os.environ.get("III_RUN_ANSIBLE_TARGET_TEST") != "1",
    reason="set III_RUN_ANSIBLE_TARGET_TEST=1 for target-equivalent systemd convergence",
)
def test_noble_systemd_first_second_drift_repair_and_finalization(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    architecture = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "ubuntu:24.04",
            "sh",
            "-c",
            'test "$(uname -m)" = aarch64 && test "$(dpkg --print-architecture)" = arm64',
        ],
    )
    assert architecture.returncode == 0
    image = "iii-ansible-target-test:24.04-amd64"
    _run(
        [
            "docker",
            "build",
            "--tag",
            image,
            str(ANSIBLE_ROOT / "tests/target"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    ssh_key = tmp_path / "bootstrap"
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_key)])
    maintenance_key = tmp_path / "maintenance"
    _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(maintenance_key)])
    maintenance_key.with_suffix(".pub").chmod(0o600)
    public_key = " ".join((ssh_key.with_suffix(".pub")).read_text().split()[:2])
    field_key = tmp_path / "field.pem"
    field_descriptor_path = tmp_path / "field-public.json"
    field_descriptor = generate_signer(
        field_key,
        field_descriptor_path,
        authority="workstation-field",
        registry=ContractRegistry(SCHEMAS),
    )
    operator_enrollment = tmp_path / "operator-enrollment.json"
    operator_enrollment.write_bytes(
        canonical_json(
            create_machine_enrollment(
                label="target-convergence-controller",
                ssh_public_key=public_key,
                runtime_token="T" * 43,
                field_signer_descriptor=field_descriptor,
                registry=ContractRegistry(SCHEMAS),
            )
        )
        + b"\n"
    )
    operator_enrollment.chmod(0o600)
    runtime_secret = tmp_path / "runtime-api.env"
    runtime_secret.write_text(
        "III_RUNTIME_API_BROWSER_PASSWORD=target-browser-secret\n"
    )
    runtime_secret.chmod(0o600)
    bundle, wheelhouse, bundle_trust, status_trust, receiver_trust = (
        _receiver_artifacts(tmp_path)
    )

    container = "iii-ansible-target-" + os.urandom(6).hex()
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--privileged",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "--publish",
                "127.0.0.1::22",
                "--hostname",
                "iii",
                "--name",
                container,
                image,
            ],
            stdout=subprocess.PIPE,
        )
        port = (
            _run(["docker", "port", container, "22/tcp"], stdout=subprocess.PIPE)
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        _wait_for_systemd(container)
        _run(
            [
                "docker",
                "cp",
                str(ssh_key.with_suffix(".pub")),
                f"{container}:/tmp/bootstrap.pub",
            ]
        )
        setup = (
            "useradd --create-home --shell /bin/bash iii-bootstrap; "
            "install -d -m 0700 -o iii-bootstrap -g iii-bootstrap /home/iii-bootstrap/.ssh; "
            "awk '{print $1, $2}' /tmp/bootstrap.pub > /home/iii-bootstrap/.ssh/authorized_keys; "
            "chown iii-bootstrap:iii-bootstrap /home/iii-bootstrap/.ssh/authorized_keys; "
            "chmod 0600 /home/iii-bootstrap/.ssh/authorized_keys; "
            "printf '%s\\n' 'iii-bootstrap ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-cloud-init-users; "
            "chmod 0440 /etc/sudoers.d/90-cloud-init-users; "
            "mkdir -p /etc/iii-bootstrap /var/lib/iii/bootstrap /boot/firmware; "
            "printf '%s\\n' 'bootstrap-secret' > /boot/firmware/user-data; "
            "printf '%s\\n' 'instance-id: target-test' > /boot/firmware/meta-data; "
            "mkdir -p /etc/netplan; "
            "printf '%s\\n' 'network:' '  version: 2' '  ethernets:' '    eth0:' '      dhcp4: true' > /etc/netplan/50-cloud-init.yaml; "
            "install -d -m 0755 /run/sshd; "
            "systemctl restart ssh.service"
        )
        _run(["docker", "exec", container, "bash", "-lc", setup])
        _wait_for_ssh(port, ssh_key)

        inventory = tmp_path / "inventory.yml"
        inventory.write_text(
            "all:\n  children:\n    aircraft:\n      hosts:\n        target:\n"
            "          ansible_host: 127.0.0.1\n"
            f"          ansible_port: {port}\n"
            "          ansible_user: iii-bootstrap\n"
            f"          ansible_ssh_private_key_file: {ssh_key}\n"
            "          ansible_ssh_common_args: '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'\n"
        )
        inputs = tmp_path / "inputs.json"
        inputs.write_text(
            json.dumps(
                {
                    "schema": "iii.host-provisioning-input/v1",
                    "target_class": "raspberry-pi-5-noble-arm64",
                    "logical_target": "drone",
                    "profile": "real",
                    "operator_cidr": "172.16.0.0/12",
                    "receiver_bundle_source": str(bundle),
                    "receiver_wheelhouse_source": str(wheelhouse),
                    "bundle_trust_source": str(bundle_trust),
                    "release_status_trust_source": str(status_trust),
                    "receiver_update_trust_source": str(receiver_trust),
                    "operator_enrollment_source": str(operator_enrollment),
                    "maintenance_ssh_public_key_source": str(
                        maintenance_key.with_suffix(".pub")
                    ),
                    "runtime_api_secret_source": str(runtime_secret),
                },
                sort_keys=True,
            )
            + "\n"
        )
        inputs.chmod(0o600)
        plan = build_plan(
            operation_id="iii-target-convergence-test",
            target="target",
            inventory=inventory,
            input_path=inputs,
            schema_root=SCHEMAS,
            ansible_root=ANSIBLE_ROOT,
            workspace_root=WORKSPACE,
            cli_root=CLI_ROOT,
            ansible_playbook=WORKSPACE / "testing/ansible-venv/bin/ansible-playbook",
        )

        from iii_deployment.host_provision import _verify_plan

        authenticated = _verify_plan(plan, schema_root=SCHEMAS)
        try:
            _run_ansible(
                plan=plan,
                values=authenticated,
                playbook="aircraft-converge-target-equivalent.yml",
                check=False,
            )
        except HostProvisionError as exc:
            diagnostics = _run(
                [
                    "docker",
                    "exec",
                    container,
                    "journalctl",
                    "--no-pager",
                    "--output=short-monotonic",
                    "--unit=iii-receiver-bootstrap-reconcile.service",
                    "--unit=iii-deployment-receiver-reconcile.service",
                    "--unit=iii-deployment-receiver.service",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).stdout
            raise AssertionError(
                f"{exc}\ntarget receiver journal:\n{diagnostics}"
            ) from exc
        second = _run_ansible(
            plan=plan,
            values=authenticated,
            playbook="aircraft-converge-target-equivalent.yml",
            check=True,
        )
        assert second["totals"]["changed"] == 0

        _run(
            [
                "docker",
                "exec",
                container,
                "bash",
                "-lc",
                "printf 'Europe/Copenhagen\\n' > /etc/timezone",
            ]
        )
        drift = _run_ansible(
            plan=plan,
            values=authenticated,
            playbook="aircraft-converge-target-equivalent.yml",
            check=True,
        )
        assert drift["totals"]["changed"] > 0
        _run_ansible(
            plan=plan,
            values=authenticated,
            playbook="aircraft-converge-target-equivalent.yml",
            check=False,
        )
        repaired = _run_ansible(
            plan=plan,
            values=authenticated,
            playbook="aircraft-converge-target-equivalent.yml",
            check=True,
        )
        assert repaired["totals"]["changed"] == 0

        try:
            finalized = _run_ansible(
                plan=plan,
                values=authenticated,
                playbook="aircraft-finalize-target-equivalent.yml",
                check=False,
            )
        except HostProvisionError as exc:
            diagnostics = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "bash",
                    "-lc",
                    "getent passwd iii-bootstrap || true; "
                    "ls -ld /home/iii-bootstrap /etc/netplan/50-cloud-init.yaml "
                    "/etc/netplan/90-iii-operator.yaml "
                    "/var/lib/iii/deployment/host-provisioning-report.json 2>&1 || true; "
                    "test -f /var/lib/iii/deployment/host-provisioning-report.json "
                    "&& cat /var/lib/iii/deployment/host-provisioning-report.json || true",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).stdout
            raise AssertionError(
                f"{exc}\ntarget finalization state:\n{diagnostics}"
            ) from exc
        assert finalized["totals"]["failures"] == 0
        report = json.loads(
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "cat",
                    "/var/lib/iii/deployment/host-provisioning-report.json",
                ],
                stdout=subprocess.PIPE,
            ).stdout
        )
        assert report["state"] == "provisioned"
        assert report["bootstrap_user_removed"] is True
        assert report["commissioned"] is False
        assert report["maintenance_access"]["user"] == "iii-maint"
        permanent_access = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "LogLevel=ERROR",
                "-i",
                str(ssh_key),
                "-p",
                port,
                "iii@127.0.0.1",
                "true",
            ],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert permanent_access.returncode == 2, permanent_access.stdout
        assert (
            "SSH command is outside the fixed deployment gateway"
            in permanent_access.stdout
        )
        maintenance_access = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "LogLevel=ERROR",
                "-i",
                str(maintenance_key),
                "-p",
                port,
                "iii-maint@127.0.0.1",
                "sudo -n id -u",
            ],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert maintenance_access.returncode == 0, maintenance_access.stdout
        assert maintenance_access.stdout.splitlines()[-1] == "0"
        assert (
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "test",
                    "!",
                    "-e",
                    "/boot/firmware/user-data",
                ]
            ).returncode
            == 0
        )
        assert (
            _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    "linux/arm64",
                    "ubuntu:24.04",
                    "sh",
                    "-c",
                    'test "$(uname -m)" = aarch64',
                ]
            ).returncode
            == 0
        )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
