from importlib.util import module_from_spec, spec_from_file_location
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).with_name("resolve_sim_fixture.py")
SPEC = spec_from_file_location("resolve_sim_fixture", SCRIPT)
assert SPEC and SPEC.loader
fixture = module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_fixture_cli_accepts_explicit_pi_host(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["resolve_sim_fixture.py", "--fixture-id", "test", "--profile", "hil", "--host", "pi.example"],
    )

    args = fixture.parse_args()

    assert args.profile == "hil"
    assert args.pi_host == "pi.example"


def test_fixture_route_resolution_uses_selected_pi_route(monkeypatch):
    monkeypatch.setattr(
        fixture.socket,
        "getaddrinfo",
        lambda *args: [(None, None, None, None, ("192.0.2.40", 0))],
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="192.0.2.40 dev wlan0 src 192.0.2.10\n", stderr="")

    monkeypatch.setattr(fixture.subprocess, "run", run)
    monkeypatch.setattr(fixture.socket, "create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.delenv("III_HIL_WORKSTATION_ADDRESS", raising=False)

    assert fixture.resolve_hil_network("pi.example") == ("192.0.2.40", "192.0.2.10")
    assert calls == [["ip", "-4", "route", "get", "192.0.2.40"]]


def test_fixture_skips_unreachable_first_dns_address(monkeypatch):
    monkeypatch.setattr(
        fixture.socket,
        "getaddrinfo",
        lambda *args: [
            (None, None, None, None, ("10.42.0.15", 0)),
            (None, None, None, None, ("192.168.1.251", 0)),
        ],
    )
    monkeypatch.setattr(
        fixture.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{command[-1]} dev eth0 src 192.168.1.10\n",
            stderr="",
        ),
    )
    attempts = []

    def connect(address, timeout):
        attempts.append((address, timeout))
        if address[0] == "10.42.0.15":
            raise OSError("unreachable stale address")
        return nullcontext()

    monkeypatch.setattr(fixture.socket, "create_connection", connect)
    monkeypatch.delenv("III_HIL_WORKSTATION_ADDRESS", raising=False)

    assert fixture.resolve_hil_network("pi.example") == ("192.168.1.251", "192.168.1.10")
    assert attempts == [(('10.42.0.15', 22), 0.75), (('192.168.1.251', 22), 0.75)]


def test_fixture_route_resolution_fails_closed_without_route(monkeypatch):
    monkeypatch.setattr(
        fixture.socket,
        "getaddrinfo",
        lambda *args: [(None, None, None, None, ("192.0.2.40", 0))],
    )
    monkeypatch.setattr(fixture.socket, "create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        fixture.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="unreachable"),
    )

    with pytest.raises(RuntimeError, match="no IPv4 workstation route"):
        fixture.resolve_hil_network("pi.example")


def test_fixture_route_rejects_workstation_override_for_another_link(monkeypatch):
    monkeypatch.setattr(
        fixture.socket,
        "getaddrinfo",
        lambda *args: [(None, None, None, None, ("192.0.2.40", 0))],
    )
    monkeypatch.setattr(
        fixture.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="192.0.2.40 dev wlan0 src 192.0.2.10\n", stderr=""),
    )
    monkeypatch.setenv("III_HIL_WORKSTATION_ADDRESS", "10.42.0.1")

    with pytest.raises(RuntimeError, match="does not match route"):
        fixture.resolve_hil_network("pi.example")
