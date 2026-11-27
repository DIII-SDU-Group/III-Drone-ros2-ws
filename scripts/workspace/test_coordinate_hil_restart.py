from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


PATH = Path(__file__).with_name("coordinate_hil_restart.py")
SPEC = importlib.util.spec_from_file_location("coordinate_hil_restart", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
from iii.runtime_api_client import RuntimeApiError


@pytest.fixture(autouse=True)
def healthy_ground_control(monkeypatch):
    monkeypatch.setattr(
        module,
        "ground_control_health",
        lambda: {
            "state": "ready",
            "url": "http://127.0.0.1:5174",
            "frontend_ready": True,
            "proxy_ready": True,
        },
    )


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
            "result": {"daemon": {"booted": booted, "active": booted, "profile": "hil"}},
        }

    def vehicle_status(self):
        return self.state


class Runner:
    def __init__(
        self, *, workstation_healthy=True,
        workstation_stopped=False, rendered_mode="rendered", viewer_state="running",
        stop_error=None, shutdown_error=False, foreign_endpoint_after_stop=False,
        battery_error=False,
    ):
        self.calls = []
        self._workstation_healthy = workstation_healthy
        self._workstation_stopped = workstation_stopped
        self._rendered_mode = rendered_mode
        self._viewer_state = viewer_state
        self._stop_error = stop_error
        self._shutdown_error = shutdown_error
        self._foreign_endpoint_after_stop = foreign_endpoint_after_stop
        self._battery_error = battery_error

    def workstation(self, action):
        self.calls.append(("workstation", action))
        if action == "stop" and self._stop_error:
            raise module.HilRestartError(self._stop_error)

    def workstation_healthy(self, timeout_seconds=None):
        self.status_timeouts = getattr(self, "status_timeouts", []) + [timeout_seconds]
        return self.workstation_snapshot()["returncode"] == 0

    def battery_check(self):
        self.calls.append(("battery", "check"))
        if self._battery_error:
            raise module.HilRestartError("PX4 battery link unavailable")

    def workstation_snapshot(self, timeout_seconds=None):
        self.calls.append(("workstation", "status"))
        return {
            "returncode": 0 if self._workstation_healthy else 1,
            "fields": {
                "hil_readiness": "ready" if self._workstation_healthy else "degraded",
                "hil_simulation": "running" if self._workstation_healthy else "stopped",
                "hil_owner_state": "healthy" if self._workstation_healthy else "stopped",
                "hil_render_mode": self._rendered_mode,
                "hil_gazebo_viewer": self._viewer_state,
            },
        }

    def workstation_stopped(self):
        self.calls.append(("workstation", "stopped"))
        return self._workstation_stopped and not self._foreign_endpoint_after_stop

    def system_mutation(self, action, *arguments):
        self.calls.append(("system", action, *arguments))
        if action == "shutdown" and self._shutdown_error:
            raise subprocess.CalledProcessError(1, ["iii", "system", "shutdown"])


class FakeClock:
    """Monotonic time that advances only when the code under test sleeps."""

    def __init__(self, now=0.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_coordinated_restart_orders_pi_shutdown_before_clock_reset():
    runner = Runner()
    module.coordinate_restart(Client(), runner, sleep=lambda _seconds: None)
    assert runner.calls == [
        ("system", "shutdown"),
        ("workstation", "stop"),
        ("workstation", "start"),
        ("system", "boot", "--profile", "hil"),
        ("system", "start"),
        ("workstation", "status"),
        ("workstation", "status"),
        ("workstation", "status"),
        ("battery", "check"),
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
        vehicle(armed=None, in_air=None),
        {
            **vehicle(),
            "telemetry_fields": {
                **vehicle()["telemetry_fields"],
                "armed": {
                    **vehicle()["telemetry_fields"]["armed"],
                    "freshness": "stale",
                },
            },
        },
        {
            **vehicle(),
            "telemetry_fields": {
                **vehicle()["telemetry_fields"],
                "in_air": {
                    **vehicle()["telemetry_fields"]["in_air"],
                    "source_availability": "degraded",
                },
            },
        },
    ],
)
def test_virtual_restart_ignores_armed_and_airborne_telemetry(state):
    runner = Runner()
    module.coordinate_restart(Client(state=state), runner, sleep=lambda _seconds: None)
    assert runner.calls == [
        ("system", "shutdown"),
        ("workstation", "stop"),
        ("workstation", "start"),
        ("system", "boot", "--profile", "hil"),
        ("system", "start"),
        ("workstation", "status"),
        ("workstation", "status"),
        ("workstation", "status"),
        ("battery", "check"),
    ]


def test_wrong_remote_profile_is_rejected_before_mutation():
    runner = Runner()
    with pytest.raises(module.HilRestartError, match="expected 'hil'"):
        module.coordinate_restart(Client(profile="real"), runner)
    assert runner.calls == []


