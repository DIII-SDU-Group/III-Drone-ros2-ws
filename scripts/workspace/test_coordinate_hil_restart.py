from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).with_name("coordinate_hil_restart.py")
SPEC = importlib.util.spec_from_file_location("coordinate_hil_restart", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def vehicle(*, armed=False, in_air=False, checks=True):
    values = {
        "armed": armed,
        "in_air": in_air,
        "arming_checks_passed": checks,
    }
    return {
        **values,
        "source_availability": "available",
        "freshness": "fresh",
        "telemetry_fields": {
            name: {
                "value": value,
                "freshness": "fresh",
                "source_availability": "available",
                "disagreement": False,
            }
            for name, value in values.items()
        },
    }


class Client:
    def __init__(self, *, booted=True, state=None, profile="hil"):
        self.booted = booted
        self.status_calls = 0
        self.state = state or vehicle()
        self.profile = profile

    def identity(self):
        return {"profile": self.profile}

    def command(self, command_id, _parameters):
        assert command_id == "runtime.status"
        self.status_calls += 1
        # An initially unbooted fixture becomes booted after the coordinator's
        # first preflight observation and subsequent boot mutation.
        booted = self.booted or self.status_calls > 1
        return {
            "accepted": True,
            "result": {"daemon": {"booted": booted, "profile": "hil"}},
        }

    def vehicle_status(self):
        return self.state


class Runner:
    def __init__(self, *, workstation_healthy=True):
        self.calls = []
        self._workstation_healthy = workstation_healthy

    def workstation(self, action):
        self.calls.append(("workstation", action))

    def workstation_healthy(self):
        self.calls.append(("workstation", "status"))
        return self._workstation_healthy

    def system_mutation(self, action, *arguments):
        self.calls.append(("system", action, *arguments))


def test_coordinated_restart_orders_pi_shutdown_before_clock_reset():
    runner = Runner()
    module.coordinate_restart(Client(), runner, sleep=lambda _seconds: None)
    assert runner.calls == [
        ("system", "shutdown"),
        ("workstation", "stop"),
        ("workstation", "start"),
        ("system", "boot", "--profile", "hil"),
        ("system", "start"),
    ]


def test_unbooted_pi_skips_shutdown_but_still_uses_safe_order():
    runner = Runner()
    module.coordinate_restart(Client(booted=False), runner, sleep=lambda _seconds: None)
    assert runner.calls[0] == ("workstation", "stop")
    assert all(call[:2] != ("system", "shutdown") for call in runner.calls)


@pytest.mark.parametrize(
    "state",
    [
        vehicle(armed=True),
        vehicle(in_air=True),
        {**vehicle(), "freshness": "stale"},
        {**vehicle(), "source_availability": "degraded"},
    ],
)
def test_clock_reset_fails_closed_without_fresh_landed_disarmed_state(state):
    runner = Runner()
    with pytest.raises(module.HilRestartError, match="refusing HIL clock reset"):
        module.coordinate_restart(Client(state=state), runner)
    assert runner.calls == [("workstation", "status")]


def test_dead_workstation_allows_hil_runtime_shutdown_and_recovery_from_stale_state():
    class RecoveringClient(Client):
        def __init__(self):
            super().__init__()
            self.vehicle_calls = 0

        def vehicle_status(self):
            self.vehicle_calls += 1
            if self.vehicle_calls == 1:
                return {**vehicle(), "freshness": "stale"}
            return vehicle()

    runner = Runner(workstation_healthy=False)
    module.coordinate_restart(
        RecoveringClient(),
        runner,
        sleep=lambda _seconds: None,
    )
    assert runner.calls == [
        ("workstation", "status"),
        ("system", "shutdown"),
        ("workstation", "stop"),
        ("workstation", "start"),
        ("system", "boot", "--profile", "hil"),
        ("system", "start"),
    ]


def test_wrong_remote_profile_is_rejected_before_mutation():
    runner = Runner()
    with pytest.raises(module.HilRestartError, match="expected 'hil'"):
        module.coordinate_restart(Client(profile="real"), runner)
    assert runner.calls == []
