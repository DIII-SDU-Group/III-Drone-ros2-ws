"""Optional aircraft Wi-Fi client provisioning (deployment/ansible).

The Wi-Fi client must not disturb the PX4 Ethernet link or the workstation
USB-Ethernet link, must own the default route while associated, and must never
log or commit its secret.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deployment/ansible"
VARS = ANSIBLE / "vars/raspberry-pi-5-noble-arm64.yml"
NETWORK_ROLE = ANSIBLE / "roles/network_baseline"
WIFI_TEMPLATE = NETWORK_ROLE / "templates/85-iii-wifi.yaml.j2"
PX4_TEMPLATE = NETWORK_ROLE / "templates/80-iii-px4.yaml.j2"
# Test-only values; never a real network's secret.
SSID = 'Lab "5G": #1 \\ Øst'
PASSPHRASE = 'not-a-real "secret" #: \\ value'


def _render(template: Path, **overrides) -> str:
    """Render like Ansible's template module (trim_blocks, to_json)."""

    values = yaml.safe_load(VARS.read_text(encoding="utf-8"))
    values.update(overrides)
    environment = jinja2.Environment(
        undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True
    )
    environment.filters["to_json"] = json.dumps
    return environment.from_string(template.read_text(encoding="utf-8")).render(values)


def _wifi(**overrides) -> dict:
    values = {"iii_wifi_ssid": SSID, "iii_wifi_psk": PASSPHRASE, **overrides}
    return yaml.safe_load(_render(WIFI_TEMPLATE, **values))


def _tasks() -> list[dict]:
    return yaml.safe_load((NETWORK_ROLE / "tasks/main.yml").read_text(encoding="utf-8"))


def test_wifi_client_is_optional_and_carries_no_committed_secret():
    values = yaml.safe_load(VARS.read_text(encoding="utf-8"))
    assert values["iii_wifi_ssid"] == ""
    assert values["iii_wifi_psk"] == ""
    assert values["iii_wifi_remove"] is False
    # Every task that mentions the secret, or copies a file holding it,
    # refuses to log it.
    for task in _tasks():
        text = yaml.safe_dump(task)
        if "iii_wifi_psk" in text or "85-iii-wifi.yaml" in text and "state: absent" not in text:
            assert task.get("no_log") is True, task["name"]


def test_wifi_client_round_trips_ssid_and_passphrase_safely():
    network = _wifi()["network"]
    assert network["version"] == 2
    # Only the Wi-Fi interface is described: the PX4 and USB-Ethernet links
    # keep their own netplan files.
    assert set(network) == {"version", "wifis"}
    wlan = network["wifis"]["wlan0"]
    assert wlan["access-points"] == {SSID: {"password": PASSPHRASE}}
    assert wlan["dhcp4"] is True
    # Never delays boot or network-online.target when the lab is out of range.
    assert wlan["optional"] is True
    assert "regulatory-domain" not in wlan


def test_wifi_owns_the_default_route_over_wired_dhcp():
    wlan = _wifi()["network"]["wifis"]["wlan0"]
    # netplan's networkd backend gives wired DHCP routes metric 100.
    assert 0 < wlan["dhcp4-overrides"]["route-metric"] < 100


def test_wifi_regulatory_domain_is_rendered_only_when_selected():
    wlan = _wifi(iii_wifi_regulatory_domain="DK")["network"]["wifis"]["wlan0"]
    assert wlan["regulatory-domain"] == "DK"


def test_wifi_client_file_is_root_only_and_applied_through_netplan():
    tasks = {task["name"]: task for task in _tasks()}
    install = tasks["Install the active Wi-Fi client"]
    template = install["ansible.builtin.template"]
    assert template["dest"] == "/etc/netplan/85-iii-wifi.yaml"
    assert template["mode"] == "0600"
    assert install["no_log"] is True
    assert install["notify"] == "Apply III netplan"
    remove = tasks["Remove the active Wi-Fi client"]
    assert remove["ansible.builtin.file"] == {"path": "/etc/netplan/85-iii-wifi.yaml", "state": "absent"}
    assert remove["when"] == "iii_wifi_remove | bool"
    handlers = yaml.safe_load((NETWORK_ROLE / "handlers/main.yml").read_text(encoding="utf-8"))
    apply = next(handler for handler in handlers if handler.get("listen") == "Apply III netplan")
    assert apply["ansible.builtin.command"]["argv"] == ["/usr/sbin/netplan", "apply"]


