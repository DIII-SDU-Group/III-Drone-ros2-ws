from __future__ import annotations

import json
from pathlib import Path
import subprocess
import base64
import hashlib
import struct

import pytest

from iii_deployment.host_provision import (
    HostProvisionChangedError,
    HostProvisionDriftError,
    HostProvisionError,
    apply_plan,
    build_plan,
    check_plan,
)
from iii_deployment.contracts import ContractRegistry, canonical_json
from iii_deployment.identity import create_machine_enrollment


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "deployment-infrastructure-redesign"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    (path / "tracked").write_text("fixture\n")
    subprocess.run(["git", "add", "tracked"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


def _fixture(tmp_path: Path) -> dict[str, Path]:
    artifacts = tmp_path / "artifacts"
    bundle = artifacts / "receiver"
    wheelhouse = artifacts / "wheels"
    bundle.mkdir(parents=True)
    wheelhouse.mkdir()
    (bundle / "receiver-update.manifest.json").write_text("{}\n")
    (wheelhouse / "receiver-requirements.txt").write_text(
        "fixture --hash=sha256:" + "0" * 64 + "\n"
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    fields = [
        "bundle_trust_source",
        "release_status_trust_source",
        "receiver_update_trust_source",
        "operator_enrollment_source",
        "maintenance_ssh_public_key_source",
        "runtime_api_secret_source",
    ]
    source_paths = {}
    for field in fields:
        path = sources / field
        path.write_text(
            "III_RUNTIME_API_BROWSER_PASSWORD=target-browser-secret\n"
            if field == "runtime_api_secret_source"
            else "fixture\n"
        )
        path.chmod(0o600)
        source_paths[field] = str(path)
    ssh_blob = (
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"o" * 32
    )
    signer = b"s" * 32
    enrollment = create_machine_enrollment(
        label="host-plan",
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
        registry=ContractRegistry(Path(__file__).parents[1] / "schemas/v1"),
    )
    Path(source_paths["operator_enrollment_source"]).write_bytes(
        canonical_json(enrollment) + b"\n"
    )
    Path(source_paths["maintenance_ssh_public_key_source"]).write_text(
        "ssh-ed25519 " + base64.b64encode(ssh_blob).decode("ascii") + "\n",
        encoding="ascii",
    )
    inputs = tmp_path / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "schema": "iii.host-provisioning-input/v1",
                "target_class": "raspberry-pi-5-noble-arm64",
                "logical_target": "drone",
                "profile": "real",
                "operator_cidr": "192.168.10.0/24",
                "receiver_bundle_source": str(bundle),
                "receiver_wheelhouse_source": str(wheelhouse),
                **source_paths,
            }
        )
        + "\n"
    )
    inputs.chmod(0o600)
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all:\n  hosts:\n    iii.local:\n")
    ansible = tmp_path / "ansible"
    (ansible / "playbooks").mkdir(parents=True)
    (ansible / "playbooks/aircraft-converge.yml").write_text("---\n")
    (ansible / "playbooks/aircraft-finalize.yml").write_text("---\n")
    executable = tmp_path / "ansible-playbook"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return {
        "inputs": inputs,
        "inventory": inventory,
        "ansible": ansible,
        "workspace": _git_repo(tmp_path / "workspace"),
        "cli": _git_repo(tmp_path / "cli"),
        "executable": executable,
        "source": Path(source_paths["runtime_api_secret_source"]),
    }