def test_process_runner_routes_lifecycle_mutations_to_the_remote_runtime(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.ProcessRunner().system_mutation("shutdown")

    assert len(calls) == 2
    assert all(call[1]["env"]["CLI_CONFIGURATION"] == "remote" for call in calls)


def test_process_runner_defers_pi_graph_gate_only_while_starting_workstation(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    runner = module.ProcessRunner()

    runner.workstation("start")
    runner.workstation("stop")

    assert "III_HIL_DEFER_PI_GRAPH_READINESS" not in calls[0][1]["env"]
    assert "III_HIL_DEFER_PI_GRAPH_READINESS" not in calls[1][1]["env"]


def test_workstation_failure_surfaces_launcher_reason_and_preserves_output(monkeypatch, capsys):
    message = "The III workspace devcontainer is not running; HIL cannot start safely."
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, message + "\n", None),
    )

    with pytest.raises(module.HilRestartError, match=r"workstation stop failed \(exit 1\): The III workspace devcontainer is not running"):
        module.ProcessRunner().workstation("stop")
    assert message in capsys.readouterr().out


def test_pi_lifecycle_failure_reports_cli_finding_before_next_step(monkeypatch):
    output = "Findings:\nIII_RUNTIME_DENIED: daemon unavailable\nNext:\niii system status\n"
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 2, output, None),
    )
    with pytest.raises(module.HilRestartError, match=r"Pi system shutdown plan failed \(exit 2\): III_RUNTIME_DENIED: daemon unavailable"):
        module.ProcessRunner().system_mutation("shutdown")


def test_hil_status_includes_workstation_launcher_failure_reason(monkeypatch):
    message = "The III workspace devcontainer is not running; HIL cannot start safely."
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", message + "\n"),
    )
    state = module.profile_status(Client(), module.ProcessRunner(), tolerate_unavailable=True)
    assert state["components"]["workstation"] == "degraded"
    assert state["diagnostics"]["workstation"] == [message]


def test_routed_cli_failure_surfaces_underlying_runtime_transcript(monkeypatch, capsys):
    reason = "configuration artifact hash mismatch: tracked_defaults/sim/default.yaml"
    transcript = json.dumps({
        "code": "III_RUNTIME_ROUTE_FAILED",
        "payload": {"runtime_result": {
            "code": "III_SYSTEM_BOOT_FAILED",
            "findings": [{"message": "legacy handler exited with status 1"}],
            "payload": {"display": reason},
        }},
    })
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 30, transcript),
    )
    with pytest.raises(module.HilRestartError) as failure:
        module._run_lifecycle_child(["iii", "system", "boot"], label="Pi system boot", environment={})
    assert str(failure.value) == "Pi system boot failed (exit 30): " + reason
    assert transcript in capsys.readouterr().out


@pytest.mark.parametrize("output", ["null", '{"payload":null}', '{"payload":{"display":12}}'])
def test_malformed_structured_failure_retains_text_fallback(output):
    assert module._failure_reason(output) == output


def test_start_core_gate_ignores_ground_control_health(monkeypatch):
    monkeypatch.setattr(
        module,
        "ground_control_health",
        lambda: (_ for _ in ()).throw(AssertionError("GC must not gate start")),
    )
    runner = Runner()
    module.coordinate_start(Client(state=vehicle(armed=True, in_air=True)), runner)
    assert runner.calls == [("workstation", "status"), ("workstation", "start"), ("battery", "check")]


def test_start_is_idempotent_even_during_an_active_flight():
    runner = Runner()
    module.coordinate_start(Client(state=vehicle(armed=True, in_air=True)), runner)
    assert runner.calls == [("workstation", "status"), ("workstation", "start"), ("battery", "check")]


def test_start_reports_missing_battery_without_restarting_running_hil():
    runner = Runner(battery_error=True)
    with pytest.raises(module.HilRestartError, match="PX4 battery link unavailable"):
        module.coordinate_start(Client(state=vehicle(armed=True, in_air=True)), runner)
    assert runner.calls == [("workstation", "status"), ("workstation", "start"), ("battery", "check")]


def test_start_waits_for_refused_api_without_mutating_healthy_flight():
    from urllib.error import URLError
    client = Client(state=vehicle(armed=True, in_air=True))
    reads = 0
    def identity():
        nonlocal reads
        reads += 1
        if reads < 3:
            raise RuntimeApiError("API opening its port") from URLError(ConnectionRefusedError())
        return {"profile": "hil"}
    client.identity = identity
    runner = Runner()
    sleeps = []
    module.coordinate_start(client, runner, sleep=sleeps.append)
    assert reads == 3
    assert len(sleeps) == 2
    assert runner.calls == [("workstation", "status"), ("workstation", "start"), ("battery", "check")]


def test_start_api_wait_is_bounded_without_mutation():
    from urllib.error import URLError
    def unavailable():
        raise RuntimeApiError("refused") from URLError(ConnectionRefusedError())
    client = Client()
    client.identity = unavailable
    runner = Runner()
    times = iter([0.0, 0.0, 1.0, 2.0])
    with pytest.raises(module.HilRestartError, match="API.*unavailable"):
        module.coordinate_start(client, runner, timeout_seconds=2,
            monotonic=lambda: next(times), sleep=lambda _: None)
    assert runner.calls == []


def test_start_does_not_retry_api_authentication_failure():
    from urllib.error import HTTPError
    def unauthorized():
        raise RuntimeApiError("unauthorized") from HTTPError("http://pi", 401, "unauthorized", {}, None)
    client = Client()
    client.identity = unauthorized
    runner = Runner()
    sleeps = []
    with pytest.raises(RuntimeApiError, match="unauthorized"):
        module.coordinate_start(client, runner, sleep=sleeps.append)
    assert sleeps == []
    assert runner.calls == []