def test_real_and_opti_track_each_keep_a_required_wifi_slot():
    values = yaml.safe_load(VARS.read_text(encoding="utf-8"))
    assert values["iii_wifi_slot_profiles"] == ["real", "opti_track"]
    slot = "{{ iii_wifi_slot_directory }}/{{ iii_profile }}.yaml"
    in_slot_profile = "iii_profile in iii_wifi_slot_profiles"
    tasks = {task["name"]: task for task in _tasks()}

    store = tasks["Store the Wi-Fi client in this profile's slot"]
    assert store["ansible.builtin.template"]["dest"] == slot
    assert store["ansible.builtin.template"]["mode"] == "0600"
    assert store["when"] == [in_slot_profile, "(iii_wifi_ssid | length) > 0"]

    # A profile without a stored client and without --wifi-ssid is refused.
    require = tasks["Require a Wi-Fi client for this profile"]
    assert require["ansible.builtin.assert"]["that"] == ["iii_wifi_slot.stat.exists"]
    assert in_slot_profile in require["when"]

    # Provisioning a slot profile always activates that profile's own client.
    activate = tasks["Activate this profile's Wi-Fi client"]
    assert activate["ansible.builtin.copy"]["src"] == slot
    assert activate["ansible.builtin.copy"]["dest"] == "/etc/netplan/85-iii-wifi.yaml"
    assert activate["ansible.builtin.copy"]["mode"] == "0600"
    assert activate["notify"] == "Apply III netplan"
    assert in_slot_profile in activate["when"]

    # hil sets the active client directly and may remove it; slot profiles may not.
    assert tasks["Install the active Wi-Fi client"]["when"][0] == "iii_profile not in iii_wifi_slot_profiles"
    validate = tasks["Validate the Wi-Fi client request"]["ansible.builtin.assert"]["that"]
    assert "not (iii_wifi_remove | bool) or iii_profile not in iii_wifi_slot_profiles" in validate

    names = list(tasks)
    assert names.index("Store the Wi-Fi client in this profile's slot") < names.index(
        "Require a Wi-Fi client for this profile"
    ) < names.index("Activate this profile's Wi-Fi client")


def test_provisioning_ends_by_restarting_the_daemon_and_the_runtime_api():
    tasks = yaml.safe_load(
        (ANSIBLE / "roles/runtime_control_plane/tasks/main.yml").read_text(encoding="utf-8")
    )
    restart = tasks[-1]
    assert restart["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert restart["loop"] == ["iii-system-daemon.service", "iii-runtime-api.service"]


@pytest.mark.skipif(shutil.which("netplan") is None, reason="netplan is not installed")
def test_netplan_generates_a_wifi_client_beside_the_existing_links(tmp_path):
    netplan = tmp_path / "etc/netplan"
    netplan.mkdir(parents=True)
    (netplan / "80-iii-px4.yaml").write_text(_render(PX4_TEMPLATE), encoding="utf-8")
    # The first-boot workstation link as seeded by `iii host image write`.
    (netplan / "50-cloud-init.yaml").write_text(
        "network:\n  version: 2\n  ethernets:\n    workstation-usb-ethernet:\n"
        "      match:\n        driver: ax88179_178a\n      addresses: [10.42.0.15/24]\n"
        "      dhcp4: true\n      link-local: []\n      optional: true\n",
        encoding="utf-8",
    )
    (netplan / "85-iii-wifi.yaml").write_text(
        _render(
            WIFI_TEMPLATE,
            iii_wifi_ssid=SSID,
            iii_wifi_psk=PASSPHRASE,
            iii_wifi_regulatory_domain="DK",
        ),
        encoding="utf-8",
    )
    for path in netplan.iterdir():
        path.chmod(0o600)
    completed = subprocess.run(
        ["netplan", "generate", "--root-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    networkd = tmp_path / "run/systemd/network"
    wifi = (networkd / "10-netplan-wlan0.network").read_text(encoding="utf-8")
    assert "Name=wlan0" in wifi
    assert "RequiredForOnline=no" in wifi
    assert "RouteMetric=50" in wifi
    usb = (networkd / "10-netplan-workstation-usb-ethernet.network").read_text(encoding="utf-8")
    assert "Address=10.42.0.15/24" in usb
    assert "RouteMetric=100" in usb
    px4 = (networkd / "10-netplan-px4-ethernet.network").read_text(encoding="utf-8")
    assert "Address=10.41.10.1/24" in px4
    assert "DHCP" not in px4
    supplicant = tmp_path / "run/netplan/wpa-wlan0.conf"
    assert supplicant.is_file()
    assert "country=DK" in supplicant.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook is not installed")
def test_aircraft_playbook_passes_ansible_syntax_check():
    completed = subprocess.run(
        [
            "ansible-playbook",
            "--syntax-check",
            "-i",
            "localhost,",
            str(ANSIBLE / "playbooks/aircraft-converge.yml"),
            "-e",
            "iii_profile=opti_track",
        ],
        cwd=ANSIBLE,
        env={"ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg"), "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(Path(shutil.which("ansible-playbook")).parent), "HOME": str(Path.home())},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
