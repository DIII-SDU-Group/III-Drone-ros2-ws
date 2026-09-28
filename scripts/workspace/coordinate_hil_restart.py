#!/usr/bin/env python3
"""Coordinate the native split-host HIL lifecycle without rewinding live ROS time."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Iterator, NoReturn
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "tools" / "III-Drone-CLI"))
sys.path.insert(0, str(WORKSPACE / "tools" / "III-Drone-MCP"))

from iii.runtime_api_client import RuntimeApiClient, RuntimeApiError  # noqa: E402


class HilRestartError(RuntimeError):
    """Raised when coordinated HIL restart cannot remain fail-closed."""


# A Runtime API request can block for the client's full (cold-boot sized)
# timeout. Lifecycle waits cap each call to the time left in their own window.
MIN_CALL_TIMEOUT_SEC = 0.5
DEFAULT_STATUS_API_TIMEOUT_SEC = 10.0
DEFAULT_WORKSTATION_STATUS_TIMEOUT_SEC = 45.0
DEFAULT_CONFIRM_INTERVAL_SEC = 1.0

_TRANSIENT_ERRNOS = frozenset({
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.EHOSTUNREACH,
    errno.EHOSTDOWN,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    errno.ETIMEDOUT,
})


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if value != value or value < 0:
        raise ValueError(f"{name} must be a non-negative number of seconds, got {raw!r}")
    return value


@contextmanager
def _bounded_call(client: Any, seconds: float | None) -> Iterator[None]:
    """Temporarily cap one Runtime API request to ``seconds``."""
    previous = getattr(client, "timeout_seconds", None)
    if seconds is None or not isinstance(previous, (int, float)):
        yield
        return
    client.timeout_seconds = max(MIN_CALL_TIMEOUT_SEC, min(float(previous), seconds))
    try:
        yield
    finally:
        client.timeout_seconds = previous


def is_transient_unavailability(exc: BaseException) -> bool:
    """Whether a Runtime API failure means "not reachable yet", not "refused".

    HTTP responses (including 4xx/5xx) prove the API answered and are never
    retried here; only connection-level and name-resolution failures are.
    """
    cause = exc.__cause__ if isinstance(exc, RuntimeApiError) else exc
    if cause is None or isinstance(cause, HTTPError):
        return False
    reason = cause.reason if isinstance(cause, URLError) else cause
    if isinstance(reason, (socket.timeout, TimeoutError, socket.gaierror)):
        return True
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(reason, OSError) and reason.errno in _TRANSIENT_ERRNOS


def _emit_progress(stage: str, state: str, detail: str) -> None:
    """Emit concise opt-in progress without changing normal or JSON output."""
    if os.environ.get("III_HIL_PROGRESS") == "1":
        print(f"III_HIL_PROGRESS|{stage}|{state}|{detail}", flush=True)


def _progress_operation(stage: str, detail: str, operation: Callable[[], Any]) -> Any:
    _emit_progress(stage, "start", detail)
    try:
        result = operation()
    except Exception:
        _emit_progress(stage, "failed", detail)
        raise
    _emit_progress(stage, "done", detail)
    return result


def _failure_reason(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            result = json.loads(line)
        except (ValueError, TypeError):
            continue
        # The native CLI wraps a remote command result. Surface the innermost
        # transcript before a generic route/handler error or a long JSON line.
        for _ in range(8):
            if not isinstance(result, dict):
                break
            payload = result.get("payload")
            if not isinstance(payload, dict):
                break
            nested = payload.get("runtime_result")
            if isinstance(nested, dict):
                result = nested
                continue
            display = payload.get("display")
            if isinstance(display, str) and display.strip():
                return _failure_reason_text(display)
            break
    return _failure_reason_text(output)


def _failure_reason_text(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if "Next:" in lines:
        lines = lines[:lines.index("Next:")]
    while lines and lines[-1].startswith(("Findings:", "usage:", "options:")):
        lines.pop()
    return next(
        (line for line in reversed(lines) if re.search(
            r"failed|error|unable|cannot|missing|not running|refus|timed out|denied|invalid|unavailable",
            line, re.IGNORECASE,
        )),
        lines[-1] if lines else "child produced no diagnostic output",
    )


def _run_lifecycle_child(
    command: list[str], *, label: str, environment: dict[str, str]
) -> None:
    """Keep full child output in the coordinator log and surface its failure."""
    result = subprocess.run(
        command, cwd=WORKSPACE, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        check=False,
    )
    output = result.stdout or ""
    if output:
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
    if result.returncode == 0:
        return
    # CLI failures may end with a suggested next command. Prefer the actual
    # diagnostic immediately above it; never replace it with a Python repr.
    reason = _failure_reason(output)
    raise HilRestartError(f"{label} failed (exit {result.returncode}): {reason}")


def wait_runtime_identity(
    client: RuntimeApiClient, *, timeout_seconds: float = 30.0,
    poll_seconds: float = 2.0, monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Allow a just-restarted or briefly unreachable API to answer.

    Connection refusal, host/network unreachability, timeouts and name
    resolution failures are retried within one bounded window; an HTTP error
    or any other failure is reported immediately.
    """
    window = min(30.0, timeout_seconds)
    deadline = None
    budget = window
    while True:
        try:
            with _bounded_call(client, budget):
                return client.identity()
        except RuntimeApiError as exc:
            if not is_transient_unavailability(exc):
                raise
            if deadline is None:
                deadline = monotonic() + window
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise HilRestartError(f"Runtime API remained unavailable: {exc}") from exc
            pause = min(poll_seconds, remaining)
            sleep(pause)
            budget = max(MIN_CALL_TIMEOUT_SEC, remaining - pause)


