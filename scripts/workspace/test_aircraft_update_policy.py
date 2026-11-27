"""No automatic package updates on the aircraft host (deployment/ansible).

Unattended-upgrades and needrestart restarted the III runtime mid-mission on
2026-11-26; provisioning must keep every automatic update path off.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deployment/ansible"
VARS = yaml.safe_load((ANSIBLE / "vars/raspberry-pi-5-noble-arm64.yml").read_text(encoding="utf-8"))
TASKS = yaml.safe_load((ANSIBLE / "roles/update_policy/tasks/main.yml").read_text(encoding="utf-8"))


def _task(name_part: str) -> dict:
    return next(task for task in TASKS if name_part in task["name"])


def test_every_provisioning_playbook_applies_the_update_policy_first():
    for playbook in ("aircraft-converge.yml", "aircraft-converge-target-equivalent.yml"):
        roles = [entry["role"] for entry in yaml.safe_load((ANSIBLE / "playbooks" / playbook).read_text())[0]["roles"]]
        assert roles.index("update_policy") == roles.index("preflight") + 1, playbook
        assert roles.index("update_policy") < roles.index("apt_baseline"), playbook


def test_apt_periodic_updates_and_unattended_upgrades_are_off():
    content = _task("periodic")["ansible.builtin.copy"]["content"]
    for setting in ("Enable", "Update-Package-Lists", "Download-Upgradeable-Packages", "Unattended-Upgrade"):
        assert f'APT::Periodic::{setting} "0";' in content
    # Sorts after Ubuntu's 10periodic and 20auto-upgrades, so it wins.
    assert _task("periodic")["ansible.builtin.copy"]["dest"] == "/etc/apt/apt.conf.d/99-iii-no-automatic-updates"


def test_every_automatic_update_unit_is_masked():
    assert set(VARS["iii_automatic_update_units"]) == {
        "apt-daily.timer", "apt-daily-upgrade.timer", "apt-daily.service",
        "apt-daily-upgrade.service", "unattended-upgrades.service",
    }
    mask = _task("mask")
    assert "systemctl mask" in mask["ansible.builtin.shell"]


def test_needrestart_never_restarts_services():
    copy = _task("needrestart from restarting")["ansible.builtin.copy"]
    assert "$nrconf{restart} = 'l';" in copy["content"]


def test_host_health_fails_unless_the_update_units_are_masked():
    health = yaml.safe_load((ANSIBLE / "roles/host_health/tasks/main.yml").read_text(encoding="utf-8"))
    check = next(task for task in health if task["name"] == "Require no automatic package updates")
    assert "masked" in check["ansible.builtin.assert"]["that"]
