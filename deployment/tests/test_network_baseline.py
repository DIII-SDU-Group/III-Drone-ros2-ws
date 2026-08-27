from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_avahi_publishes_iii_local_without_fixed_ip_or_reflector() -> None:
    template = (
        ROOT
        / "deployment/ansible/roles/network_baseline/templates/avahi-daemon.conf.j2"
    ).read_text()
    policy = (
        ROOT
        / "deployment/ansible/roles/network_baseline/templates/network-policy.json.j2"
    ).read_text()
    variables = yaml.safe_load(
        (ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml").read_text()
    )
    assert "host-name=iii" in template
    assert "domain-name=local" in template
    assert "publish-addresses=yes" in template
    assert "use-ipv6=no" in template
    assert "enable-reflector=no" in template
    assert "avahi-daemon" in variables["iii_host_packages"]
    assert variables["iii_hostname_manage"] is True
    assert "mdns_fqdn': 'iii.local'" in policy
    assert "'fixed_ip': false" in policy
    assert "'onboard_access_point': false" in policy


def test_network_baseline_precedes_firewall_receiver_and_runtime() -> None:
    for name in ("aircraft-converge.yml", "aircraft-converge-target-equivalent.yml"):
        playbook = (ROOT / "deployment/ansible/playbooks" / name).read_text()
        assert playbook.index("role: network_baseline") < playbook.index(
            "role: firewall"
        )
        assert playbook.index("role: network_baseline") < playbook.index(
            "role: receiver"
        )
    firewall = (
        ROOT / "deployment/ansible/roles/firewall/templates/nftables.conf.j2"
    ).read_text()
    assert "udp dport {{ iii_mdns_port }} accept" in firewall


def test_network_apply_and_revert_have_fixed_privileged_units_and_90_second_timer() -> (
    None
):
    systemd = ROOT / "deployment/systemd"
    apply = (systemd / "iii-network-apply@.service").read_text()
    revert = (systemd / "iii-network-revert@.service").read_text()
    timeout = (systemd / "iii-network-timeout@.service").read_text()
    timer = (systemd / "iii-network-revert@.timer").read_text()
    receiver = (systemd / "iii-deployment-receiver.service").read_text()
    assert (
        "ExecStart=/opt/iii/receiver/selectors/current/bin/iii-network-apply --operation-id %i"
        in apply
    )
    assert (
        "ExecStart=/opt/iii/receiver/selectors/current/bin/iii-network-revert --operation-id %i"
        in revert
    )
    assert "Unit=iii-network-timeout@%i.service" in timer
    assert "try-restart iii-deployment-receiver.service" not in revert
    assert "try-restart iii-deployment-receiver.service" in timeout
    for unit in (apply, revert, timeout):
        assert "ProtectSystem=strict" in unit
        assert "ReadWritePaths=/etc/netplan /var/lib/iii/deployment/network" in unit
        assert "PrivateTmp=yes" in unit
    assert "OnActiveSec=90s" in timer
    assert "Persistent=false" in timer
    assert "/etc/netplan" not in next(
        line for line in receiver.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "PrivateNetwork=yes" in receiver


def test_application_and_receiver_updates_cannot_mutate_network_host_policy() -> None:
    policy = json.loads((ROOT / "deployment/receiver-policy.json").read_text())
    for forbidden in (
        "/etc/netplan",
        "/etc/avahi/avahi-daemon.conf",
        "/etc/hostname",
        "/var/lib/iii/deployment/network",
        "/etc/iii/network-policy.json",
        "/etc/systemd/system/iii-network-apply@.service",
        "/etc/systemd/system/iii-network-revert@.service",
        "/etc/systemd/system/iii-network-timeout@.service",
        "/etc/systemd/system/iii-network-revert@.timer",
    ):
        assert forbidden in policy["normal_release_forbidden_paths"]
        assert forbidden in policy["self_update_forbidden_paths"]
    assert policy["host_control_mutable_paths"] == ["/etc/netplan"]
