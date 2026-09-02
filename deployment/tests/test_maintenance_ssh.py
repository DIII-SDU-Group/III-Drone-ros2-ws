from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_maintenance_account_is_separate_from_receiver_and_bootstrap() -> None:
    identity = (ROOT / "deployment/ansible/roles/identity/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    variables = (
        ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml"
    ).read_text(encoding="utf-8")

    assert "iii_maintenance_user: iii-maint" in variables
    assert "iii_maintenance_uid: 1101" in variables
    assert "password_lock: true" in identity
    assert "maintenance_ssh_public_key" in identity
    assert "/home/iii-maint/.ssh/authorized_keys" in identity
    assert "/etc/sudoers.d/90-iii-maintenance" in identity


def test_maintenance_account_has_explicit_full_sudo_only_in_its_own_fragment() -> None:
    maintenance = (
        ROOT
        / "deployment/ansible/roles/identity/templates/90-iii-maintenance.sudoers.j2"
    ).read_text(encoding="utf-8")
    runtime = (ROOT / "deployment/host/iii-deployment-final.sudoers").read_text(
        encoding="utf-8"
    )

    assert maintenance == "{{ iii_maintenance_user }} ALL=(ALL:ALL) NOPASSWD: ALL\n"
    assert "NOPASSWD: ALL" not in runtime


def test_ssh_policy_allows_human_tty_but_no_forwarding() -> None:
    policy = (
        ROOT
        / "deployment/ansible/roles/receiver/templates/50-iii-forced-command.conf.j2"
    ).read_text(encoding="utf-8")
    receiver, maintenance = policy.split("Match User iii-maint", 1)

    assert "Match User iii" in receiver
    assert "PermitTTY no" in receiver
    assert "PermitTTY yes" in maintenance
    assert "AuthenticationMethods publickey" in maintenance
    assert "AllowTcpForwarding no" in maintenance
    assert "AllowAgentForwarding no" in maintenance
    assert "PermitTunnel no" in maintenance


def test_firewall_limits_maintenance_ssh_to_operator_network() -> None:
    policy = (
        ROOT / "deployment/ansible/roles/firewall/templates/nftables.conf.j2"
    ).read_text(encoding="utf-8")

    ssh_rules = [line.strip() for line in policy.splitlines() if "tcp dport 22" in line]
    assert ssh_rules == [
        'ip saddr {{ iii_provisioning_inputs.operator_cidr }} tcp dport 22 accept comment "receiver, maintenance, and bootstrap SSH"'
    ]
