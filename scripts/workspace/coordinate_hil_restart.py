#!/usr/bin/env python3
"""Coordinate a split-host HIL restart without rewinding a live Pi ROS clock."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4


WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "tools" / "III-Drone-CLI"))

from iii.runtime_api_client import RuntimeApiClient  # noqa: E402


class HilRestartError(RuntimeError):
    """Raised when coordinated HIL restart cannot remain fail-closed."""


def _accepted_result(response: dict[str, Any], command_id: str) -> dict[str, Any]:
    if not response.get("accepted") or not isinstance(response.get("result"), dict):
        rejection = response.get("rejection") or {}
        reason = rejection.get("message") or response.get("message") or "rejected"
        raise HilRestartError(f"{command_id} failed: {reason}")
    return response["result"]


def _field_is_fresh(state: dict[str, Any], name: str, expected: Any) -> bool:
    field = (state.get("telemetry_fields") or {}).get(name) or {}
    return (
        state.get(name) == expected
        and field.get("value") == expected
        and field.get("freshness") == "fresh"
        and field.get("source_availability") == "available"
        and field.get("disagreement") is not True
    )


def vehicle_is_safely_landed(state: dict[str, Any]) -> bool:
    return (
        state.get("source_availability") == "available"
        and state.get("freshness") == "fresh"
        and _field_is_fresh(state, "armed", False)
        and _field_is_fresh(state, "in_air", False)
    )


class ProcessRunner:
    def __init__(self) -> None:
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

    def workstation(self, action: str) -> None:
        subprocess.run([str(self.launcher), action], check=True, cwd=WORKSPACE)

    def workstation_healthy(self) -> bool:
        return subprocess.run(
            [str(self.launcher), "status"],
            cwd=WORKSPACE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    def system_mutation(self, action: str, *arguments: str) -> None:
        operation_id = f"hil-restart-{action}-{uuid4()}"
        common = ["system", action, *arguments]
        subprocess.run(
            [str(self.cli), *common, "--dry-run", "--operation-id", operation_id, "--output=json"],
            check=True,
            cwd=WORKSPACE,
        )
        subprocess.run(
            [
                str(self.cli),
                *common,
                "--operation-id",
                operation_id,
                "--confirm",
                "--non-interactive",
                "--output=json",
            ],
            check=True,
            cwd=WORKSPACE,
        )


def coordinate_restart(
    client: RuntimeApiClient,
    runner: ProcessRunner,
    *,
    timeout_seconds: float = 240.0,
    poll_seconds: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    identity = client.identity()
    if identity.get("profile") != "hil":
        raise HilRestartError(
            f"remote runtime profile is {identity.get('profile')!r}, expected 'hil'"
        )

    runtime = _accepted_result(client.command("runtime.status", {}), "runtime.status")
    daemon = runtime.get("daemon") or {}
    if daemon.get("booted"):
        vehicle = client.vehicle_status()
        # A dead HIL simulator cannot produce a fresh landed sample. The remote
        # identity check above proves this is the simulation-only profile, so
        # shutting down the Pi runtime is the safe recovery action before the
        # workstation clock is recreated. Keep failing closed whenever the
        # simulator is still healthy: stale telemetry must never reset a live
        # simulation clock.
        if not vehicle_is_safely_landed(vehicle) and runner.workstation_healthy():
            raise HilRestartError(
                "refusing HIL clock reset: vehicle is not freshly confirmed disarmed and landed"
            )
        runner.system_mutation("shutdown")

    # Once all Pi-managed ROS processes are gone, simulator time can safely
    # return to zero. Boot the Pi only after the replacement clock is live.
    runner.workstation("stop")
    runner.workstation("start")
    runner.system_mutation("boot", "--profile", "hil")
    runner.system_mutation("start")

    deadline = monotonic() + timeout_seconds
    stable = 0
    last_reason = "runtime did not report readiness"
    while monotonic() < deadline:
        try:
            runtime = _accepted_result(
                client.command("runtime.status", {}), "runtime.status"
            )
            daemon = runtime.get("daemon") or {}
            vehicle = client.vehicle_status()
            ready = (
                daemon.get("booted") is True
                and daemon.get("profile") == "hil"
                and vehicle_is_safely_landed(vehicle)
                and _field_is_fresh(vehicle, "arming_checks_passed", True)
            )
            if ready:
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
                last_reason = (
                    "waiting for booted HIL runtime, landed/disarmed state, and arming checks"
                )
        except Exception as exc:  # service/node convergence is transient here
            stable = 0
            last_reason = str(exc)
        sleep(poll_seconds)
    raise HilRestartError(f"coordinated HIL restart timed out: {last_reason}")


def main() -> int:
    try:
        coordinate_restart(
            RuntimeApiClient.from_env(),
            ProcessRunner(),
            timeout_seconds=float(os.environ.get("III_HIL_RESTART_TIMEOUT_SEC", "240")),
            poll_seconds=float(os.environ.get("III_HIL_RESTART_POLL_SEC", "2")),
        )
    except (HilRestartError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Coordinated HIL restart failed: {exc}", file=sys.stderr)
        return 1
    print("Coordinated HIL restart ready: Pi runtime and workstation SITL are healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
