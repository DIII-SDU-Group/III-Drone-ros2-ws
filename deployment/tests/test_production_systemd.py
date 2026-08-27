from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from iii_deployment.contracts import ContractRegistry, canonical_json, content_identity


ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deployment/systemd"
LAUNCHER = ROOT / "deployment/host/iii-release-launch"
UNITS = (
    "iii-runtime-api.service",
    "iii-system-daemon.service",
    "iii.target",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def test_production_units_are_real_profile_host_owned_and_independently_stoppable():
    combined = "\n".join((SYSTEMD / name).read_text() for name in UNITS)
    for forbidden in (
        "/home/iii/ws",
        "setup_dev.bash",
        "PROFILE=sim",
        "dev-password",
        "dev-cli-token",
    ):
        assert forbidden not in combined
    daemon = (SYSTEMD / "iii-system-daemon.service").read_text()
    api = (SYSTEMD / "iii-runtime-api.service").read_text()
    assert "ExecStart=/usr/libexec/iii/iii-release-launch system-daemon" in daemon
    assert "ExecStart=/usr/libexec/iii/iii-release-launch runtime-api" in api
    assert "Wants=network-online.target iii-system-daemon.service" in api
    assert "Requires=iii-system-daemon.service" not in api
    assert "StandardOutput=null" in daemon and "StandardOutput=null" in api
    assert "ProtectSystem=strict" in daemon and "ProtectSystem=strict" in api
    assert "/var/lib/iii/tuning" in daemon
    runtime_environment = (
        ROOT / "deployment/ansible/roles/runtime_control_plane/templates/runtime.env.j2"
    ).read_text(encoding="utf-8")
    assert "III_TUNING_STATE_ROOT=/var/lib/iii/tuning" in runtime_environment


def test_minimal_time_untrusted_clock_audit_has_host_retention_policy():
    filesystem = (
        ROOT / "deployment/ansible/roles/filesystem/tasks/main.yml"
    ).read_text(encoding="utf-8")
    assert "/var/log/iii/deployment/*.jsonl" in filesystem
    assert "rotate 14" in filesystem


def test_host_maintenance_privilege_is_isolated_in_fixed_oneshot_unit():
    receiver = (SYSTEMD / "iii-deployment-receiver.service").read_text()
    maintenance = (SYSTEMD / "iii-host-maintenance@.service").read_text()
    assert "ProtectSystem=strict" in receiver
    assert "PrivateNetwork=yes" in receiver
    assert "ProtectSystem=no" in maintenance
    assert "PrivateNetwork=no" in maintenance
    assert "Type=oneshot" in maintenance
    assert "%i/ansible-extra-vars.json" in maintenance
    assert "/usr/share/iii/host-maintenance/aircraft-maintenance.yml" in maintenance


def test_systemd_unit_graph_verifies_with_installed_commands_represented(tmp_path):
    for source in SYSTEMD.iterdir():
        if source.suffix in {".service", ".target", ".timer"}:
            shutil.copy2(source, tmp_path / source.name)
    for service in tmp_path.glob("*.service"):
        text = service.read_text()
        rows = []
        for row in text.splitlines():
            if row.startswith("ExecStartPre="):
                row = "ExecStartPre=/bin/true"
            elif row.startswith("ExecStart="):
                row = "ExecStart=/bin/true"
            rows.append(row)
        service.write_text("\n".join(rows) + "\n")
    result = subprocess.run(
        [
            "systemd-analyze",
            "verify",
            *map(str, sorted(tmp_path.glob("*.service"))),
            *map(str, sorted(tmp_path.glob("*.target"))),
            *map(str, sorted(tmp_path.glob("*.timer"))),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unit_contract_authenticates_every_host_asset():
    contract = json.loads((SYSTEMD / "unit-contract.json").read_text())
    ContractRegistry(ROOT / "deployment/schemas/v1").validate(
        "host-unit-contract", contract
    )
    assert contract["contract_id"] == content_identity(
        {key: item for key, item in contract.items() if key != "contract_id"}
    )
    assert contract["launcher"]["sha256"] == _sha256(
        ROOT / contract["launcher"]["path"]
    )
    assert contract["environment"]["sha256"] == _sha256(
        ROOT / contract["environment"]["path"]
    )
    assert [item["path"] for item in contract["units"]] == sorted(
        item["path"] for item in contract["units"]
    )
    for item in contract["units"]:
        assert item["sha256"] == _sha256(ROOT / item["path"])


def _launcher_root(tmp_path: Path) -> tuple[Path, dict, dict]:
    unit_contract = json.loads((SYSTEMD / "unit-contract.json").read_text())
    _write(tmp_path / "etc/iii/host-unit-contract.json", unit_contract)
    installed_launcher = tmp_path / "usr/libexec/iii/iii-release-launch"
    installed_launcher.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER, installed_launcher)
    installed_units = tmp_path / "etc/systemd/system"
    installed_units.mkdir(parents=True)
    for name in UNITS:
        shutil.copy2(SYSTEMD / name, installed_units / name)
    manifest = {
        "schema_version": "1",
        "manifest_type": "release",
        "release_id": "0" * 64,
        "target": {
            "definition_id": "a" * 64,
            "host_baseline": "b" * 64,
            "host_unit_contract": unit_contract["contract_id"],
        },
        "profiles": [{"id": "real", "bootable": True}],
    }
    manifest["release_id"] = content_identity(
        {key: item for key, item in manifest.items() if key != "release_id"}
    )
    release = tmp_path / "opt/iii/releases" / manifest["release_id"]
    _write(release / "release-manifest.json", manifest)
    wrapper = release / "bin/iii-release-env"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nexec "$@"\n')
    wrapper.chmod(0o755)
    current = tmp_path / "opt/iii/current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(Path("releases") / manifest["release_id"])
    selector = {
        "schema": "iii.activation-selector/v1",
        "selector_id": "0" * 64,
        "release_id": manifest["release_id"],
        "release_path": f"/opt/iii/releases/{manifest['release_id']}",
        "configuration_checkpoint_id": "c" * 64,
        "configuration_checkpoint_path": "/var/lib/iii/configuration/checkpoints/"
        + "c" * 64,
        "configuration_schema_version": 1,
        "mission_catalog_hash": "sha256:" + "d" * 64,
        "profile": "real",
    }
    selector["selector_id"] = content_identity(
        {key: item for key, item in selector.items() if key != "selector_id"}
    )
    _write(tmp_path / "var/lib/iii/deployment/active-selector.json", selector)
    report = {
        "schema": "iii.host-baseline-report/v1",
        "state": "converged",
        "baseline_id": "b" * 64,
        "unit_contract_id": unit_contract["contract_id"],
        "target_definition_id": "a" * 64,
    }
    _write(tmp_path / "var/lib/iii/deployment/host-baseline-report.json", report)
    return tmp_path, manifest, report


def test_launcher_accepts_exact_selector_and_refuses_host_contract_drift(tmp_path):
    root, _manifest, report = _launcher_root(tmp_path)
    accepted = subprocess.run(
        [str(LAUNCHER), "system-daemon", "--verify", "--root", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    installed_unit = root / "etc/systemd/system/iii-system-daemon.service"
    installed_unit.write_text(
        installed_unit.read_text(encoding="utf-8") + "# drift\n",
        encoding="utf-8",
    )
    drifted = subprocess.run(
        [str(LAUNCHER), "system-daemon", "--verify", "--root", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert drifted.returncode == os.EX_CONFIG
    assert "unit drifted" in drifted.stderr
    shutil.copy2(SYSTEMD / "iii-system-daemon.service", installed_unit)
    report["unit_contract_id"] = "e" * 64
    _write(root / "var/lib/iii/deployment/host-baseline-report.json", report)
    refused = subprocess.run(
        [str(LAUNCHER), "runtime-api", "--verify", "--root", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode == os.EX_CONFIG
    assert "host maintenance" in refused.stderr


def test_normal_release_policy_explicitly_forbids_host_unit_mutation():
    policy = json.loads((ROOT / "deployment/receiver-policy.json").read_text())
    forbidden = set(policy["normal_release_forbidden_paths"])
    assert {
        "/etc/systemd/system/iii-system-daemon.service",
        "/etc/systemd/system/iii-runtime-api.service",
        "/etc/systemd/system/iii.target",
        "/usr/libexec/iii/iii-release-launch",
        "/etc/iii/runtime.env",
        "/etc/iii/host-unit-contract.json",
    } <= forbidden
