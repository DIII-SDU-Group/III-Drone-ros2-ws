from __future__ import annotations

import json
from pathlib import Path

from iii_deployment.contracts import ContractRegistry
from iii_deployment.identity import load_shared_target_profile


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "deployment/schemas/v1"
PROFILE = ROOT / "deployment/targets/v1/shared-aircraft.json"


def test_committed_shared_target_profile_is_public_stable_and_inventory_free() -> None:
    profile = load_shared_target_profile(PROFILE, ContractRegistry(SCHEMAS))
    assert profile["logical_target"] == "drone"
    assert profile["runtime"] == {
        "runtime_id": "iii-aircraft-runtime",
        "system_id": "iii-aircraft",
        "display_name": "III Aircraft Runtime",
        "mdns_instance": "III Aircraft Runtime",
    }
    assert profile["inventory_mode"] == "shared-hardware-class"
    assert profile["credentials"]["committed_secrets"] is False
    rendered = json.dumps(profile).lower()
    assert "password" not in rendered
    assert "private_key" not in rendered
    assert "serial" not in rendered


def test_ansible_uses_only_the_shared_profile_identity_values() -> None:
    variables = (
        ROOT / "deployment/ansible/vars/raspberry-pi-5-noble-arm64.yml"
    ).read_text()
    profile = json.loads(PROFILE.read_text())
    assert profile["profile_id"] in variables
    assert "iii_runtime_api_id: iii-aircraft-runtime" in variables
    assert "iii_runtime_system_id: iii-aircraft" in variables