def _accepted_result(response: dict[str, Any], command_id: str) -> dict[str, Any]:
    if not response.get("accepted") or not isinstance(response.get("result"), dict):
        rejection = response.get("rejection") or {}
        reason = rejection.get("message") or response.get("message") or "rejected"
        raise HilRestartError(f"{command_id} failed: {reason}")
    return response["result"]


def wait_runtime_status(
    client: RuntimeApiClient, *, timeout_seconds: float = 30.0,
    poll_seconds: float = 2.0, monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait only for a restarted daemon's absent/refused control socket."""
    window = min(30.0, timeout_seconds)
    deadline = None
    budget = window
    while True:
        try:
            with _bounded_call(client, budget):
                response = client.command("runtime.status", {})
        except RuntimeApiError as exc:
            if not is_transient_unavailability(exc):
                raise
            response = None
            reason = str(exc)
        if response is None:
            socket_unavailable = True
        else:
            reason = str(response.get("message") or "")
            socket_unavailable = (
                response.get("accepted") is False
                and response.get("rejection") is None
                and (response.get("result") or {}).get("permission") == "read_only"
                and reason.startswith(("[Errno 2] ", "[Errno 111] "))
            )
        if not socket_unavailable:
            return _accepted_result(response, "runtime.status")
        if deadline is None:
            deadline = monotonic() + window
        remaining = deadline - monotonic()
        if remaining <= 0:
            if response is None:
                raise HilRestartError(f"Runtime API remained unavailable: {reason}")
            raise HilRestartError(f"Runtime daemon socket remained unavailable: {reason}")
        pause = min(poll_seconds, remaining)
        sleep(pause)
        budget = max(MIN_CALL_TIMEOUT_SEC, remaining - pause)


def _field_is_fresh(state: dict[str, Any], name: str, expected: Any) -> bool:
    field = (state.get("telemetry_fields") or {}).get(name) or {}
    return (
        state.get(name) == expected
        and field.get("value") == expected
        and field.get("freshness") == "fresh"
        and field.get("source_availability") == "available"
        and field.get("disagreement") is not True
    )


class ProcessRunner:
    def __init__(self, *, rendered: bool = True, host: str | None = None) -> None:
        self.rendered = rendered
        self.host = host
        self.cli = Path(
            os.environ.get(
                "III_HIL_CLI", str(WORKSPACE / "tools" / "III-Drone-CLI" / "bin" / "iii")
            )
        )
        self.launcher = Path(
            os.environ.get(
                "III_HIL_WORKSTATION_LAUNCHER",
                str(WORKSPACE / "tools" / "simulation" / "launch_hil_workstation.sh"),
            )
        )

    def launcher_command(self, action: str) -> list[str]:
        command = [str(self.launcher), action]
        if self.host is not None:
            command.extend(("--host", self.host))
        return command

    def workstation(self, action: str) -> None:
        environment = dict(os.environ)
        environment["III_HIL_RENDERED"] = "1" if self.rendered else "0"
        _run_lifecycle_child(
            self.launcher_command(action), label=f"workstation {action}",
            environment=environment,
        )

    def battery_check(self) -> None:
        _run_lifecycle_child(
            self.launcher_command("battery-check"), label="PX4 battery link",
            environment=dict(os.environ),
        )

    def status_timeout(self, timeout_seconds: float | None = None) -> float:
        configured = _env_seconds(
            "III_HIL_WORKSTATION_STATUS_TIMEOUT_SEC",
            DEFAULT_WORKSTATION_STATUS_TIMEOUT_SEC,
        ) or DEFAULT_WORKSTATION_STATUS_TIMEOUT_SEC
        if timeout_seconds is None:
            return configured
        return max(MIN_CALL_TIMEOUT_SEC, min(configured, timeout_seconds))

    def workstation_healthy(self, timeout_seconds: float | None = None) -> bool:
        snapshot = self.workstation_snapshot(timeout_seconds=timeout_seconds)
        return bool(
            snapshot["returncode"] == 0
            and snapshot["fields"].get("hil_readiness") == "ready"
        )

    def workstation_snapshot(self, timeout_seconds: float | None = None) -> dict[str, Any]:
        timeout = self.status_timeout(timeout_seconds)
        try:
            result = subprocess.run(
                self.launcher_command("status"),
                cwd=WORKSPACE,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "returncode": 124,
                "fields": {},
                "diagnostic": f"workstation launcher status timed out after {timeout:g} s",
            }
        fields = dict(
            line.split(": ", 1)
            for line in result.stdout.splitlines()
            if ": " in line
        )
        diagnostic_output = result.stderr or (result.stdout if not fields else "")
        diagnostic = _failure_reason(diagnostic_output) if result.returncode and diagnostic_output else None
        return {"returncode": result.returncode, "fields": fields, "diagnostic": diagnostic}

    def workstation_stopped(self) -> bool:
        # Failed readiness alone is not proof that PX4 is stopped: one missing
        # sensor bridge can degrade an otherwise running simulator.
        timeout = self.status_timeout()
        try:
            result = subprocess.run(
                self.launcher_command("status"), cwd=WORKSPACE,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise HilRestartError(
                f"workstation launcher status timed out after {timeout:g} s; "
                "stopped proof unavailable"
            ) from exc
        fields = dict(
            line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line
        )
        if not all(fields.get(key) == value for key, value in {
            "canonical_px4_process_alive": "no",
            "canonical_tmux_session": "stopped",
            "hil_owner_state": "stopped",
            "canonical_endpoint_snapshot": "ready",
            "hil_pi_reachability": "ready",
            "canonical_xrce_endpoint_ownership": "no",
        }.items()):
            return False
        return fields.get("conflicting_px4_owners", "").startswith("count=0 ")

    def system_mutation(self, action: str, *arguments: str) -> None:
        operation_id = f"hil-restart-{action}-{uuid4()}"
        common = ["system", action, *arguments]
        environment = dict(os.environ)
        # The coordinator is executed on the workstation but the runtime it
        # restarts belongs to the Pi. Keep both plan and apply on that one
        # remote authority; a local CLI default silently targets a different
        # (usually absent) simulation daemon.
        environment["CLI_CONFIGURATION"] = "remote"
        _run_lifecycle_child(
            [str(self.cli), *common, "--dry-run", "--operation-id", operation_id, "--output=json"],
            label=f"Pi system {action} plan", environment=environment,
        )
        _run_lifecycle_child(
            [
                str(self.cli),
                *common,
                "--operation-id",
                operation_id,
                "--confirm",
                "--non-interactive",
                "--output=json",
            ],
            label=f"Pi system {action}", environment=environment,
        )


def coordinate_restart(
    client: RuntimeApiClient,
    runner: ProcessRunner,
    *,
    timeout_seconds: float = 240.0,
    poll_seconds: float = 2.0,
    confirm_interval_seconds: float = DEFAULT_CONFIRM_INTERVAL_SEC,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    identity = _progress_operation(
        "pi_api_identity", "checking Pi runtime identity", lambda: wait_runtime_identity(
            client, timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds, monotonic=monotonic, sleep=sleep,
        ),
    )
    if identity.get("profile") != "hil":
        raise HilRestartError(
            f"remote runtime profile is {identity.get('profile')!r}, expected 'hil'"
        )

    runtime = _progress_operation(
        "pi_runtime_status", "checking Pi runtime status", lambda: wait_runtime_status(
            client, timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds, monotonic=monotonic, sleep=sleep,
        ),
    )
    daemon = runtime.get("daemon") or {}
    if daemon.get("booted"):
        _progress_operation(
            "pi_shutdown", "shutting down Pi runtime", lambda: runner.system_mutation("shutdown")
        )

    # Once all Pi-managed ROS processes are gone, simulator time can safely
    # return to zero. Boot the Pi only after the replacement clock is live.
    _progress_operation(
        "workstation_stop", "stopping workstation simulation", lambda: runner.workstation("stop")
    )
    _progress_operation(
        "workstation_start", "starting workstation simulation", lambda: runner.workstation("start")
    )
    _progress_operation(
        "pi_boot", "booting Pi HIL runtime",
        lambda: runner.system_mutation("boot", "--profile", "hil"),
    )
    _progress_operation(
        "pi_start", "starting Pi HIL runtime", lambda: runner.system_mutation("start")
    )

    deadline = monotonic() + timeout_seconds
    stable = 0
    waiting_reason = (
        "waiting for active HIL runtime, arming checks, "
        "and workstation adapter/cross-host readiness"
    )
    # A deadline reached part-way through a sample keeps the last complete
    # sample's reason, or this generic one if no sample completed.
    last_reason = waiting_reason
    last_progress: tuple[str, str, str] | None = None
    last_progress_time = 0.0

    def remaining_budget() -> float | None:
        # Every blocking probe receives only the time left before the overall
        # readiness deadline, so one slow call cannot overrun it.
        remaining = deadline - monotonic()
        return remaining if remaining > 0 else None

    _emit_progress("readiness", "waiting", "waiting for runtime, arming checks, and workstation readiness")
    while True:
        now = monotonic()
        if now >= deadline:
            break
        try:
            with _bounded_call(client, deadline - now):
                runtime = _accepted_result(
                    client.command("runtime.status", {}), "runtime.status"
                )
            daemon = runtime.get("daemon") or {}
            budget = remaining_budget()
            if budget is None:
                break
            with _bounded_call(client, budget):
                vehicle = client.vehicle_status()
            prerequisites_ready = (
                daemon.get("booted") is True
                and daemon.get("active") is True
                and daemon.get("profile") == "hil"
                and _field_is_fresh(vehicle, "arming_checks_passed", True)
            )
            # Preserve the original short-circuit: the workstation status
            # probe is potentially expensive and is only needed after the Pi
            # and vehicle prerequisites have converged.
            workstation_ready = None
            if prerequisites_ready:
                budget = remaining_budget()
                if budget is None:
                    break
                workstation_ready = runner.workstation_healthy(timeout_seconds=budget)
            ready = prerequisites_ready and workstation_ready is True
            runtime_progress = (
                "ready" if daemon.get("booted") is True and daemon.get("active") is True
                and daemon.get("profile") == "hil" else
                "running" if daemon.get("booted") else "stopped"
            )
            checks_progress = (
                "ready" if _field_is_fresh(vehicle, "arming_checks_passed", True) else
                "failed" if _field_is_fresh(vehicle, "arming_checks_passed", False) else "unknown"
            )
            workstation_progress = (
                "ready" if workstation_ready is True else
                "waiting" if workstation_ready is False else "pending"
            )
            progress_state = (
                runtime_progress, checks_progress, workstation_progress
            )
            if progress_state != last_progress or now - last_progress_time >= 20.0:
                _emit_progress(
                    "readiness", "update",
                    "runtime=" + runtime_progress + " checks=" + checks_progress
                    + " workstation=" + workstation_progress,
                )
                last_progress = progress_state
                last_progress_time = now
            if ready:
                stable += 1
                if stable >= 3:
                    _progress_operation(
                        "battery_link", "checking PX4 battery at workstation charger",
                        runner.battery_check,
                    )
                    _emit_progress("readiness", "done", "runtime and workstation readiness confirmed")
                    return
                # Space the confirmation samples so three passes demonstrate
                # stability rather than one instant observed three times.
                if confirm_interval_seconds > 0:
                    sleep(min(confirm_interval_seconds, max(deadline - now, 0.0)))
                continue
            else:
                stable = 0
                last_reason = waiting_reason
        except Exception as exc:  # service/node convergence is transient here
            stable = 0
            last_reason = str(exc)
            progress_state = ("unknown", "unknown", "unknown")
            if progress_state != last_progress or now - last_progress_time >= 20.0:
                _emit_progress(
                    "readiness", "update",
                    "runtime=unknown checks=unknown workstation=unknown",
                )
                last_progress = progress_state
                last_progress_time = now
        sleep(poll_seconds)
    _emit_progress("readiness", "failed", "readiness timed out")
    raise HilRestartError(f"coordinated HIL restart timed out: {last_reason}")


def profile_status(
    client: RuntimeApiClient | None,
    runner: ProcessRunner,
    *,
    identity: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    tolerate_unavailable: bool = False,
    client_error: str | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, list[str]] = {}
    pi_diagnostics = diagnostics.setdefault("pi_runtime", [])
    identity_valid = False
    identity_unavailable = False
    runtime_valid = runtime is not None

    if client_error:
        pi_diagnostics.append(f"client unavailable: {client_error}")
    if identity is None and client is not None:
        try:
            identity = client.identity()
        except (RuntimeApiError, OSError, ValueError) as exc:
            if not tolerate_unavailable:
                raise
            identity_unavailable = True
            pi_diagnostics.append(f"identity unavailable: {exc}")
            identity = {}
    identity = identity or {}
    if identity.get("profile") == "hil":
        identity_valid = True
    else:
        message = f"remote profile is {identity.get('profile')!r}, expected 'hil'"
        if not tolerate_unavailable:
            raise HilRestartError(message)
        if identity:
            pi_diagnostics.append(message)

    # An unavailable identity already proves that this API endpoint cannot
    # provide a trustworthy Pi status snapshot. Avoid a second potentially
    # long network timeout; workstation and ground-control checks remain
    # useful and are still performed below.
    if runtime is None and client is not None and not identity_unavailable:
        try:
            runtime = _accepted_result(
                client.command("runtime.status", {}), "runtime.status"
            )
            runtime_valid = True
        except (HilRestartError, RuntimeApiError, OSError, ValueError) as exc:
            if not tolerate_unavailable:
                raise
            pi_diagnostics.append(f"status unavailable: {exc}")
            runtime = {}
    runtime = runtime or {}
    daemon = runtime.get("daemon") or {}

    try:
        snapshot = runner.workstation_snapshot()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if not tolerate_unavailable:
            raise
        diagnostics.setdefault("workstation", []).append(
            f"status unavailable: {exc}"
        )
        snapshot = {"returncode": 1, "fields": {}}
    fields = snapshot["fields"]
    if snapshot.get("diagnostic"):
        diagnostics.setdefault("workstation", []).append(snapshot["diagnostic"])
    workstation_ready = bool(
        snapshot["returncode"] == 0 and fields.get("hil_readiness") == "ready"
    )
    if identity_valid and runtime_valid:
        runtime_state = _runtime_component_state(daemon)
    elif identity and not identity_valid:
        runtime_state = "degraded"
    else:
        runtime_state = "unknown"
    workstation_state = _workstation_component_state(fields, workstation_ready)
    rendered_mode = fields.get("hil_render_mode", "unknown")
    viewer_state = fields.get("hil_gazebo_viewer", "unknown")
    try:
        ground_control = ground_control_health()
    except (OSError, ValueError) as exc:
        if not tolerate_unavailable:
            raise
        diagnostics.setdefault("ground_control", []).append(
            f"status unavailable: {exc}"
        )
        ground_control = {
            "state": "degraded",
            "url": os.environ.get("III_HIL_GC_URL", "http://127.0.0.1:5174"),
            "frontend_ready": False,
            "proxy_ready": False,
        }
    state = _aggregate_profile_state(
        runtime_state=runtime_state,
        workstation_state=workstation_state,
        rendered_mode=rendered_mode,
        viewer_state=viewer_state,
        ground_control_state=ground_control["state"],
    )
    return {
        "profile": "hil",
        "pi_target": {
            "host": os.environ.get("III_HIL_PI_ENDPOINT") or os.environ.get("III_HIL_PI_ADDRESS") or "iii.local",
            "peer_ipv4": fields.get("hil_pi_peer") or None,
            "workstation_source_ipv4": fields.get("hil_workstation_source") or None,
        },
        "native_install": {
            "root": os.environ.get(
                "III_GC_INSTALL_ROOT",
                str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "iii" / "gc"),
            ),
            "install_profile": os.environ.get("III_GC_INSTALL_PROFILE", "unknown"),
            "runtime_profile": os.environ.get("III_GC_EXPECTED_PROFILE", "hil"),
            "host": socket.gethostname(),
        },
        "runtime": daemon,
        "workstation_ready": workstation_ready,
        "core_ready": runtime_state == "ready" and workstation_state == "ready",
        "ready": state == "ready",
        "state": state,
        "components": {
            "pi_runtime": runtime_state,
            "workstation": workstation_state,
            "gazebo_viewer": viewer_state,
            "ground_control": ground_control["state"],
        },
        "rendered": {"rendered": True, "headless": False}.get(rendered_mode),
        "render_mode": rendered_mode,
        "viewer_state": viewer_state,
        "ground_control": ground_control,
        "diagnostics": {key: value for key, value in diagnostics.items() if value},
        "gc_url": ground_control["url"],
        "log_path": os.environ.get(
            "III_HIL_LOG_PATH", str(WORKSPACE / "runtime_logs" / "hil" / "latest.log")
        ),
    }


def coordinate_start(client: RuntimeApiClient, runner: ProcessRunner, **kwargs: Any) -> None:
    # An idempotent start must not reset simulator time or interrupt a mission.
    wait = {key: value for key, value in kwargs.items() if key != "confirm_interval_seconds"}
    identity = _progress_operation(
        "pi_api_identity", "checking Pi runtime identity",
        lambda: wait_runtime_identity(client, **wait),
    )
    if identity.get("profile") != "hil":
        raise HilRestartError(f"remote runtime profile is {identity.get('profile')!r}, expected 'hil'")
    runtime = _progress_operation(
        "pi_runtime_status", "checking Pi runtime status",
        lambda: wait_runtime_status(client, **wait),
    )
    daemon = runtime.get("daemon") or {}
    # Start's idempotence gate is deliberately independent of ground-control
    # health. A healthy core must not be restarted because the UI is down.
    core_ready = (
        daemon.get("booted") is True
        and daemon.get("active") is True
        and daemon.get("profile") == "hil"
        and runner.workstation_healthy()
    )
    if core_ready:
        _emit_progress("idempotent_start", "done", "core already running; repairing viewer if needed")
        # This is intentionally a non-destructive repair path.  The workstation
        # launcher only opens a missing owned GUI pane when requested; it does
        # not recreate PX4, Gazebo, adapters, or an active mission.
        _progress_operation(
            "viewer_repair", "repairing missing workstation viewer if needed",
            lambda: runner.workstation("start"),
        )
        _progress_operation(
            "battery_link", "checking PX4 battery at workstation charger",
            runner.battery_check,
        )
        return
    coordinate_restart(client, runner, **kwargs)


def _emit_stop_component(component: str, outcome: str, detail: str = "") -> None:
    """Record one per-owner stop outcome for the operator wrapper and log."""
    detail = " ".join(str(detail).split())
    print(f"III_HIL_STOP|{component}|{outcome}|{detail}", flush=True)


_PI_UNAVAILABLE_ERRORS = (RuntimeApiError, HilRestartError, OSError, ValueError)


def _stop_workstation_without_pi(runner: ProcessRunner, pi_reason: str) -> NoReturn:
    """Best-effort workstation stop when the Pi runtime cannot be queried.

    The launcher still validates and stops only its recorded owner under its
    lifecycle lock (and verifies the result). Pi-side endpoint proof and the
    Pi shutdown itself are impossible, so the overall stop still fails.
    """
    _emit_stop_component("pi_runtime", "unreachable", pi_reason)
    try:
        runner.workstation("stop")
    except (HilRestartError, OSError, subprocess.SubprocessError) as exc:
        _emit_stop_component("workstation", "failed", str(exc))
        raise HilRestartError(
            f"Pi runtime unreachable ({pi_reason}); workstation stop also failed: {exc}"
        ) from exc
    _emit_stop_component(
        "workstation", "stopped",
        "launcher-verified; Pi-side endpoint proof unavailable",
    )
    raise HilRestartError(
        f"Pi runtime unreachable ({pi_reason}); owned workstation HIL components "
        "were stopped, but the Pi runtime was not shut down"
    )


def coordinate_stop(
    client: RuntimeApiClient | None,
    runner: ProcessRunner,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    client_error: str | None = None,
) -> None:
    wait = {
        "timeout_seconds": timeout_seconds, "poll_seconds": poll_seconds,
        "monotonic": monotonic, "sleep": sleep,
    }
    if client is None:
        _stop_workstation_without_pi(
            runner, f"client unavailable: {client_error or 'not configured'}"
        )
    try:
        identity = wait_runtime_identity(client, **wait)
    except _PI_UNAVAILABLE_ERRORS as exc:
        _stop_workstation_without_pi(runner, f"identity unavailable: {exc}")
    # A reachable runtime that is positively not virtual HIL is refused before
    # any mutation, exactly as before: this command must never act beside a
    # real/opti-track aircraft runtime.
    if identity.get("profile") != "hil":
        raise HilRestartError(
            f"remote runtime profile is {identity.get('profile')!r}, expected 'hil'"
        )
    try:
        runtime = wait_runtime_status(client, **wait)
    except _PI_UNAVAILABLE_ERRORS as exc:
        _stop_workstation_without_pi(runner, f"status unavailable: {exc}")
    daemon = runtime.get("daemon") or {}
    # Identity establishes this as virtual HIL; refuse if the daemon disagrees.
    # An airborne virtual vehicle may be stopped as an operator emergency action.
    # The workstation launcher validates ownership before acting, then we require
    # explicit canonical stopped/endpoint proof after it returns.
    if daemon.get("booted") and daemon.get("profile") != "hil":
        raise HilRestartError(
            f"Pi daemon profile is {daemon.get('profile')!r}, expected 'hil'"
        )
    # The workstation stop holds its lifecycle lock through ownership checking
    # and process/session termination. Complete it before shutting down the Pi,
    # so a changed or ambiguous owner cannot leave PX4 alive without the Pi graph.
    try:
        runner.workstation("stop")
        stopped = runner.workstation_stopped()
    except (HilRestartError, OSError, subprocess.SubprocessError) as exc:
        _emit_stop_component("workstation", "failed", str(exc))
        _emit_stop_component("pi_runtime", "not_attempted", "workstation stop failed")
        raise
    if not stopped:
        _emit_stop_component("workstation", "failed", "canonical ownership/stopped proof unavailable")
        _emit_stop_component("pi_runtime", "not_attempted", "workstation stopped proof unavailable")
        raise HilRestartError(
            "workstation stop returned, but canonical ownership/stopped proof "
            "remains unavailable; Pi runtime shutdown was not attempted"
        )
    _emit_stop_component("workstation", "stopped")
    if daemon.get("booted"):
        try:
            runner.system_mutation("shutdown")
        except (OSError, subprocess.CalledProcessError, HilRestartError) as exc:
            _emit_stop_component("pi_runtime", "failed", str(exc))
            raise HilRestartError(
                "workstation HIL components are stopped, but Pi runtime shutdown "
                f"failed; workstation PX4/Gazebo are already stopped ({exc})"
            ) from exc
        _emit_stop_component("pi_runtime", "stopped")
    else:
        _emit_stop_component("pi_runtime", "already_stopped")
    if not runner.workstation_stopped():
        raise HilRestartError(
            "HIL owned components were stopped, but canonical endpoint/stopped "
            "proof remains unavailable"
        )


def _runtime_component_state(daemon: dict[str, Any]) -> str:
    if daemon.get("booted") and daemon.get("active") and daemon.get("profile") == "hil":
        return "ready"
    if daemon.get("booted"):
        return "running"
    return "stopped"


def _workstation_component_state(fields: dict[str, str], ready: bool) -> str:
    if ready:
        return "ready"
    if (
        fields.get("hil_owner_state") == "stopped"
        and fields.get("hil_simulation") == "stopped"
    ):
        return "stopped"
    if fields.get("hil_simulation") == "running":
        return "running"
    return "degraded"


def _aggregate_profile_state(
    *, runtime_state: str, workstation_state: str, rendered_mode: str,
    viewer_state: str, ground_control_state: str,
) -> str:
    if runtime_state == "stopped" and workstation_state == "stopped":
        return "stopped"
    if runtime_state == "ready" and workstation_state == "ready":
        if ground_control_state != "ready":
            return "degraded"
        if rendered_mode == "rendered":
            return "ready" if viewer_state == "running" else "degraded"
        if rendered_mode == "headless":
            return "ready"
        return "running"
    if runtime_state in {"degraded", "unknown"} or workstation_state == "degraded":
        return "degraded"
    return "running"


def _http_available(url: str) -> bool:
    try:
        with urlopen(url, timeout=float(os.environ.get("III_HIL_GC_HEALTH_TIMEOUT_SEC", "1"))):
            return True
    except (OSError, ValueError):
        return False


def ground_control_health() -> dict[str, Any]:
    url = os.environ.get("III_HIL_GC_URL", "http://127.0.0.1:5174")
    proxy_url = os.environ.get("III_HIL_GC_PROXY_HEALTH_URL", "http://127.0.0.1:8781/health")
    frontend_ready = _http_available(url)
    proxy_ready = _http_available(proxy_url)
    return {
        "state": "ready" if frontend_ready and proxy_ready else "degraded",
        "url": url,
        "frontend_ready": frontend_ready,
        "proxy_ready": proxy_ready,
    }


def print_operator_status(state: dict[str, Any]) -> None:
    components = state["components"]
    print(f"HIL · {state['state']}")
    target = state.get("pi_target", {})
    print(f"Pi target:       {target.get('host', 'iii.local')}" + (
        f" ({target['peer_ipv4']})" if target.get("peer_ipv4") else ""
    ))
    print(f"Pi runtime:      {components['pi_runtime']}")
    print(f"Workstation:     {components['workstation']}")
    print(f"Gazebo viewer:   {state['viewer_state']} ({state['render_mode']})")
    print(f"Ground control:  {components['ground_control']} · {state['gc_url']}")
    for detail in state.get("diagnostics", {}).get("pi_runtime", []):
        print(f"Pi detail:       {detail}")
    for detail in state.get("diagnostics", {}).get("workstation", []):
        print(f"Workstation detail: {detail}")
    for detail in state.get("diagnostics", {}).get("ground_control", []):
        print(f"Ground control detail: {detail}")
    print(f"Log:             {state['log_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "restart", "status", "stop"), nargs="?", default="restart")
    parser.add_argument("--headless", action="store_true", help="do not create a Gazebo viewer")
    parser.add_argument("--json", action="store_true", help="emit machine-readable status")
    parser.add_argument("--host", help="Pi hostname or IPv4 address (default: iii.local)")
    args = parser.parse_args()
    if args.json and args.action != "status":
        parser.error("--json is only valid with status")
    if args.host:
        if not args.host[0].isalnum() or not all(
            character.isascii() and (character.isalnum() or character in ".-")
            for character in args.host
        ):
            parser.error("--host must be a hostname or IPv4 address")
        os.environ["III_HIL_PI_ENDPOINT"] = args.host
        os.environ.pop("III_HIL_PI_ADDRESS", None)
        os.environ.pop("CYCLONEDDS_URI", None)
        os.environ["III_SSH_HOST"] = args.host
        os.environ["III_RUNTIME_HOST"] = args.host
        os.environ["III_RUNTIME_API_HOST"] = args.host
        os.environ["III_RUNTIME_API_URL"] = f"http://{args.host}:8765"
    try:
        runner = ProcessRunner(rendered=not args.headless, host=args.host)
        if args.action == "status":
            client_error = None
            try:
                client = RuntimeApiClient.from_env()
            except (RuntimeApiError, OSError, ValueError) as exc:
                client = None
                client_error = str(exc)
            if client is not None and isinstance(getattr(client, "timeout_seconds", None), (int, float)):
                # A read-only status snapshot must stay responsive; the
                # client's cold-boot sized default is only for mutations.
                client.timeout_seconds = min(
                    client.timeout_seconds,
                    _env_seconds("III_HIL_STATUS_API_TIMEOUT_SEC", DEFAULT_STATUS_API_TIMEOUT_SEC)
                    or DEFAULT_STATUS_API_TIMEOUT_SEC,
                )
            state = profile_status(
                client,
                runner,
                tolerate_unavailable=True,
                client_error=client_error,
            )
            if args.json:
                print(json.dumps(state, indent=2, sort_keys=True))
            else:
                print_operator_status(state)
            return 0 if state["state"] in {"ready", "running", "stopped"} else 1
        timeout_seconds = _env_seconds("III_HIL_RESTART_TIMEOUT_SEC", 240.0)
        poll_seconds = _env_seconds("III_HIL_RESTART_POLL_SEC", 2.0)
        if args.action == "stop":
            # Stop is best effort across owners: an unusable Pi client must
            # not prevent stopping the workstation-owned simulation.
            client_error = None
            try:
                client = RuntimeApiClient.from_env()
            except (RuntimeApiError, OSError, ValueError) as exc:
                client = None
                client_error = str(exc)
            coordinate_stop(
                client, runner, timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds, client_error=client_error,
            )
        else:
            client = RuntimeApiClient.from_env()
            (coordinate_start if args.action == "start" else coordinate_restart)(
                client, runner,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                confirm_interval_seconds=_env_seconds(
                    "III_HIL_RESTART_CONFIRM_INTERVAL_SEC", DEFAULT_CONFIRM_INTERVAL_SEC
                ),
            )
    except (HilRestartError, RuntimeApiError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Coordinated HIL {args.action} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Coordinated HIL {args.action} completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