def test_degraded_workstation_fails_readiness_after_restart():
    state = vehicle()
    state["telemetry_fields"]["armed"]["freshness"] = "stale"
    runner = Runner(workstation_healthy=False)
    times = iter(range(100))
    with pytest.raises(module.HilRestartError, match="adapter/cross-host readiness"):
        module.coordinate_restart(Client(booted=False, state=state), runner,
                                  timeout_seconds=3, monotonic=lambda: next(times),
                                  sleep=lambda _seconds: None)
    assert ("workstation", "stop") in runner.calls


def test_stop_stops_the_owned_workstation_before_shutting_down_pi():
    runner = Runner(workstation_stopped=True)
    module.coordinate_stop(Client(), runner)
    assert runner.calls == [
        ("workstation", "stop"),
        ("workstation", "stopped"),
        ("system", "shutdown"),
        ("workstation", "stopped"),
    ]


def test_stop_rejects_missing_workstation_proof_before_pi_shutdown():
    runner = Runner(workstation_stopped=False)
    with pytest.raises(
        module.HilRestartError,
        match="workstation stop returned, but canonical ownership/stopped proof remains unavailable; Pi runtime shutdown was not attempted",
    ):
        module.coordinate_stop(Client(), runner)
    assert runner.calls == [
        ("workstation", "stop"),
        ("workstation", "stopped"),
    ]


def test_owned_stop_with_foreign_endpoint_stops_px4_but_preserves_pi_runtime():
    # The launcher can successfully stop its recorded PX4 while preserving an
    # unrelated endpoint occupant; final ownership proof must prevent Pi stop.
    runner = Runner(
        workstation_stopped=True, foreign_endpoint_after_stop=True
    )
    with pytest.raises(
        module.HilRestartError,
        match="Pi runtime shutdown was not attempted",
    ):
        module.coordinate_stop(Client(), runner)

    assert runner.calls == [
        ("workstation", "stop"),
        ("workstation", "stopped"),
    ]


def test_stop_allows_fresh_airborne_virtual_hil_then_requires_stopped_proof():
    runner = Runner(workstation_stopped=True)
    module.coordinate_stop(
        Client(state=vehicle(armed=True, in_air=True)), runner
    )
    assert runner.calls == [
        ("workstation", "stop"),
        ("workstation", "stopped"),
        ("system", "shutdown"),
        ("workstation", "stopped"),
    ]


def test_stop_rejects_non_hil_identity_before_mutation():
    runner = Runner(workstation_stopped=True)
    with pytest.raises(module.HilRestartError, match="expected 'hil'"):
        module.coordinate_stop(Client(profile="real"), runner)
    assert runner.calls == []


def test_stop_rejects_mismatched_pi_daemon_profile_before_mutation():
    class WrongDaemonProfileClient(Client):
        def command(self, command_id, parameters):
            response = super().command(command_id, parameters)
            response["result"]["daemon"]["profile"] = "real"
            return response

    runner = Runner(workstation_stopped=True)
    with pytest.raises(module.HilRestartError, match="Pi daemon profile.*expected 'hil'"):
        module.coordinate_stop(WrongDaemonProfileClient(), runner)
    assert runner.calls == []


@pytest.mark.parametrize("reason", ["unowned PX4", "ambiguous PX4 owner"])
def test_workstation_stop_rejects_unowned_or_ambiguous_owner_before_pi_mutation(reason):
    runner = Runner(stop_error=reason)
    with pytest.raises(module.HilRestartError, match=reason):
        module.coordinate_stop(
            Client(state=vehicle(armed=True, in_air=True)), runner
        )
    assert runner.calls == [("workstation", "stop")]


def test_pi_shutdown_failure_reports_workstation_as_already_stopped():
    runner = Runner(workstation_stopped=True, shutdown_error=True)
    with pytest.raises(
        module.HilRestartError,
        match="workstation HIL components are stopped, but Pi runtime shutdown failed",
    ):
        module.coordinate_stop(
            Client(state=vehicle(armed=True, in_air=True)), runner
        )
    assert runner.calls == [
        ("workstation", "stop"),
        ("workstation", "stopped"),
        ("system", "shutdown"),
    ]


@pytest.mark.parametrize("output,expected", [
    ("canonical_px4_process_alive: no\ncanonical_tmux_session: stopped\nhil_owner_state: stopped\ncanonical_endpoint_snapshot: ready\ncanonical_xrce_endpoint_ownership: no\nconflicting_px4_owners: count=0 pids=\nhil_pi_reachability: ready\n", True),
    ("canonical_px4_process_alive: no\ncanonical_tmux_session: stopped\nhil_owner_state: stopped\ncanonical_endpoint_snapshot: unavailable\nhil_pi_reachability: ready\n", False),
    ("canonical_px4_process_alive: yes\ncanonical_tmux_session: running\nhil_owner_state: healthy\nhil_pi_reachability: ready\n", False),
    ("hil_readiness: degraded\n", False),
    ("canonical_px4_process_alive: no\ncanonical_tmux_session: stopped\nhil_owner_state: stopped\ncanonical_endpoint_snapshot: ready\ncanonical_xrce_endpoint_ownership: yes\nconflicting_px4_owners: count=0 pids=\nhil_pi_reachability: ready\n", False),
    ("canonical_px4_process_alive: no\ncanonical_tmux_session: stopped\nhil_owner_state: stopped\ncanonical_endpoint_snapshot: ready\ncanonical_xrce_endpoint_ownership: no\nconflicting_px4_owners: count=2 pids=41 42\nhil_pi_reachability: ready\n", False),
])
def test_stopped_detection_requires_explicit_ownership_evidence(monkeypatch, output, expected):
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: type("Result", (), {"stdout": output, "returncode": 1})())
    assert module.ProcessRunner().workstation_stopped() is expected


