from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.runtime_target import (
    detect_middleware_interface,
    load_middleware_policy,
    load_runtime_targets,
    resolve_runtime_target,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")


def _contracts():
    targets = load_runtime_targets(ROOT / "deployment/runtime-targets.json", REGISTRY)
    policy = load_middleware_policy(
        ROOT / "deployment/middleware-policy.json", REGISTRY
    )
    return targets, policy


def test_target_descriptor_decouples_endpoint_execution_profile_and_simulator() -> None:
    targets, _policy = _contracts()
    assert set(targets) == {"sim", "real", "opti_track", "hil"}
    assert targets["sim"] == {
        "selector": "sim",
        "endpoint": "local",
        "logical_id": "drone",
        "execution_host": "operator",
        "runtime_profile": "sim",
        "parameter_profile": "sim",
        "simulator_provider": "gazebo-local",
        "profile_alias": None,
        "bootable": True,
        "capabilities": ["simulation"],
        "middleware_policy": "iii.middleware-interface-policy/v1",
    }
    opti = targets["opti_track"]
    assert opti["endpoint"] == targets["real"]["endpoint"] == "iii.local"
    assert opti["runtime_profile"] == "opti_track"
    assert opti["profile_alias"] == "real"
    assert opti["parameter_profile"] == "real"


def test_future_split_host_hil_is_representable_but_fails_boot_closed() -> None:
    targets, policy = _contracts()
    hil = resolve_runtime_target(targets, selector="hil", default_selector="real")
    assert hil["execution_host"] == "aircraft"
    assert hil["simulator_provider"] == "gazebo-workstation"
    assert hil["bootable"] is False and hil["capabilities"] == []
    assert policy["future_simulator_peer"] == {
        "enabled": False,
        "allowed_profiles": ["hil"],
        "address": None,
    }


def test_middleware_selection_uses_loopback_or_detected_stable_lan(
    tmp_path: Path,
) -> None:
    targets, policy = _contracts()
    sim = detect_middleware_interface(targets["sim"], policy)
    assert sim["interface"] == "lo" and sim["peers"] == []

    interfaces = tmp_path / "sys/class/net"
    for name, state in (("docker0", "up"), ("wlan0", "up"), ("eth0", "down")):
        root = interfaces / name
        root.mkdir(parents=True)
        (root / "operstate").write_text(state + "\n", encoding="ascii")
    route = tmp_path / "proc/net/route"
    route.parent.mkdir(parents=True)
    route.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "wlan0 00000000 01020304 0003 0 0 0 00000000 0 0 0\n",
        encoding="ascii",
    )
    selected = detect_middleware_interface(
        targets["real"], policy, sys_class_net=interfaces, route_path=route
    )
    assert selected == {
        "schema": "iii.middleware-selection/v1",
        "interface": "wlan0",
        "source": "default-route",
        "peers": [],
        "future_simulator_peer_enabled": False,
    }
    (interfaces / "wlan0/operstate").write_text("down\n", encoding="ascii")
    with pytest.raises(ContractError, match="no stable LAN"):
        detect_middleware_interface(
            targets["real"], policy, sys_class_net=interfaces, route_path=route
        )
