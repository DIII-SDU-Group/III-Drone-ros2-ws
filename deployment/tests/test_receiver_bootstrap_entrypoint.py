from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from iii_deployment.contracts import ContractError
from iii_deployment.receiver import bootstrap, server


class Candidate:
    def __init__(self) -> None:
        self.running = True
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated += 1
        self.running = False

    def wait(self, *, timeout):
        assert timeout == 5
        return 0

    def kill(self):
        self.killed += 1
        self.running = False


def test_apply_replaces_candidate_and_stops_final_child(monkeypatch, capsys):
    candidates = [Candidate(), Candidate()]

    class Recovery:
        def __init__(self, *_args, restart_receiver, **_kwargs):
            self.restart_receiver = restart_receiver

        def apply(self):
            self.restart_receiver()
            self.restart_receiver()
            return {"stage": "committed"}

        def reconcile(self):
            raise AssertionError("apply routed to reconcile")

    monkeypatch.setattr(bootstrap, "assert_production_root", lambda: None)
    monkeypatch.setattr(bootstrap, "ContractRegistry", lambda _root: object())
    monkeypatch.setattr(
        bootstrap, "ReceiverSlotStore", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(bootstrap, "ReceiverRecoveryBootstrap", Recovery)
    monkeypatch.setattr(bootstrap, "_spawn_candidate", lambda: candidates.pop(0))
    monkeypatch.setattr("sys.argv", ["iii-receiver-bootstrap", "--apply"])

    first = candidates[0]
    second = candidates[1]
    assert bootstrap.main() == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "committed"
    assert first.terminated == second.terminated == 1
    assert first.killed == second.killed == 0


def test_prepare_never_spawns_candidate(monkeypatch, capsys):
    class Recovery:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare_staging(self):
            return "b"

    monkeypatch.setattr(bootstrap, "assert_production_root", lambda: None)
    monkeypatch.setattr(bootstrap, "ContractRegistry", lambda _root: object())
    monkeypatch.setattr(
        bootstrap, "ReceiverSlotStore", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(bootstrap, "ReceiverRecoveryBootstrap", Recovery)
    monkeypatch.setattr(
        bootstrap,
        "_spawn_candidate",
        lambda: (_ for _ in ()).throw(
            AssertionError("candidate spawned during prepare")
        ),
    )
    monkeypatch.setattr("sys.argv", ["iii-receiver-bootstrap", "--prepare"])

    assert bootstrap.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "iii.receiver-update-prepare/v1",
        "inactive_slot": "b",
    }


def test_server_accepts_newer_active_slot_with_older_pending_control(monkeypatch):
    manifest = {"receiver_id": "a" * 64, "generation": 2}

    class Slots:
        def __init__(self, *_args, **_kwargs):
            pass

        def active_slot(self):
            return "b"

        def verify_slot(self, slot):
            assert slot == "b"
            return manifest

    monkeypatch.setattr(server, "ReceiverSlotStore", Slots)
    monkeypatch.setattr(
        server.ReceiverControlStore,
        "persisted_generation",
        lambda *_args, **_kwargs: 1,
    )
    slots, observed, control_generation = server._receiver_runtime(
        SimpleNamespace(receiver_generation=1), object()
    )

    assert isinstance(slots, Slots)
    assert observed == manifest
    assert control_generation == 1


def test_server_rejects_slot_older_than_baseline_or_control(monkeypatch):
    class Slots:
        def __init__(self, *_args, **_kwargs):
            pass

        def active_slot(self):
            return "a"

        def verify_slot(self, _slot):
            return {"receiver_id": "a" * 64, "generation": 1}

    monkeypatch.setattr(server, "ReceiverSlotStore", Slots)
    with pytest.raises(ContractError, match="predates"):
        server._receiver_runtime(SimpleNamespace(receiver_generation=2), object())

    monkeypatch.setattr(
        server.ReceiverControlStore,
        "persisted_generation",
        lambda *_args, **_kwargs: 2,
    )
    with pytest.raises(ContractError, match="newer"):
        server._receiver_runtime(SimpleNamespace(receiver_generation=1), object())
