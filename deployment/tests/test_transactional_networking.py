from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess

import pytest

from iii_deployment.networking import (
    INPUT_SCHEMA,
    NetworkController,
    NetworkError,
    load_network_input,
    redacted_profile,
    render_netplan,
    validate_network_input,
)

CLIENT_ID = "a" * 64


def _profile(*, wifi: list[dict] | None = None) -> dict:
    return {
        "schema": INPUT_SCHEMA,
        "ethernet_dhcp4": True,
        "wifi": [] if wifi is None else wifi,
    }


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.now = 10_000_000_000
        self.boot = "boot-a"
        self.commands: list[tuple[str, ...]] = []
        self.controller = NetworkController(
            root=root,
            monotonic_ns=lambda: self.now,
            boot_id=lambda: self.boot,
            run=self.run,
            maintenance_safe=lambda: True,
        )

    def run(self, argv, **_kwargs):
        self.commands.append(tuple(argv))
        if len(argv) == 3 and argv[:2] == ["/usr/bin/systemctl", "start"]:
            unit = argv[2]
            if unit.startswith("iii-network-apply@"):
                operation_id = unit.removeprefix("iii-network-apply@").removesuffix(
                    ".service"
                )
                self.controller.apply_claimed(operation_id)
            elif unit.startswith("iii-network-revert@") and unit.endswith(".service"):
                operation_id = unit.removeprefix("iii-network-revert@").removesuffix(
                    ".service"
                )
                self.controller.revert(operation_id)
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def installed(self) -> Path:
        return self.root / "etc/netplan/90-iii-operator.yaml"


def test_ethernet_only_and_multiple_wifi_render_without_access_point() -> None:
    ethernet = json.loads(render_netplan(_profile()))
    assert ethernet["network"]["ethernets"]["ethernet-recovery"] == {
        "match": {"name": "e*"},
        "dhcp4": True,
        "optional": True,
    }
    assert "wifis" not in ethernet["network"]
    multiple = _profile(
        wifi=[
            {"ssid": "field-a", "password": "secret-pass-a"},
            {"ssid": "field-b", "password": "secret-pass-b", "hidden": True},
        ]
    )
    rendered = json.loads(render_netplan(multiple))
    assert set(rendered["network"]["wifis"]["wlan0"]["access-points"]) == {
        "field-a",
        "field-b",
    }
    assert "access-points" not in rendered["network"].get("ethernets", {})
    assert redacted_profile(multiple)["onboard_access_point"] is False


def test_input_rejects_duplicate_ssids_and_ethernet_disable() -> None:
    with pytest.raises(NetworkError, match="duplicate"):
        validate_network_input(
            _profile(
                wifi=[
                    {"ssid": "same", "password": "first-secret"},
                    {"ssid": "same", "password": "second-secret"},
                ]
            )
        )
    invalid = _profile()
    invalid["ethernet_dhcp4"] = False
    with pytest.raises(NetworkError, match="Ethernet DHCP"):
        validate_network_input(invalid)


def test_network_input_must_be_owner_only_and_git_ignored(tmp_path: Path) -> None:
    path = tmp_path / "network.json"
    path.write_text(json.dumps(_profile()))
    path.chmod(0o644)
    with pytest.raises(NetworkError, match="owner-only"):
        load_network_input(path)
    path.chmod(0o600)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    with pytest.raises(NetworkError, match="must be ignored"):
        load_network_input(path)
    (tmp_path / ".gitignore").write_text("network.json\n")
    assert load_network_input(path)["ethernet_dhcp4"] is True


