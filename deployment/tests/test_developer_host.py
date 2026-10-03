from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_aircraft_convergence_uses_no_receiver_or_firewall_role() -> None:
    playbook = _read("deployment/ansible/playbooks/aircraft-converge.yml")
    assert "hosts: all" in playbook
    assert "role: receiver" not in playbook
    assert "role: firewall" not in playbook
    assert "role: host_maintenance_install" not in playbook


def test_developer_identity_is_interactive_and_not_forced_command() -> None:
    identity = _read("deployment/ansible/roles/identity/tasks/main.yml")
    assert "password_lock: false" in identity
    assert "PermitRootLogin yes" in identity
    assert "AllowTcpForwarding yes" in identity
    assert "50-iii-forced-command.conf" in identity
    assert "name: iii-deploy" in identity
    assert "state: absent" in identity
    assert "iii_provisioning_inputs" not in identity


def test_runtime_units_launch_the_editable_workspace_without_sandboxing() -> None:
    for relative in (
        "deployment/systemd/iii-system-daemon.service",
        "deployment/systemd/iii-runtime-api.service",
    ):
        unit = _read(relative)
        assert "/home/iii/ws/install/setup.bash" in unit
        assert "/home/iii/ws/.venv/bin/python" in unit
        assert "iii-deployment-receiver" not in unit
        for directive in (
            "ProtectSystem",
            "ProtectHome",
            "NoNewPrivileges",
            "PrivateTmp",
            "ReadWritePaths",
            "RestrictSUIDSGID",
            "RestrictAddressFamilies",
        ):
            assert directive not in unit


def test_runtime_environment_does_not_require_credentials_or_immutable_release() -> None:
    environment = _read(
        "deployment/ansible/roles/runtime_control_plane/templates/runtime.env.j2"
    )
    assert "III_RUNTIME_API_REQUIRE_SECRETS=0" in environment
    assert "/home/iii/.config/iii_drone" in environment
    assert "CREDENTIALS_PATH" not in environment
    assert "/opt/iii" not in environment


def test_developer_filesystem_removes_immutable_release_surface() -> None:
    filesystem = _read("deployment/ansible/roles/filesystem/tasks/main.yml")
    assert "/home/iii/ws" in filesystem
    assert "/opt/iii" in filesystem
    assert "state: absent" in filesystem
    assert "iii-deploy" not in filesystem


def test_package_and_time_setup_use_ordinary_host_defaults() -> None:
    packages = _read("deployment/ansible/roles/apt_baseline/tasks/main.yml")
    variables = _read("deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml")
    time = _read("deployment/ansible/roles/time/tasks/main.yml")
    assert "snapshot" not in packages.lower()
    assert "nftables" not in variables
    assert "ros-dev-tools" in variables
    assert "ros-jazzy-ros-base" in variables
    assert "ros-infrastructure/ros-apt-source" in packages
    assert "ros2-apt-source_" in packages
    assert "makestep" not in time


def test_usb_ethernet_is_owned_once_by_first_boot_networking() -> None:
    first_boot = _read("tools/III-Drone-CLI/iii/host.py")
    px4_network = _read(
        "deployment/ansible/roles/network_baseline/templates/80-iii-px4.yaml.j2"
    )

    assert "workstation-usb-ethernet" in first_boot
    assert "driver: ax88179_178a" in first_boot
    assert "addresses: [10.42.0.15/24]" in first_boot
    assert "dhcp4: true" in first_boot
    assert "workstation-ethernet" not in px4_network
