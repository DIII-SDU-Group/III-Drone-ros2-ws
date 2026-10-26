from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).with_name("bind_hil_gc_target.py")
SPEC = importlib.util.spec_from_file_location("bind_hil_gc_target", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_binding_selects_and_checks_the_explicit_runtime(monkeypatch):
    calls = []

    def request(base, path, payload=None):
        calls.append((base, path, payload))
        if path == "/runtime/target":
            return {"selected": None, "browser_connected": False}
        if path.endswith("/manual"):
            return {"endpoint_id": "manual:http://alternate.local:8765", "base_url": payload["base_url"]}
        return {"selected": {"base_url": "http://alternate.local:8765"}}

    monkeypatch.setattr(module, "request_json", request)
    assert module.bind("http://127.0.0.1:8781", "http://alternate.local:8765/") == "manual:http://alternate.local:8765"
    assert calls == [
        ("http://127.0.0.1:8781", "/runtime/target", None),
        ("http://127.0.0.1:8781", "/runtime/discovery/manual", {"base_url": "http://alternate.local:8765"}),
        ("http://127.0.0.1:8781", "/runtime/target/select", {"endpoint_id": "manual:http://alternate.local:8765"}),
    ]


def test_proxy_binding_uses_reachable_ipv4_for_host_only_mdns(monkeypatch):
    import socket
    from urllib.error import URLError

    monkeypatch.setattr(
        module.socket, "getaddrinfo",
        lambda *_args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.0.15", 8765)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.251", 8765)),
        ],
    )
    def identity(base, path, _payload=None):
        assert path == "/identity"
        if base == "http://10.42.0.15:8765":
            raise URLError("stale direct link")
        return {"runtime_id": "iii-runtime", "profile": "hil"}

    monkeypatch.setattr(module, "request_json", identity)
    assert module.proxy_reachable_runtime_url("http://iii.local:8765") == "http://192.168.1.251:8765"


def test_disconnected_hostname_selection_is_rebound_to_proxy_reachable_ip(monkeypatch):
    import socket

    monkeypatch.setattr(
        module.socket, "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.251", 0))],
    )
    calls = []
    def request(base, path, payload=None):
        calls.append((base, path, payload))
        if path == "/runtime/target":
            return {"selected": {"endpoint_id": "mdns:pi", "base_url": "http://iii.local:8765"}, "browser_connected": False}
        if path == "/identity":
            return {"runtime_id": "iii-runtime", "host_label": "iii-drone", "profile": "hil"}
        if path == "/runtime/discovery/manual":
            return {"endpoint_id": "manual:ip", "base_url": payload["base_url"]}
        return {"selected": {"base_url": "http://192.168.1.251:8765"}}

    monkeypatch.setattr(module, "request_json", request)
    assert module.bind("http://127.0.0.1:8781", "http://192.168.1.251:8765") == "manual:ip"
    assert any(path == "/runtime/discovery/manual" for _, path, _ in calls)


def test_binding_refuses_proxy_target_mismatch(monkeypatch):
    def request(_base, path, payload=None):
        if path == "/runtime/target":
            return {"selected": None, "browser_connected": False}
        if path.endswith("/manual"):
            return {"endpoint_id": "manual:http://alternate.local:8765", "base_url": payload["base_url"]}
        return {"selected": {"base_url": "http://iii.local:8765"}}

    monkeypatch.setattr(module, "request_json", request)
    with pytest.raises(ValueError, match="different runtime"):
        module.bind("http://127.0.0.1:8781", "http://alternate.local:8765")


def test_repeated_binding_preserves_same_selected_runtime(monkeypatch):
    calls = []

    def request(_base, path, payload=None):
        calls.append(path)
        return {
            "selected": {
                "endpoint_id": "mdns:iii.local",
                "base_url": "http://iii.local:8765",
            },
            "browser_connected": True,
        }

    monkeypatch.setattr(module, "request_json", request)
    assert module.bind("http://127.0.0.1:8781", "http://iii.local:8765") == "mdns:iii.local"
    assert calls == ["/runtime/target"]


def test_repeated_binding_preserves_mdns_ipv4_selection(monkeypatch):
    def request(_base, path, _payload=None):
        if path == "/identity":
            return {"runtime_id": "iii-runtime", "host_label": "iii-drone", "profile": "hil"}
        return {
            "selected": {
                "endpoint_id": "mdns:pi",
                "base_url": "http://192.168.1.251:8765",
            },
            "browser_connected": True,
        }

    monkeypatch.setattr(module, "request_json", request)

    def addresses(host, *_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.251", 0))]

    import socket
    monkeypatch.setattr(module.socket, "getaddrinfo", addresses)
    assert module.bind("http://127.0.0.1:8781", "http://iii.local:8765") == "mdns:pi"


def test_stale_mdns_address_cannot_satisfy_repeated_binding(monkeypatch):
    import socket
    from urllib.error import URLError

    def request(base, path, _payload=None):
        if path == "/identity" and base == "http://10.42.0.15:8765":
            raise URLError("unreachable direct link")
        return {
            "selected": {"endpoint_id": "mdns:pi", "base_url": "http://10.42.0.15:8765"},
            "browser_connected": True,
        }

    monkeypatch.setattr(module, "request_json", request)
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.0.15", 0))],
    )
    with pytest.raises(ValueError, match="connected to an unreachable runtime"):
        module.bind("http://127.0.0.1:8781", "http://iii.local:8765")


def test_disconnected_stale_selection_is_replaced_with_reachable_host(monkeypatch):
    import socket
    from urllib.error import URLError

    calls = []

    def request(base, path, payload=None):
        calls.append((base, path))
        if path == "/runtime/target":
            return {
                "selected": {"endpoint_id": "mdns:old", "base_url": "http://10.42.0.15:8765"},
                "browser_connected": False,
            }
        if path == "/identity":
            raise URLError("stale direct link")
        if path == "/runtime/discovery/manual":
            return {"endpoint_id": "manual:http://iii.local:8765", "base_url": payload["base_url"]}
        return {"selected": {"base_url": "http://iii.local:8765"}}

    monkeypatch.setattr(module, "request_json", request)
    monkeypatch.setattr(
        module.socket,
        "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.0.15", 0))],
    )
    assert module.bind("http://127.0.0.1:8781", "http://iii.local:8765") == "manual:http://iii.local:8765"
    assert ("http://127.0.0.1:8781", "/runtime/target/select") in calls


def test_binding_refuses_switch_while_browser_connected(monkeypatch):
    monkeypatch.setattr(module, "request_json", lambda *_args: {
        "selected": {"base_url": "http://iii.local:8765"},
        "browser_connected": True,
    })
    with pytest.raises(ValueError, match="connected to a different runtime"):
        module.bind("http://127.0.0.1:8781", "http://alternate.local:8765")