def test_plan_is_redacted_and_reports_connectivity_impact(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    profile = _profile(
        wifi=[{"ssid": "private-field-net", "password": "private-passphrase"}]
    )
    plan = harness.controller.plan(
        operation_id="network-operation-01", client_id=CLIENT_ID, profile=profile
    )
    serialized = json.dumps(plan, sort_keys=True)
    assert "private-field-net" not in serialized
    assert "private-passphrase" not in serialized
    assert plan["profile"]["wifi_profile_count"] == 1
    assert plan["connectivity_impacting"] is True
    assert plan["confirmation_deadline_s"] == 90
    assert plan["profile"]["ethernet_dhcp4"] is True


def test_apply_arms_onboard_timer_and_confirmation_commits_root_only_profile(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    profile = _profile(wifi=[{"ssid": "field", "password": "field-secret"}])
    plan = harness.controller.plan(
        operation_id="network-operation-02", client_id=CLIENT_ID, profile=profile
    )
    harness.controller.claim(plan, profile)
    result = harness.controller.apply(plan)
    assert result["state"] == "pending-confirmation"
    assert result["confirmation_deadline_s"] == 90
    assert (
        "/usr/bin/systemctl",
        "start",
        "iii-network-apply@network-operation-02.service",
    ) in harness.commands
    assert (
        "/usr/bin/systemctl",
        "start",
        "iii-network-revert@network-operation-02.timer",
    ) in harness.commands
    assert stat.S_IMODE(harness.installed.stat().st_mode) == 0o600
    assert "field-secret" in harness.installed.read_text()
    confirmed = harness.controller.confirm(
        "network-operation-02", client_id=CLIENT_ID, network_id=plan["network_id"]
    )
    assert confirmed["state"] == "confirmed"
    assert (
        "/usr/bin/systemctl",
        "stop",
        "iii-network-revert@network-operation-02.timer",
    ) in harness.commands
    current = json.loads(
        (tmp_path / "var/lib/iii/deployment/network/current.json").read_text()
    )
    assert current["network_id"] == plan["network_id"]
    assert "field-secret" not in json.dumps(current)
    harness.boot = "boot-b"
    assert harness.controller.reconcile()["recovered"] == []
    assert harness.controller.status("network-operation-02")["state"] == "confirmed"
    assert "field-secret" in harness.installed.read_text()


def test_unconfirmed_profile_reverts_previous_configuration(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.installed.parent.mkdir(parents=True)
    previous = render_netplan(_profile())
    harness.installed.write_bytes(previous)
    harness.installed.chmod(0o600)
    profile = _profile(wifi=[{"ssid": "broken", "password": "broken-secret"}])
    plan = harness.controller.plan(
        operation_id="network-operation-03", client_id=CLIENT_ID, profile=profile
    )
    harness.controller.claim(plan, profile)
    harness.controller.apply(plan)
    harness.now += 91_000_000_000
    with pytest.raises(NetworkError, match="deadline expired"):
        harness.controller.confirm(
            "network-operation-03", client_id=CLIENT_ID, network_id=plan["network_id"]
        )
    assert harness.installed.read_bytes() == previous
    assert (
        json.loads(harness.installed.read_text())["network"]["ethernets"][
            "ethernet-recovery"
        ]["dhcp4"]
        is True
    )


def test_reboot_reconciliation_restores_previous_profile(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.installed.parent.mkdir(parents=True)
    previous = render_netplan(_profile())
    harness.installed.write_bytes(previous)
    harness.installed.chmod(0o600)
    candidate = _profile(wifi=[{"ssid": "candidate", "password": "candidate-pass"}])
    plan = harness.controller.plan(
        operation_id="network-operation-04", client_id=CLIENT_ID, profile=candidate
    )
    harness.controller.claim(plan, candidate)
    harness.controller.apply(plan)
    harness.boot = "boot-b"
    recovered = harness.controller.reconcile()
    assert recovered["recovered"] == ["network-operation-04"]
    assert harness.installed.read_bytes() == previous


def test_generate_failure_restores_working_ethernet_profile(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.installed.parent.mkdir(parents=True)
    previous = render_netplan(_profile())
    harness.installed.write_bytes(previous)
    harness.installed.chmod(0o600)
    generate_calls = 0

    def fail_first_generate(argv, **_kwargs):
        nonlocal generate_calls
        if (
            len(argv) == 3
            and argv[:2] == ["/usr/bin/systemctl", "start"]
            and argv[2].startswith("iii-network-apply@")
        ):
            harness.controller.apply_claimed("network-operation-05")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == ["/usr/sbin/netplan", "generate"]:
            generate_calls += 1
        if generate_calls == 1:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    harness.controller.run = fail_first_generate
    candidate = _profile(wifi=[{"ssid": "bad", "password": "invalid-but-long"}])
    plan = harness.controller.plan(
        operation_id="network-operation-05", client_id=CLIENT_ID, profile=candidate
    )
    harness.controller.claim(plan, candidate)
    with pytest.raises(NetworkError, match="network command failed"):
        harness.controller.apply(plan)
    assert harness.installed.read_bytes() == previous


def test_connectivity_mutation_requires_maintenance_safe_state(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.controller.maintenance_safe = lambda: False
    candidate = _profile(wifi=[{"ssid": "unsafe", "password": "unsafe-password"}])
    plan = harness.controller.plan(
        operation_id="network-operation-06", client_id=CLIENT_ID, profile=candidate
    )
    harness.controller.claim(plan, candidate)
    with pytest.raises(NetworkError, match="maintenance-safe"):
        harness.controller.apply(plan)
    assert not harness.installed.exists()
    assert not any(
        "iii-network-apply" in " ".join(command) for command in harness.commands
    )