def _plan(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    fixture = _fixture(tmp_path)
    plan = build_plan(
        operation_id="iii-host-provision-test",
        target="iii.local",
        inventory=fixture["inventory"],
        input_path=fixture["inputs"],
        schema_root=Path(__file__).parents[1] / "schemas/v1",
        ansible_root=fixture["ansible"],
        workspace_root=fixture["workspace"],
        cli_root=fixture["cli"],
        ansible_playbook=fixture["executable"],
    )
    return plan, fixture


def test_plan_binds_repositories_permissions_and_every_input(tmp_path: Path) -> None:
    plan, _fixture_paths = _plan(tmp_path)

    assert plan["schema"] == "iii.host-provisioning-plan/v1"
    assert plan["target"] == "iii.local"
    assert len(plan["repositories"]) == 2
    assert all(row["old_sha"] == row["new_sha"] for row in plan["repositories"])
    assert "bootstrap-user-removal" in plan["declared_permissions"]
    assert "maintenance-account-full-sudo" in plan["declared_permissions"]
    assert "maintenance_ssh_public_key_source" in plan["controller_inputs"]
    assert set(plan["artifacts"]) == {
        "receiver_bundle_source",
        "receiver_wheelhouse_source",
    }
    assert len(plan["content_id"]) == 64


def test_check_refuses_input_changed_after_retention(
    monkeypatch, tmp_path: Path
) -> None:
    plan, fixture = _plan(tmp_path)
    fixture["source"].write_text(
        "III_RUNTIME_API_BROWSER_PASSWORD=changed-browser-secret\n"
    )

    with pytest.raises(HostProvisionChangedError, match="runtime_api_secret_source"):
        check_plan(plan, schema_root=Path(__file__).parents[1] / "schemas/v1")


def test_apply_runs_converge_check_finalize_in_order(
    monkeypatch, tmp_path: Path
) -> None:
    import iii_deployment.host_provision as provision

    plan, _fixture_paths = _plan(tmp_path)
    calls: list[tuple[str, bool]] = []

    def run(**kwargs):
        calls.append((kwargs["playbook"], kwargs["check"]))
        counters = {
            "ok": 1,
            "changed": 0,
            "failures": 0,
            "unreachable": 0,
            "skipped": 0,
            "rescued": 0,
            "ignored": 0,
        }
        return {
            "schema": "iii.ansible-run-result/v1",
            "check_mode": kwargs["check"],
            "hosts": {"iii.local": dict(counters)},
            "totals": counters,
            "categories": {"operational": dict(counters)},
        }

    monkeypatch.setattr(provision, "_run_ansible", run)
    report = apply_plan(plan, schema_root=Path(__file__).parents[1] / "schemas/v1")

    assert calls == [
        ("aircraft-converge.yml", False),
        ("aircraft-converge.yml", True),
        ("aircraft-finalize.yml", False),
    ]
    assert report["state"] == "provisioned"
    assert set(report["runs"]["first_convergence"]["categories"]) == {"operational"}
    assert len(report["report_id"]) == 64


def test_apply_refuses_nonzero_second_run_drift(monkeypatch, tmp_path: Path) -> None:
    import iii_deployment.host_provision as provision

    plan, _fixture_paths = _plan(tmp_path)

    def run(**kwargs):
        changed = 2 if kwargs["check"] else 4
        counters = {
            "ok": 1,
            "changed": changed,
            "failures": 0,
            "unreachable": 0,
            "skipped": 0,
            "rescued": 0,
            "ignored": 0,
        }
        return {
            "schema": "iii.ansible-run-result/v1",
            "check_mode": kwargs["check"],
            "hosts": {"iii.local": dict(counters)},
            "totals": counters,
        }

    monkeypatch.setattr(provision, "_run_ansible", run)
    with pytest.raises(HostProvisionDriftError, match="2 unintended"):
        apply_plan(plan, schema_root=Path(__file__).parents[1] / "schemas/v1")


def test_input_permissions_and_artifact_symlinks_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["inputs"].chmod(0o644)
    with pytest.raises(HostProvisionError, match="owner-only"):
        build_plan(
            operation_id="iii-host-provision-test",
            target="iii.local",
            inventory=fixture["inventory"],
            input_path=fixture["inputs"],
            schema_root=Path(__file__).parents[1] / "schemas/v1",
            ansible_root=fixture["ansible"],
            workspace_root=fixture["workspace"],
            cli_root=fixture["cli"],
            ansible_playbook=fixture["executable"],
        )

    fixture["inputs"].chmod(0o600)
    (tmp_path / "artifacts/receiver/escape").symlink_to("/etc/passwd")
    with pytest.raises(HostProvisionError, match="symbolic link"):
        build_plan(
            operation_id="iii-host-provision-test",
            target="iii.local",
            inventory=fixture["inventory"],
            input_path=fixture["inputs"],
            schema_root=Path(__file__).parents[1] / "schemas/v1",
            ansible_root=fixture["ansible"],
            workspace_root=fixture["workspace"],
            cli_root=fixture["cli"],
            ansible_playbook=fixture["executable"],
        )