def test_restart_requires_full_workstation_readiness_after_pi_boot():
    runner = Runner(workstation_healthy=False)
    clock = FakeClock()
    with pytest.raises(module.HilRestartError, match="adapter/cross-host readiness"):
        module.coordinate_restart(
            Client(), runner, timeout_seconds=3.0, poll_seconds=1.0,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
    assert runner.calls.index(("system", "start")) < runner.calls.index(("workstation", "status"))
    assert runner.calls.count(("workstation", "status")) == 3


def test_restart_confirmation_interval_zero_takes_back_to_back_samples():
    sleeps = []
    module.coordinate_restart(
        Client(), Runner(), confirm_interval_seconds=0, sleep=sleeps.append
    )
    assert sleeps == []


def test_restart_spaces_stability_confirmation_samples_by_default():
    clock = FakeClock()
    runner = Runner()
    module.coordinate_restart(
        Client(), runner, monotonic=clock.monotonic, sleep=clock.sleep
    )
    # Three ready samples, separated by two confirmation intervals.
    assert clock.sleeps == [module.DEFAULT_CONFIRM_INTERVAL_SEC] * 2
    assert runner.calls.count(("workstation", "status")) == 3


def test_restart_confirmation_interval_never_overruns_the_deadline():
    clock = FakeClock()
    with pytest.raises(module.HilRestartError, match="timed out"):
        module.coordinate_restart(
            Client(), Runner(), timeout_seconds=10.0, confirm_interval_seconds=60.0,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
    assert clock.sleeps == [10.0]
    assert clock.now == 10.0


def test_readiness_calls_are_bounded_by_the_remaining_deadline():
    class TimedClient(Client):
        def __init__(self, clock):
            super().__init__()
            self.clock = clock
            self.timeout_seconds = 210.0
            self.observed = []

        def command(self, command_id, parameters):
            self.observed.append(("runtime.status", self.timeout_seconds))
            # Every call consumes wall time, as a slow API would.
            self.clock.now += 5.0
            return super().command(command_id, parameters)

        def vehicle_status(self):
            self.observed.append(("vehicle", self.timeout_seconds))
            self.clock.now += 5.0
            return super().vehicle_status()

    clock = FakeClock()
    client = TimedClient(clock)
    runner = Runner(workstation_healthy=False)
    with pytest.raises(module.HilRestartError, match="timed out"):
        module.coordinate_restart(
            client, runner, timeout_seconds=30.0, poll_seconds=1.0,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
    readiness = client.observed[1:]  # skip the preflight status read
    assert readiness, "readiness loop made no Runtime API calls"
    assert all(timeout <= 30.0 for _name, timeout in readiness)
    # Each request is capped by the time left, so later calls get less.
    assert readiness[-1][1] < readiness[0][1]
    # The client's configured timeout is restored after every bounded call.
    assert client.timeout_seconds == 210.0
    assert runner.status_timeouts and all(
        value is not None and value <= 30.0 for value in runner.status_timeouts
    )
    # The loop never ran past its deadline by more than one bounded call.
    assert clock.now <= 30.0 + 5.0 + 5.0


def _progress_lines(output):
    lines = [line for line in output.splitlines() if line.startswith("III_HIL_PROGRESS|")]
    for line in lines:
        marker, stage, state, detail = line.split("|", 3)
        assert marker == "III_HIL_PROGRESS"
        assert stage.replace("_", "").isalpha()
        assert state.replace("_", "").isalpha()
        assert detail and "\n" not in detail
    return lines


def test_restart_progress_events_follow_lifecycle_order(monkeypatch, capsys):
    monkeypatch.setenv("III_HIL_PROGRESS", "1")
    module.coordinate_restart(Client(), Runner(), sleep=lambda _seconds: None)
    lines = _progress_lines(capsys.readouterr().out)
    stages = [(line.split("|", 3)[1], line.split("|", 3)[2]) for line in lines]
    expected = [
        ("pi_api_identity", "start"), ("pi_api_identity", "done"),
        ("pi_runtime_status", "start"), ("pi_runtime_status", "done"),
        ("pi_shutdown", "start"), ("pi_shutdown", "done"),
        ("workstation_stop", "start"), ("workstation_stop", "done"),
        ("workstation_start", "start"), ("workstation_start", "done"),
        ("pi_boot", "start"), ("pi_boot", "done"),
        ("pi_start", "start"), ("pi_start", "done"),
        ("battery_link", "start"), ("battery_link", "done"),
    ]
    assert [item for item in stages if item[0] != "readiness"] == expected
    assert ("readiness", "waiting") in stages
    assert ("readiness", "update") in stages
    assert stages[-1] == ("readiness", "done")


def test_idempotent_start_reports_running_core_and_viewer_repair(monkeypatch, capsys):
    monkeypatch.setenv("III_HIL_PROGRESS", "1")
    module.coordinate_start(Client(), Runner(), sleep=lambda _seconds: None)
    lines = _progress_lines(capsys.readouterr().out)
    assert any("idempotent_start|done|core already running; repairing viewer if needed" in line for line in lines)
    assert any("viewer_repair|start|" in line for line in lines)
    assert any("viewer_repair|done|" in line for line in lines)
    assert not any("safety_check|" in line or "pi_shutdown|" in line for line in lines)


def test_progress_is_disabled_by_default(monkeypatch, capsys):
    monkeypatch.delenv("III_HIL_PROGRESS", raising=False)
    module.coordinate_restart(Client(), Runner(), sleep=lambda _seconds: None)
    assert "III_HIL_PROGRESS|" not in capsys.readouterr().out


def test_progress_wait_updates_only_at_state_change_or_twenty_second_interval(
    monkeypatch, capsys,
):
    monkeypatch.setenv("III_HIL_PROGRESS", "1")
    clock = FakeClock()
    runner = Runner(workstation_healthy=False)
    with pytest.raises(module.HilRestartError, match="timed out"):
        module.coordinate_restart(
            Client(), runner, timeout_seconds=24.0, poll_seconds=2.0,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
    updates = [
        line for line in _progress_lines(capsys.readouterr().out)
        if "|readiness|update|" in line
    ]
    assert len(updates) == 2
    assert all("workstation=waiting" in line for line in updates)


def test_readiness_progress_does_not_add_workstation_probes_before_pi_ready(
    monkeypatch, capsys,
):
    monkeypatch.setenv("III_HIL_PROGRESS", "1")
    client = Client(booted=False)
    client.command = lambda *_args: {
        "accepted": True,
        "result": {"daemon": {"booted": False, "active": False, "profile": "hil"}},
    }
    runner = Runner()
    ticks = iter(range(0, 30, 2))
    with pytest.raises(module.HilRestartError, match="timed out"):
        module.coordinate_restart(
            client, runner, timeout_seconds=8.0,
            monotonic=lambda: next(ticks), sleep=lambda _seconds: None,
        )
    assert ("workstation", "status") not in runner.calls
    assert "workstation=pending" in capsys.readouterr().out


def test_status_json_suppresses_opt_in_progress_markers(monkeypatch, capsys):
    monkeypatch.setenv("III_HIL_PROGRESS", "1")
    client = Client()
    runner = Runner()
    monkeypatch.setattr(module.RuntimeApiClient, "from_env", lambda: client)
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: runner)
    monkeypatch.setattr(sys, "argv", [str(PATH), "status", "--json"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "III_HIL_PROGRESS|" not in output
    assert json.loads(output)["state"] == "ready"


def test_host_argument_overrides_inherited_runtime_targets(monkeypatch, capsys):
    for key, value in {
        "III_HIL_PI_ADDRESS": "10.42.0.15",
        "III_HIL_PI_ENDPOINT": "old.local",
        "III_SSH_HOST": "old.local",
        "III_RUNTIME_HOST": "old.local",
        "III_RUNTIME_API_HOST": "old.local",
        "III_RUNTIME_API_URL": "http://old.local:8765",
    }.items():
        monkeypatch.setenv(key, value)
    observed = {}

    def from_env():
        observed.update({key: __import__("os").environ.get(key) for key in (
            "III_HIL_PI_ADDRESS", "III_HIL_PI_ENDPOINT", "III_SSH_HOST",
            "III_RUNTIME_HOST", "III_RUNTIME_API_HOST", "III_RUNTIME_API_URL",
        )})
        return Client()

    monkeypatch.setattr(module.RuntimeApiClient, "from_env", from_env)
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: Runner())
    monkeypatch.setattr(sys, "argv", [str(PATH), "status", "--json", "--host", "alternate.local"])
    assert module.main() == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "ready"
    assert status["pi_target"]["host"] == "alternate.local"
    assert observed == {
        "III_HIL_PI_ADDRESS": None,
        "III_HIL_PI_ENDPOINT": "alternate.local",
        "III_SSH_HOST": "alternate.local",
        "III_RUNTIME_HOST": "alternate.local",
        "III_RUNTIME_API_HOST": "alternate.local",
        "III_RUNTIME_API_URL": "http://alternate.local:8765",
    }


def test_process_runner_forwards_explicit_host_to_all_launcher_reads_and_writes(monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "hil_readiness: ready\n", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    runner = module.ProcessRunner(host="alternate.local")
    runner.workstation("start")
    runner.workstation_snapshot()
    runner.workstation_stopped()
    assert [command[1:] for command in calls] == [
        ["start", "--host", "alternate.local"],
        ["status", "--host", "alternate.local"],
        ["status", "--host", "alternate.local"],
    ]


def test_start_waits_for_daemon_socket_without_mutating_healthy_flight():
    client = Client(state=vehicle(armed=True, in_air=True))
    original = client.command
    reads = []
    def command(command_id, parameters):
        reads.append(command_id)
        if len(reads) < 3:
            return {"accepted": False, "message": "[Errno 2] No such file or directory",
                    "rejection": None, "result": {"permission": "read_only"}}
        return original(command_id, parameters)
    client.command = command
    runner = Runner()
    sleeps = []
    module.coordinate_start(client, runner, sleep=sleeps.append)
    assert len(reads) == 3
    assert len(sleeps) == 2
    assert runner.calls == [("workstation", "status"), ("workstation", "start"), ("battery", "check")]


def test_daemon_socket_wait_is_bounded_before_any_mutation():
    client = Client()
    client.command = lambda *args: {"accepted": False,
        "message": "[Errno 111] Connection refused", "rejection": None,
        "result": {"permission": "read_only"}}
    runner = Runner()
    times = iter([0.0, 0.0, 1.0, 2.0])
    with pytest.raises(module.HilRestartError, match="daemon socket remained unavailable"):
        module.coordinate_start(client, runner, timeout_seconds=2,
            monotonic=lambda: next(times), sleep=lambda _: None)
    assert runner.calls == []


def test_daemon_permission_error_is_not_retried():
    client = Client()
    client.command = lambda *args: {"accepted": False,
        "message": "[Errno 13] Permission denied", "rejection": None,
        "result": {"permission": "read_only"}}
    runner = Runner()
    with pytest.raises(module.HilRestartError, match="Permission denied"):
        module.coordinate_start(client, runner,
            sleep=lambda _: pytest.fail("permission error must not retry"))
    assert runner.calls == []


@pytest.mark.parametrize(
    ("runner", "ground_control", "expected"),
    [
        (Runner(), "ready", "ready"),
        (Runner(rendered_mode="headless", viewer_state="missing"), "ready", "ready"),
        (Runner(viewer_state="missing"), "ready", "degraded"),
        (Runner(rendered_mode="unknown"), "ready", "running"),
        (Runner(), "degraded", "degraded"),
        (Runner(workstation_healthy=False), "ready", "stopped"),
    ],
)
def test_status_classifies_ready_running_degraded_and_stopped(
    monkeypatch, runner, ground_control, expected,
):
    monkeypatch.setenv("III_GC_INSTALL_ROOT", "/tmp/installed-gc")
    monkeypatch.setenv("III_GC_INSTALL_PROFILE", "deploy")
    monkeypatch.setattr(
        module,
        "ground_control_health",
        lambda: {
            "state": ground_control,
            "url": "http://127.0.0.1:5174",
            "frontend_ready": ground_control == "ready",
            "proxy_ready": ground_control == "ready",
        },
    )
    state = module.profile_status(Client(booted=expected != "stopped"), runner)
    assert state["state"] == expected
    assert state["gc_url"] == "http://127.0.0.1:5174"
    assert state["components"]["ground_control"] == ground_control
    assert state["viewer_state"] == runner._viewer_state
    assert state["native_install"]["root"] == "/tmp/installed-gc"
    assert state["native_install"]["install_profile"] == "deploy"
    assert state["native_install"]["runtime_profile"] == "hil"
    assert state["native_install"]["host"]


def test_process_runner_forwards_rendered_selection_to_workstation(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.ProcessRunner(rendered=False).workstation("start")
    module.ProcessRunner(rendered=True).workstation("start")
    assert calls[0][1]["env"]["III_HIL_RENDERED"] == "0"
    assert calls[1][1]["env"]["III_HIL_RENDERED"] == "1"


def test_status_text_is_emitted_when_pi_identity_is_unavailable(
    monkeypatch, capsys,
):
    class IdentityUnavailable(Client):
        def identity(self):
            raise RuntimeApiError("connection refused")

    runner = Runner()
    client = IdentityUnavailable()
    monkeypatch.setattr(module.RuntimeApiClient, "from_env", lambda: client)
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: runner)
    monkeypatch.setattr(sys, "argv", [str(PATH), "status"])

    assert module.main() == 1
    output = capsys.readouterr()
    assert "HIL · degraded" in output.out
    assert "Pi runtime:      unknown" in output.out
    assert "Pi detail:       identity unavailable: connection refused" in output.out
    assert client.status_calls == 0
    assert runner.calls == [("workstation", "status")]


def test_status_json_is_emitted_when_pi_runtime_status_is_unavailable(
    monkeypatch, capsys,
):
    class StatusUnavailable(Client):
        def command(self, _command_id, _parameters):
            raise RuntimeApiError("runtime API timed out")

    runner = Runner()
    monkeypatch.setattr(
        module.RuntimeApiClient, "from_env", lambda: StatusUnavailable()
    )
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: runner)
    monkeypatch.setattr(sys, "argv", [str(PATH), "status", "--json"])

    assert module.main() == 1
    output = capsys.readouterr()
    status = json.loads(output.out)
    assert status["state"] == "degraded"
    assert status["components"]["pi_runtime"] == "unknown"
    assert status["components"]["workstation"] == "ready"
    assert status["components"]["ground_control"] == "ready"
    assert status["diagnostics"]["pi_runtime"] == [
        "status unavailable: runtime API timed out"
    ]
    assert runner.calls == [("workstation", "status")]


def _unavailable(cause):
    error = RuntimeApiError(f"Runtime API unavailable: {cause}")
    error.__cause__ = cause
    return error


def _transient_causes():
    import errno
    import socket
    from urllib.error import URLError
    return [
        URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
        URLError(OSError(errno.EHOSTUNREACH, "No route to host")),
        URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
        URLError(OSError(errno.ETIMEDOUT, "Connection timed out")),
        URLError(socket.timeout("timed out")),
        URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known")),
        TimeoutError("read timed out"),
        ConnectionResetError(errno.ECONNRESET, "reset"),
    ]


@pytest.mark.parametrize("cause", _transient_causes(), ids=lambda cause: repr(cause)[:40])
def test_identity_wait_retries_transient_unreachability(cause):
    client = Client()
    reads = 0

    def identity():
        nonlocal reads
        reads += 1
        if reads < 3:
            raise _unavailable(cause)
        return {"profile": "hil"}

    client.identity = identity
    clock = FakeClock()
    assert module.wait_runtime_identity(
        client, monotonic=clock.monotonic, sleep=clock.sleep
    ) == {"profile": "hil"}
    assert reads == 3
    assert clock.sleeps == [2.0, 2.0]


@pytest.mark.parametrize("cause_factory", [
    lambda: __import__("urllib.error").error.HTTPError("http://pi", 404, "missing", {}, None),
    lambda: __import__("urllib.error").error.HTTPError("http://pi", 503, "busy", {}, None),
    lambda: __import__("urllib.error").error.URLError("unknown url type: htp"),
    lambda: PermissionError(13, "Permission denied"),
    lambda: None,
])
def test_identity_wait_fails_fast_on_non_transient_errors(cause_factory):
    client = Client()
    cause = cause_factory()

    def identity():
        if cause is None:
            raise RuntimeApiError("Runtime API returned invalid JSON")
        raise _unavailable(cause)

    client.identity = identity
    with pytest.raises(RuntimeApiError):
        module.wait_runtime_identity(
            client, sleep=lambda _seconds: pytest.fail("must not retry")
        )


def test_identity_wait_for_unreachable_host_is_bounded_and_per_call_capped():
    import errno
    from urllib.error import URLError
    clock = FakeClock()
    client = Client()
    client.timeout_seconds = 210.0
    observed = []

    def identity():
        observed.append(client.timeout_seconds)
        clock.now += 1.0
        raise _unavailable(URLError(OSError(errno.EHOSTUNREACH, "No route to host")))

    client.identity = identity
    with pytest.raises(module.HilRestartError, match="Runtime API remained unavailable"):
        module.wait_runtime_identity(
            client, timeout_seconds=10.0, monotonic=clock.monotonic, sleep=clock.sleep
        )
    assert observed[0] == 10.0
    assert all(value <= 10.0 for value in observed)
    assert client.timeout_seconds == 210.0
    # First failed call, then the retry window, then one final bounded call.
    assert clock.now <= 1.0 + 10.0 + 1.0


def test_runtime_status_wait_retries_transient_api_unreachability():
    import errno
    from urllib.error import URLError
    client = Client()
    original = client.command
    reads = 0

    def command(command_id, parameters):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise _unavailable(URLError(OSError(errno.EHOSTUNREACH, "No route to host")))
        return original(command_id, parameters)

    client.command = command
    clock = FakeClock()
    result = module.wait_runtime_status(client, monotonic=clock.monotonic, sleep=clock.sleep)
    assert result["daemon"]["profile"] == "hil"
    assert reads == 2


def _stop_lines(output):
    return [line.split("|", 3)[1:] for line in output.splitlines() if line.startswith("III_HIL_STOP|")]


def test_stop_with_unreachable_pi_still_stops_workstation_and_fails(capsys):
    import errno
    from urllib.error import URLError
    client = Client()

    def identity():
        raise _unavailable(URLError(OSError(errno.EHOSTUNREACH, "No route to host")))

    client.identity = identity
    runner = Runner(workstation_stopped=True)
    clock = FakeClock()
    with pytest.raises(module.HilRestartError, match=r"Pi runtime unreachable \(identity unavailable: .*No route to host.*\); owned workstation HIL components were stopped, but the Pi runtime was not shut down"):
        module.coordinate_stop(
            client, runner, timeout_seconds=4.0,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
    assert runner.calls == [("workstation", "stop")]
    assert all(call[0] != "system" for call in runner.calls)
    assert clock.now <= 4.0
    lines = _stop_lines(capsys.readouterr().out)
    assert [line[:2] for line in lines] == [
        ["pi_runtime", "unreachable"], ["workstation", "stopped"],
    ]


def test_stop_without_a_usable_client_still_stops_workstation(capsys):
    runner = Runner(workstation_stopped=True)
    with pytest.raises(module.HilRestartError, match="client unavailable: token missing"):
        module.coordinate_stop(None, runner, client_error="token missing")
    assert runner.calls == [("workstation", "stop")]
    assert [line[:2] for line in _stop_lines(capsys.readouterr().out)] == [
        ["pi_runtime", "unreachable"], ["workstation", "stopped"],
    ]


def test_stop_with_unreachable_pi_reports_workstation_failure_too(capsys):
    client = Client()
    client.identity = lambda: (_ for _ in ()).throw(RuntimeApiError("unauthorized"))
    runner = Runner(stop_error="unowned PX4")
    with pytest.raises(module.HilRestartError, match="workstation stop also failed: unowned PX4"):
        module.coordinate_stop(client, runner, sleep=lambda _seconds: None)
    assert runner.calls == [("workstation", "stop")]
    assert [line[:2] for line in _stop_lines(capsys.readouterr().out)] == [
        ["pi_runtime", "unreachable"], ["workstation", "failed"],
    ]


def test_stop_with_unavailable_pi_status_still_stops_workstation():
    client = Client()
    client.command = lambda *_args: {
        "accepted": False, "message": "rejected", "rejection": {"message": "busy"},
    }
    runner = Runner(workstation_stopped=True)
    with pytest.raises(module.HilRestartError, match="status unavailable: runtime.status failed: busy"):
        module.coordinate_stop(client, runner)
    assert runner.calls == [("workstation", "stop")]


def test_successful_stop_reports_each_component(capsys):
    runner = Runner(workstation_stopped=True)
    module.coordinate_stop(Client(), runner)
    assert [line[:2] for line in _stop_lines(capsys.readouterr().out)] == [
        ["workstation", "stopped"], ["pi_runtime", "stopped"],
    ]
    capsys.readouterr()
    module.coordinate_stop(Client(booted=False), Runner(workstation_stopped=True))
    assert [line[:2] for line in _stop_lines(capsys.readouterr().out)] == [
        ["workstation", "stopped"], ["pi_runtime", "already_stopped"],
    ]


def test_main_stop_honours_restart_timeouts_and_survives_client_errors(monkeypatch, capsys):
    observed = {}

    def fake_stop(client, runner, **kwargs):
        observed.update(kwargs, client=client)

    monkeypatch.setattr(module, "coordinate_stop", fake_stop)
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: Runner())
    monkeypatch.setattr(
        module.RuntimeApiClient, "from_env",
        lambda: (_ for _ in ()).throw(RuntimeApiError("token file missing")),
    )
    monkeypatch.setenv("III_HIL_RESTART_TIMEOUT_SEC", "12")
    monkeypatch.setenv("III_HIL_RESTART_POLL_SEC", "0.5")
    monkeypatch.setattr(sys, "argv", [str(PATH), "stop"])
    assert module.main() == 0
    assert observed == {
        "client": None, "timeout_seconds": 12.0, "poll_seconds": 0.5,
        "client_error": "token file missing",
    }


def test_main_restart_forwards_confirmation_interval(monkeypatch):
    observed = {}
    monkeypatch.setattr(module, "coordinate_restart", lambda client, runner, **kwargs: observed.update(kwargs))
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: Runner())
    monkeypatch.setattr(module.RuntimeApiClient, "from_env", lambda: Client())
    monkeypatch.setenv("III_HIL_RESTART_CONFIRM_INTERVAL_SEC", "0")
    monkeypatch.setattr(sys, "argv", [str(PATH), "restart"])
    assert module.main() == 0
    assert observed["confirm_interval_seconds"] == 0.0


def test_main_rejects_invalid_restart_timeout(monkeypatch, capsys):
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: Runner())
    monkeypatch.setenv("III_HIL_RESTART_TIMEOUT_SEC", "forever")
    monkeypatch.setattr(sys, "argv", [str(PATH), "restart"])
    assert module.main() == 1
    assert "III_HIL_RESTART_TIMEOUT_SEC" in capsys.readouterr().err


def test_status_caps_runtime_api_timeout(monkeypatch, capsys):
    client = Client()
    client.timeout_seconds = 210.0
    seen = []
    client.identity = lambda: seen.append(client.timeout_seconds) or {"profile": "hil"}
    monkeypatch.setattr(module.RuntimeApiClient, "from_env", lambda: client)
    monkeypatch.setattr(module, "ProcessRunner", lambda **_kwargs: Runner())
    monkeypatch.setenv("III_HIL_STATUS_API_TIMEOUT_SEC", "4")
    monkeypatch.setattr(sys, "argv", [str(PATH), "status", "--json"])
    assert module.main() == 0
    assert seen == [4.0]


def test_workstation_status_probe_is_bounded(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", run)
    runner = module.ProcessRunner()
    snapshot = runner.workstation_snapshot()
    assert snapshot["returncode"] == 124
    assert "timed out after 45 s" in snapshot["diagnostic"]
    assert runner.workstation_healthy(timeout_seconds=3.0) is False
    with pytest.raises(module.HilRestartError, match="timed out.*stopped proof unavailable"):
        runner.workstation_stopped()
    monkeypatch.setenv("III_HIL_WORKSTATION_STATUS_TIMEOUT_SEC", "7")
    runner.workstation_snapshot()
    assert calls == [45.0, 3.0, 45.0, 7.0]


def test_status_reports_workstation_status_timeout(monkeypatch):
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda command, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, kwargs["timeout"])),
    )
    state = module.profile_status(Client(), module.ProcessRunner(), tolerate_unavailable=True)
    assert state["components"]["workstation"] == "degraded"
    assert state["diagnostics"]["workstation"] == ["workstation launcher status timed out after 45 s"]
