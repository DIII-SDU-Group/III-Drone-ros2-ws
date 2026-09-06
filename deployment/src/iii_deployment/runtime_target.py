"""Versioned runtime-target and detected middleware-interface contracts."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from iii_deployment.contracts import ContractError, ContractRegistry


INTERFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _document(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must contain one JSON object")
    return value


def load_runtime_targets(
    path: Path, registry: ContractRegistry
) -> dict[str, dict[str, Any]]:
    value = _document(path, label="runtime target catalog")
    registry.validate("runtime-target-catalog", value)
    targets = {item["selector"]: dict(item) for item in value["targets"]}
    if len(targets) != len(value["targets"]):
        raise ContractError("runtime target selectors are not unique")
    for context, selector in value["defaults"].items():
        if selector not in targets:
            raise ContractError(f"runtime target default {context} is unavailable")
    if targets["opti_track"].get("profile_alias") != "real":
        raise ContractError("initial opti_track target must explicitly alias real")
    if not targets["hil"]["bootable"] or targets["hil"]["capabilities"] != ["flight", "simulation"]:
        raise ContractError("commissioned HIL target must be bootable with flight and simulation capabilities")
    return targets


def resolve_runtime_target(
    targets: Mapping[str, Mapping[str, Any]],
    *,
    selector: str | None,
    default_selector: str,
) -> dict[str, Any]:
    selected = selector or default_selector
    if selected not in targets:
        raise ContractError(f"runtime target is unavailable: {selected}")
    return dict(targets[selected])


def load_middleware_policy(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    value = _document(path, label="middleware interface policy")
    registry.validate("middleware-interface-policy", value)
    peer = value["future_simulator_peer"]
    if peer["enabled"] and not peer["address"]:
        raise ContractError("enabled simulator peer requires an address")
    return value


def _default_route_interface(route_path: Path) -> str | None:
    if not route_path.is_file() or route_path.is_symlink():
        return None
    try:
        rows = route_path.read_text(encoding="ascii").splitlines()[1:]
    except (OSError, UnicodeDecodeError):
        return None
    candidates = []
    for row in rows:
        fields = row.split()
        if len(fields) >= 4 and fields[1] == "00000000":
            try:
                flags = int(fields[3], 16)
            except ValueError:
                continue
            if flags & 0x1:
                candidates.append(fields[0])
    return sorted(set(candidates))[0] if candidates else None


def detect_middleware_interface(
    target: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    sys_class_net: Path = Path("/sys/class/net"),
    route_path: Path = Path("/proc/net/route"),
) -> dict[str, Any]:
    if target["execution_host"] == "operator" and target["runtime_profile"] == "sim":
        return {
            "schema": "iii.middleware-selection/v1",
            "interface": policy["loopback_interface"],
            "source": "profile-loopback",
            "peers": [],
            "future_simulator_peer_enabled": False,
        }
    if sys_class_net.is_symlink() or not sys_class_net.is_dir():
        raise ContractError("network interface inventory is unavailable")
    excluded = tuple(policy["excluded_interface_prefixes"])
    available: list[str] = []
    for path in sorted(sys_class_net.iterdir(), key=lambda item: item.name):
        name = path.name
        if (
            not INTERFACE.fullmatch(name)
            or name == policy["loopback_interface"]
            or name.startswith(excluded)
        ):
            continue
        try:
            state = (path / "operstate").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if state in {"up", "unknown"}:
            available.append(name)
    default = _default_route_interface(route_path)
    if default in available:
        selected, source = default, "default-route"
    elif available:
        selected, source = available[0], "stable-up-interface"
    else:
        raise ContractError(
            "no stable LAN interface is available for the aircraft target"
        )
    simulator_peer = policy["future_simulator_peer"]
    peer_enabled = bool(
        target["runtime_profile"] in simulator_peer["allowed_profiles"]
        and simulator_peer["enabled"]
    )
    return {
        "schema": "iii.middleware-selection/v1",
        "interface": selected,
        "source": source,
        "peers": [simulator_peer["address"]] if peer_enabled else [],
        "future_simulator_peer_enabled": peer_enabled,
    }
