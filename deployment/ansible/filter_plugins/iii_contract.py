"""Controller-side deterministic filters for the III host baseline."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_id(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def private_network(value: str) -> bool:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return False
    return network.version == 4 and network.is_private and not network.is_loopback


def live_state(profile: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "iii.receiver-live-state/v1",
        "target_state_hash": "0" * 64,
        "active_release_id": None,
        "configuration_hash": None,
        "commissioning_hash": None,
        "profile": profile,
    }
    value["target_state_hash"] = content_id(
        {key: item for key, item in value.items() if key != "target_state_hash"}
    )
    return value


class FilterModule:
    def filters(self) -> Mapping[str, Any]:
        return {
            "iii_canonical_json": canonical_json,
            "iii_content_id": content_id,
            "iii_private_network": private_network,
            "iii_live_state": live_state,
        }
