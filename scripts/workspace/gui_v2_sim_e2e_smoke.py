#!/usr/bin/env python3
"""GUI v2 sim end-to-end smoke runner.

The default mode is read-only: it proves the GC stack can discover/select a sim
runtime API, authenticate through the proxy, and read all operator domains.
Mutating flight and workflow commands require explicit flags.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit


DEFAULT_READ_ENDPOINTS = [
    ("session", "/proxy/session"),
    ("runtime-status", "/proxy/runtime/status"),
    ("system-health", "/proxy/system/health"),
    ("subsystems-health", "/proxy/subsystems/health"),
    ("vehicle-status", "/proxy/vehicle/status"),
    ("control-status", "/proxy/control/status"),
    ("mission-status", "/proxy/mission/status"),
    ("operations-status", "/proxy/operations/status"),
    ("payload-status", "/proxy/payload/status"),
    ("perception-status", "/proxy/perception/status"),
    ("powerline-status", "/proxy/powerline/status"),
    ("configuration-status", "/proxy/configuration/status"),
    ("map-state", "/proxy/map/state"),
    ("rosbag-status", "/proxy/rosbag/status"),
    ("logs-sources", "/proxy/logs/sources"),
    ("commands-handlers", "/proxy/commands/handlers"),
    ("events-recent", "/proxy/events/recent"),
]

MUTATING_WORKFLOW_COMMANDS = [
    ("runtime-boot", "runtime.boot", {"profile": "sim"}),
    ("runtime-start", "runtime.start", {}),
    ("payload-gripper-open", "payload.gripper.open", {}),
    ("payload-gripper-close", "payload.gripper.close", {}),
    ("perception-pl-mapper-start", "perception.pl_mapper.start", {}),
    ("perception-pl-mapper-pause", "perception.pl_mapper.pause", {}),
    ("perception-pl-mapper-resume-after-pause", "perception.pl_mapper.start", {}),
    ("perception-pl-mapper-freeze", "perception.pl_mapper.freeze", {}),
    ("perception-pl-mapper-resume-after-freeze", "perception.pl_mapper.start", {}),
    (
        "rosbag-smoke-start",
        "rosbag.start",
        {
            "all_topics": False,
            "topics": [
                "/clock",
                "/fmu/out/vehicle_status_v1",
                "/perception/pl_mapper/powerline",
                "/perception/pl_mapper/state",
                "/supervision/system_health",
            ],
        },
    ),
    ("rosbag-smoke-stop", "rosbag.stop", {}),
    ("powerline-overview-update", "powerline.overview.update", {"timeout_s": 5}),
    ("perception-pl-mapper-stop", "perception.pl_mapper.stop", {}),
    (
        "configuration-list-snapshots",
        "configuration.snapshot.list",
        {},
    ),
    (
        "rosbag-list",
        "rosbag.list",
        {},
    ),
    (
        "custom-operation-validate",
        "custom_operation.validate",
        {
            "operation": "hover",
            "arguments": {
                "duration_s": 1.0,
                "sustain": False,
            },
        },
    ),
    (
        "custom-operation-hover-start",
        "custom_operation.hover.start",
        {
            "operation": "hover",
            "hold_confirmed": True,
            "arguments": {
                "duration_s": 1.0,
                "sustain": False,
            },
        },
    ),
    ("custom-operation-cancel", "custom_operation.cancel", {}),
]

FLIGHT_COMMANDS = [
    ("px4-arm", "px4.arm", {}),
    ("px4-takeoff", "px4.takeoff", {"altitude_m": 1.5}),
    ("px4-hold", "px4.hold", {}),
    ("px4-land", "px4.land", {}),
]

EXPECTED_BENCH_DEGRADED_COMMANDS = {
    "powerline.overview.update": "at least 4 live powerline lines are required",
    "custom_operation.hover.start": "CustomOperation mode is not active",
}

# Acceptance evidence deliberately excludes raw images and point clouds. Those
# streams can exceed 50 MiB/s and are validated through the dedicated perception
# state endpoints instead of making every GUI smoke run a multi-gigabyte bag.
INSPECTION_EVIDENCE_TOPICS = [
    "/control/maneuver_controller/combined_drone_awareness",
    "/control/maneuver_controller/current_maneuver",
    "/control/maneuver_controller/reference",
    "/fmu/out/battery_status",
    "/fmu/out/vehicle_land_detected",
    "/fmu/out/vehicle_local_position",
    "/fmu/out/vehicle_status_v1",
    "/mission/custom_operation/mode_status",
    "/mission/modes/cable_charging/status",
    "/mission/modes/inspection_demo/status",
    "/mission/modes/leave_cable/status",
    "/mission/modes/reach_cable/status",
    "/mission/powerline_overview_provider/overview_status",
    "/mission/pylon_overview_provider/overview_status",
    "/mission/status",
    "/payload/charger_gripper/charger_status",
    "/payload/charger_gripper/gripper_status",
    "/perception/pl_mapper/powerline",
    "/perception/pl_mapper/state",
    "/supervision/system_health",
]


class SmokeFailure(RuntimeError):
    pass


def recovery_required(args: argparse.Namespace) -> bool:
    """Return whether this run may have changed vehicle state."""
    return bool(args.run_mutating_workflows or args.run_flight_commands or args.run_inspection_cycle)


class SmokeRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.workspace = Path(args.workspace).resolve()
        self.artifacts = Path(args.artifacts_dir).resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.step_index = 0
        self.run_id = uuid4().hex[:12]
        self.summary: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "artifacts_dir": str(self.artifacts),
            "runtime_url": args.runtime_url,
            "proxy_url": args.proxy_url,
            "frontend_url": args.frontend_url,
            "steps": [],
            "mutating_workflows": bool(args.run_mutating_workflows),
            "flight_commands": bool(args.run_flight_commands),
            "inspection_cycle": bool(args.run_inspection_cycle),
        }
        self.session_headers: dict[str, str] | None = None
        self.session_token: str | None = None
        self.endpoint_id: str | None = None
        self._last_heartbeat_at = 0.0
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_error: str | None = None

    def run(self) -> int:
        compose_started = False
        try:
            if self.args.start_compose:
                self.compose_up()
                compose_started = True
            self.wait_for_url(f"{self.args.runtime_url}/identity", "runtime identity")
            self.wait_for_url(f"{self.args.proxy_url}/identity", "GC proxy identity")
            if self.args.frontend_url:
                self.wait_for_url(f"{self.args.frontend_url}/", "frontend")

            runtime_identity = self.http_json("GET", f"{self.args.runtime_url}/identity", step_name="runtime-identity")
            self.summary["runtime_identity"] = runtime_identity
            if runtime_identity.get("profile") != self.args.expected_profile:
                raise SmokeFailure(
                    f"expected {self.args.expected_profile} runtime profile, "
                    f"got {runtime_identity.get('profile')!r}"
                )

            self.http_json("GET", f"{self.args.proxy_url}/identity", step_name="proxy-identity")
            self.http_json("GET", f"{self.args.proxy_url}/runtime/discovery?timeout_s=2", step_name="runtime-discovery")
            endpoint = self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/discovery/manual",
                {
                    "base_url": self.args.runtime_url,
                    "runtime_name": f"iii-{self.args.expected_profile}",
                },
                step_name="manual-runtime-endpoint",
            )
            endpoint_id = endpoint["endpoint_id"]
            self.endpoint_id = endpoint_id
            self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/targets/validate",
                {"endpoint_id": endpoint_id},
                step_name="validate-runtime-target",
            )
            self.http_json(
                "POST",
                f"{self.args.proxy_url}/runtime/target/select",
                {"endpoint_id": endpoint_id},
                step_name="select-runtime-target",
            )
            self.http_json("GET", f"{self.args.proxy_url}/proxy/identity", step_name="proxied-runtime-identity")

            login = self.http_json(
                "POST",
                f"{self.args.proxy_url}/proxy/session/login",
                {"password": self.args.password, "client_label": "gui-v2-sim-e2e-smoke"},
                step_name="session-login",
            )
            token = login["session_token"]
            self.session_token = token
            headers = {"Authorization": f"Bearer {token}"}
            self.session_headers = headers
            self.summary["session_acquired"] = True
            self.start_session_heartbeat(headers)

            for name, path in DEFAULT_READ_ENDPOINTS:
                self.heartbeat(headers)
                self.http_json("GET", f"{self.args.proxy_url}{path}", headers=headers, step_name=name)

            if self.args.run_mutating_workflows:
                self.run_mutating_workflows(headers)
            if self.args.run_flight_commands:
                if not self.args.run_mutating_workflows:
                    raise SmokeFailure("--run-flight-commands requires --run-mutating-workflows")
                self.run_flight_commands(headers)
            if self.args.run_inspection_cycle:
                self.run_inspection_cycle(headers)
            elif self.args.capture_frontend:
                self.capture_frontend_screenshot("frontend-final")

            self.stop_session_heartbeat()
            self.http_json("POST", f"{self.args.proxy_url}/proxy/session/logout", headers=headers, step_name="session-logout")
            self.session_headers = None
            self.summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.summary["status"] = "passed"
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            self.summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.summary["status"] = "failed"
            self.summary["failure"] = str(exc)
            print(f"GUI v2 sim E2E smoke failed: {exc}", file=sys.stderr)
            print(f"Artifacts: {self.artifacts}", file=sys.stderr)
            if recovery_required(self.args) and self.session_headers:
                self.recover_vehicle(self.session_headers)
            return 1
        finally:
            self.stop_session_heartbeat()
            if self.session_headers:
                try:
                    self.http_json(
                        "POST",
                        f"{self.args.proxy_url}/proxy/session/logout",
                        headers=self.session_headers,
                        step_name="session-logout-after-failure",
                    )
                except Exception as exc:
                    self.summary["session_logout_warning"] = str(exc)
                self.session_headers = None
            if compose_started:
                self.capture_compose_logs()
                if not self.args.keep_compose:
                    self.compose_down()
            self.write_summary()

    def run_mutating_workflows(self, headers: dict[str, str]) -> None:
        recording_id: str | None = None
        for name, command_id, parameters in MUTATING_WORKFLOW_COMMANDS:
            parameters = dict(parameters)
            if command_id == "runtime.boot":
                parameters["profile"] = self.args.expected_profile
            expected_degraded_message = EXPECTED_BENCH_DEGRADED_COMMANDS.get(command_id)
            result = self.dispatch_command(
                name,
                command_id,
                parameters,
                headers,
                require_accepted=expected_degraded_message is None,
            )
            if command_id == "rosbag.start":
                rosbag = (result.get("result") or {}).get("rosbag") or {}
                recording_id = rosbag.get("recording_id")
                if not recording_id:
                    raise SmokeFailure("rosbag.start did not return a recording ID")
                self.wait_for_state(
                    "rosbag-smoke-active",
                    "/proxy/rosbag/status",
                    headers,
                    lambda state: state.get("recording") is True
                    and state.get("recording_id") == recording_id,
                    timeout_s=10.0,
                    interval_s=0.5,
                )
                # MCAP output is buffered and can legitimately remain zero bytes
                # until SIGINT closes the writer. Give live topics time to arrive,
                # then prove non-empty persistence after the stop below.
                time.sleep(self.args.rosbag_observation_s)
            elif command_id == "rosbag.stop" and recording_id:
                state = self.get_state("/proxy/rosbag/status", headers)
                recordings = (state.get("latest") or {}).get("recordings") or []
                saved = next(
                    (item for item in recordings if item.get("recording_id") == recording_id),
                    None,
                )
                if saved is None or int(saved.get("size_bytes") or 0) <= 0:
                    raise SmokeFailure(
                        f"rosbag recording {recording_id} was not retained with data after stop"
                    )
            if expected_degraded_message is not None and not result.get("accepted"):
                rejection = result.get("rejection") or result
                if rejection.get("code") != "degraded_state" or expected_degraded_message not in str(
                    rejection.get("message", "")
                ):
                    raise SmokeFailure(
                        f"{command_id} had an unexpected rejection: "
                        f"{json.dumps(rejection, sort_keys=True)}"
                    )
        self.round_trip_safe_configuration_parameter(headers)

    def round_trip_safe_configuration_parameter(self, headers: dict[str, str]) -> None:
        name = "/control/trajectory_interpolator/interpolation_avg_velocity_m_s"
        initial = self.wait_for_state(
            "configuration-round-trip-ready",
            "/proxy/configuration/status",
            headers,
            configuration_manifest_available,
            timeout_s=30.0,
        )
        parameter = configuration_parameter(initial, name)
        if parameter is None or not isinstance(parameter.get("current_value"), (int, float)):
            raise SmokeFailure(f"safe configuration round-trip parameter is unavailable: {name}")
        original = float(parameter["current_value"])
        constraints = parameter.get("constraints") or {}
        maximum = constraints.get("maximum")
        candidate = original + 0.01
        if isinstance(maximum, (int, float)) and candidate > float(maximum):
            candidate = original - 0.01
        edit = {"node_id": parameter["node_id"], "name": name, "value": candidate}
        changed = False
        changed_state = initial
        try:
            self.apply_configuration_edit_with_retry(
                "configuration-round-trip-apply",
                edit,
                headers,
                timeout_s=60.0,
            )
            changed = True
            changed_state = self.wait_for_state(
                "configuration-round-trip-changed",
                "/proxy/configuration/status",
                headers,
                lambda current: numeric_values_match(
                    (configuration_parameter(current, name) or {}).get("current_value"), candidate
                )
                and numeric_values_match(
                    (configuration_parameter(current, name) or {}).get("persisted_value"), candidate
                ),
                timeout_s=30.0,
            )
        finally:
            if changed:
                self.apply_configuration_edit_with_retry(
                    "configuration-round-trip-restore",
                    {"node_id": parameter["node_id"], "name": name, "value": original},
                    headers,
                    timeout_s=60.0,
                )
                self.wait_for_state(
                    "configuration-round-trip-restored",
                    "/proxy/configuration/status",
                    headers,
                    lambda current: numeric_values_match(
                        (configuration_parameter(current, name) or {}).get("current_value"), original
                    )
                    and numeric_values_match(
                        (configuration_parameter(current, name) or {}).get("persisted_value"), original
                    ),
                    timeout_s=30.0,
                )

    def run_flight_commands(self, headers: dict[str, str]) -> None:
        self.wait_for_stable_state(
            "px4-arming-ready",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("arming_checks_passed") is True
            and (state.get("latest") or {}).get("command_transport", {}).get("command_available") is True,
            timeout_s=90.0,
            consecutive_samples=3,
        )
        self.dispatch_command("px4-arm", "px4.arm", {}, headers)
        self.wait_for_stable_state(
            "px4-armed",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("armed") is True
            and telemetry_field_matches(state, "armed", True),
            timeout_s=30.0,
            consecutive_samples=2,
        )

        self.dispatch_command("px4-takeoff", "px4.takeoff", {"altitude_m": 1.5}, headers)
        self.wait_for_state(
            "px4-airborne",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("armed") is True
            and state.get("in_air") is True
            and telemetry_field_matches(state, "in_air", True),
            timeout_s=45.0,
        )

        self.dispatch_command("px4-hold", "px4.hold", {}, headers)
        self.wait_for_stable_state(
            "px4-holding",
            "/proxy/vehicle/status",
            headers,
            lambda state: nav_is(state, "hold")
            and state.get("in_air") is True
            and telemetry_field_matches(state, "nav_state", "hold"),
            timeout_s=30.0,
            consecutive_samples=2,
        )

        self.dispatch_command("px4-land", "px4.land", {}, headers)
        self.wait_for_stable_state(
            "px4-landed-disarmed",
            "/proxy/vehicle/status",
            headers,
            vehicle_landed_and_disarmed,
            timeout_s=120.0,
            consecutive_samples=4,
        )

    def run_inspection_cycle(self, headers: dict[str, str]) -> None:
        """Exercise the operator inspection path through the GUI runtime API."""
        self.capture_operator_state("inspection-preflight", headers)
        vehicle = self.get_state("/proxy/vehicle/status", headers)
        if vehicle.get("armed") or vehicle.get("in_air"):
            raise SmokeFailure("inspection cycle requires a disarmed, landed vehicle")

        self.reset_sim_battery(self.args.initial_battery_pct)
        self.wait_for_state(
            "inspection-battery-reset-confirmed",
            "/proxy/vehicle/status",
            headers,
            lambda state: simulated_battery_reset_visible(state, self.args.initial_battery_pct),
            timeout_s=15.0,
        )
        self.ensure_inspection_sim_configuration(headers)

        self.wait_for_state(
            "inspection-px4-command-ready",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("latest", {}).get("command_transport", {}).get("command_available") is True
            and state.get("arming_checks_passed") is True,
            timeout_s=90.0,
        )

        self.dispatch_command("inspection-arm", "px4.arm", {}, headers)
        self.wait_for_stable_state(
            "inspection-armed",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("armed") is True
            and telemetry_field_matches(state, "armed", True),
            timeout_s=30.0,
            consecutive_samples=2,
        )
        self.dispatch_command("inspection-takeoff", "px4.takeoff", {"altitude_m": 2.0}, headers)
        self.wait_for_state(
            "inspection-airborne",
            "/proxy/vehicle/status",
            headers,
            lambda state: state.get("armed") is True and state.get("in_air") is True,
            timeout_s=45.0,
        )
        self.wait_for_state(
            "inspection-takeoff-complete",
            "/proxy/vehicle/status",
            headers,
            lambda state: nav_is(state, "hold"),
            timeout_s=60.0,
        )
        self.position_fixture(self.args.overview_fixture, headers, cable_aware=True)
        self.dispatch_command("inspection-mapper-start", "perception.pl_mapper.start", {}, headers)
        self.wait_for_state(
            "inspection-mapper-live",
            "/proxy/perception/status",
            headers,
            mapper_active,
            timeout_s=30.0,
        )
        powerline_state = self.get_state("/proxy/powerline/status", headers)
        if (
            powerline_state.get("stored_overview_valid") is True
            and powerline_state.get("stored_overview_source") == "live_mapper_store"
        ):
            self.record_structured_artifact(
                "inspection-powerline-overview-reused",
                {"source": "live_mapper_store", "reason": "current-session live overview is already qualified"},
            )
        else:
            self.wait_for_stable_state(
                "inspection-powerline-capture-ready",
                "/proxy/powerline/status",
                headers,
                lambda state: state.get("latest", {}).get("capture_ready") is True
                and visible_live_line_count(state) >= self.args.min_powerline_lines,
                timeout_s=60.0,
                consecutive_samples=3,
            )
            self.dispatch_retryable_command(
                "inspection-overview-store",
                "powerline.overview.update",
                {"timeout_s": 10},
                headers,
                timeout_s=self.args.overview_guard_s - 5.0,
                interval_s=0.5,
            )
        self.dispatch_command("inspection-pylons-clear", "pylon.overview.clear", {}, headers)
        # Exercise the same component-by-component GUI commands used in the
        # field without teleporting an armed PX4 vehicle. The simulation-only
        # fixture seed below then supplies the real test-world pylon endpoints.
        for pylon_id in (1, 2):
            self.dispatch_retryable_command(
                f"inspection-pylon-{pylon_id}-capture",
                "pylon.capture_current",
                {"pylon_id": pylon_id},
                headers,
                timeout_s=45.0,
            )
        self.seed_sim_pylon_overview(("pos_pylon_1", "pos_pylon_2"))
        self.wait_for_state(
            "inspection-overviews-complete",
            "/proxy/powerline/status",
            headers,
            overviews_complete,
            timeout_s=20.0,
        )

        self.position_fixture(self.args.inspection_start_fixture, headers)
        # Staging is deliberately flight-heavy. Restore the sim-only acceptance
        # fixture here so manual recharge is exercised before automatic depletion.
        self.reset_sim_battery(self.args.initial_battery_pct)
        self.wait_for_state(
            "inspection-pre-mission-battery-confirmed",
            "/proxy/vehicle/status",
            headers,
            lambda state: simulated_battery_reset_visible(state, self.args.initial_battery_pct),
            timeout_s=15.0,
        )
        before = self.get_state("/proxy/vehicle/status", headers)
        self.dispatch_command(
            "inspection-activate",
            "mission.activate",
            {"mode_key": "inspection_demo"},
            headers,
        )
        self.wait_for_mission_mode("inspection-running", "inspection_demo", headers, timeout_s=45.0)
        time.sleep(self.args.inspection_observation_s)
        after = self.get_state("/proxy/vehicle/status", headers)
        assert_battery_depleted(before, after)
        self.capture_operator_state("inspection-before-recharge", headers)

        self.dispatch_command("inspection-recharge-now", "mission.recharge_now", {}, headers)
        self.wait_for_mission_mode("inspection-reach-cable", "reach_cable", headers, timeout_s=180.0)
        self.wait_for_mission_mode("inspection-charging", "cable_charging", headers, timeout_s=180.0)
        charging_start = self.get_state("/proxy/vehicle/status", headers)
        charging_end = self.wait_for_state(
            "inspection-battery-charging",
            "/proxy/vehicle/status",
            headers,
            lambda state: battery_increased(charging_start, state),
            timeout_s=self.args.charging_observation_s,
            interval_s=0.25,
        )
        assert_battery_charged(charging_start, charging_end)

        charging_mode = self.get_state("/proxy/mission/status", headers)
        if active_mission_mode(charging_mode) != "cable_charging":
            raise SmokeFailure(
                "cable charging ended before the operator leave command could be exercised; "
                "reduce the initial battery level or charging observation threshold"
            )
        self.dispatch_command("inspection-leave-now", "mission.leave_cable_now", {}, headers)
        self.wait_for_mission_mode("inspection-leave-cable", "leave_cable", headers, timeout_s=90.0)
        self.wait_for_mission_mode("inspection-resumed", "inspection_demo", headers, timeout_s=180.0)
        self.capture_operator_state("inspection-resumed-state", headers)

        self.dispatch_command("inspection-global-hold", "px4.hold", {}, headers)
        self.wait_for_state(
            "inspection-hold-confirmed",
            "/proxy/vehicle/status",
            headers,
            lambda state: nav_is(state, "hold"),
            timeout_s=30.0,
        )
        self.wait_for_state(
            "inspection-mission-released",
            "/proxy/mission/status",
            headers,
            lambda state: not mission_active(state),
            timeout_s=30.0,
        )
        released_mission = self.get_state("/proxy/mission/status", headers)
        self.wait_for_stable_state(
            "inspection-modes-remain-selectable",
            "/proxy/vehicle/status",
            headers,
            lambda state: mission_modes_selectable(state, released_mission),
            timeout_s=15.0,
            consecutive_samples=8,
            interval_s=0.5,
        )
        self.dispatch_command("inspection-land", "px4.land", {}, headers)
        self.wait_for_stable_state(
            "inspection-landed",
            "/proxy/vehicle/status",
            headers,
            vehicle_landed_and_disarmed,
            timeout_s=120.0,
            consecutive_samples=4,
        )
        self.wait_for_stable_state(
            "inspection-terminated-cleanly",
            "/proxy/mission/status",
            headers,
            lambda state: state.get("operational_safety", {}).get("status") == "normal",
            timeout_s=15.0,
            consecutive_samples=3,
        )
        self.dispatch_command(
            "inspection-recording-stop",
            "rosbag.stop",
            {"hold_confirmed": True},
            headers,
        )
        self.capture_operator_state("inspection-final", headers)
        self.capture_frontend_screenshot("inspection-final")

    def ensure_inspection_sim_configuration(self, headers: dict[str, str]) -> None:
        state = self.wait_for_state(
            "inspection-configuration-ready",
            "/proxy/configuration/status",
            headers,
            configuration_manifest_available,
            timeout_s=30.0,
        )
        requested = {
            "/control/trajectory_interpolator/interpolation_avg_velocity_m_s": self.args.inspection_translation_speed_m_s,
            "/control/trajectory_interpolator/interpolation_avg_yaw_rate_rad_s": self.args.inspection_yaw_rate_rad_s,
        }
        edits = []
        for name, value in requested.items():
            parameter = configuration_parameter(state, name)
            if parameter is None:
                raise SmokeFailure(f"sim acceptance parameter is absent from configuration manifest: {name}")
            if not numeric_values_match(parameter.get("current_value"), value):
                edits.append({"node_id": parameter["node_id"], "name": name, "value": value})
        if edits:
            self.dispatch_command(
                "inspection-configuration-apply",
                "configuration.apply",
                {"edits": edits},
                headers,
            )
        self.wait_for_state(
            "inspection-configuration-confirmed",
            "/proxy/configuration/status",
            headers,
            lambda current: all(
                numeric_values_match((configuration_parameter(current, name) or {}).get("current_value"), value)
                and numeric_values_match((configuration_parameter(current, name) or {}).get("persisted_value"), value)
                for name, value in requested.items()
            ),
            timeout_s=30.0,
        )

    def call_sim_mcp(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        artifact_name: str,
        timeout_s: float = 30.0,
        acceptable_result: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        discovery = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=devcontainer.local_folder={self.workspace}",
                "--format",
                "{{.ID}}",
            ],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        containers = [line.strip() for line in discovery.stdout.splitlines() if line.strip()]
        if discovery.returncode != 0 or len(containers) != 1:
            raise SmokeFailure(
                f"expected one workspace devcontainer for {tool}, found {len(containers)}: "
                f"{discovery.stderr.strip()}"
            )
        serialized_arguments = json.dumps(arguments, separators=(",", ":"))
        px4_system_address = "udpin://0.0.0.0:14540"
        runtime_environment = ""
        if self.args.expected_profile == "hil":
            workstation_address = os.environ.get("III_HIL_WORKSTATION_ADDRESS", "10.42.0.1")
            pi_address = os.environ.get("III_HIL_PI_ADDRESS", "10.42.0.15")
            ros_domain_id = os.environ.get("III_HIL_ROS_DOMAIN_ID", "42")
            gz_partition = os.environ.get("III_HIL_GZ_PARTITION", "iii_hil_0")
            parameter_port = os.environ.get("III_HIL_MAVLINK_PARAMETER_REMOTE_PORT", "14551")
            px4_system_address = f"udpin://0.0.0.0:{parameter_port}"
            cyclone_uri = (
                "<CycloneDDS><Domain><General><Interfaces>"
                f'<NetworkInterface address="{workstation_address}" priority="default" multicast="default"/>'
                "</Interfaces></General><Discovery><Peers>"
                f'<Peer address="{pi_address}"/>'
                "</Peers></Discovery></Domain></CycloneDDS>"
            )
            runtime_environment = (
                f"export ROS_DOMAIN_ID={shlex.quote(ros_domain_id)} "
                "ROS_LOCALHOST_ONLY=0 ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET "
                "ROS2CLI_DISABLE_DAEMON=1 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp "
                "III_DRONE_MCP_KEEP_RMW=1 III_MAVSDK_SERVER_PORT=50052 "
                f"GZ_PARTITION={shlex.quote(gz_partition)} "
                f"CYCLONEDDS_URI={shlex.quote(cyclone_uri)} && "
            )
        try:
            artifact_relative = self.artifacts.relative_to(self.workspace)
        except ValueError as exc:
            raise SmokeFailure("MCP acceptance artifacts must be inside the workspace") from exc
        mcp_artifacts = Path("/home/iii/ws") / artifact_relative / "mcp"
        shell = (
            "source /opt/ros/jazzy/setup.bash && "
            "cd /home/iii/ws && source install/setup.bash && "
            f"{runtime_environment}"
            f"iii-drone-mcp-call --json --artifact-dir {shlex.quote(str(mcp_artifacts))} "
            f"--px4-system-address {shlex.quote(px4_system_address)} "
            f"{shlex.quote(tool)} {shlex.quote(serialized_arguments)}"
        )
        result = subprocess.run(
            ["docker", "exec", "--user", "iii", containers[0], "bash", "-lc", shell],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_s,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SmokeFailure(
                f"{tool} returned invalid JSON: {(result.stdout + result.stderr).strip()}"
            ) from exc
        self.record_structured_artifact(artifact_name, payload)
        accepted = result.returncode == 0 and payload.get("success") is True
        if not accepted and acceptable_result is not None:
            accepted = acceptable_result(payload)
        if not accepted:
            raise SmokeFailure(f"{tool} failed: {payload.get('message') or result.stderr.strip()}")
        return payload

    def reset_sim_battery(self, remaining_pct: float) -> None:
        self.call_sim_mcp(
            "battery.reset",
            {"remaining_pct": float(remaining_pct), "timeout_sec": 15.0, "tolerance_pct": 1.0},
            artifact_name="inspection-battery-reset",
            acceptable_result=(
                acknowledged_hil_battery_reset
                if self.args.expected_profile == "hil"
                else None
            ),
        )

    def seed_sim_pylon_overview(self, fixture_ids: tuple[str, str]) -> None:
        """Seed mapped test-world pylons without moving the simulated aircraft."""
        targets = [self.resolve_fixture(fixture_id) for fixture_id in fixture_ids]
        self.call_sim_mcp(
            "perception.clear_pylon_overview",
            {"timeout_sec": 5.0},
            artifact_name="inspection-sim-pylons-seed-clear",
        )
        for pylon_id, (fixture_id, target) in enumerate(zip(fixture_ids, targets, strict=True), start=1):
            self.call_sim_mcp(
                "perception.store_pylon_overview",
                {
                    "pylon_id": pylon_id,
                    "x": float(target["x"]),
                    "y": float(target["y"]),
                    "frame_id": "world",
                    "timeout_sec": 5.0,
                    "filename": f"gui_e2e_seeded_pylon_{pylon_id}.json",
                },
                artifact_name=f"inspection-sim-pylon-{pylon_id}-seed",
            )
            self.record_structured_artifact(
                f"inspection-sim-pylon-{pylon_id}-fixture",
                {"setup_only": True, "fixture_id": fixture_id, "mapped_world_target": target},
            )

    def position_fixture(
        self,
        fixture_id: str,
        headers: dict[str, str],
        *,
        cable_aware: bool = False,
    ) -> None:
        target = self.resolve_fixture(fixture_id)
        self.dispatch_command(f"fixture-{fixture_id}-release-hold", "custom_operation.activate", {}, headers)
        self.wait_for_state(
            f"fixture-{fixture_id}-custom-mode",
            "/proxy/operations/status",
            headers,
            lambda state: state.get("latest", {}).get("control_owner") == "custom_operation",
            timeout_s=15.0,
        )
        command_id = (
            "custom_operation.cable_aware_fly_to_position.start"
            if cable_aware
            else "custom_operation.fly_to_position.start"
        )
        started = self.dispatch_command(
            f"fixture-{fixture_id}-flight-start",
            command_id,
            {
                "hold_confirmed": True,
                "arguments": {
                    "frame_id": str(target.get("frame_id") or "world"),
                    "x": float(target["x"]),
                    "y": float(target["y"]),
                    # Recorded fixture height can sit just below the maneuver
                    # controller's live ground-clearance estimate. Keep the
                    # real altitude guard enabled and add a small setup margin.
                    "z": float(target["z"]) + 0.06,
                    "yaw": float(target["yaw"]),
                    "blend_to_next": False,
                    "ignore_altitude": False,
                },
            },
            headers,
        )
        action_id = str(started.get("action_id") or "")
        if not action_id:
            raise SmokeFailure(f"fixture flight did not return an action id: {fixture_id}")
        self.wait_for_state(
            f"fixture-{fixture_id}-flight-active",
            "/proxy/operations/status",
            headers,
            lambda state: operation_started_or_finished(state, action_id),
            timeout_s=20.0,
        )
        self.wait_for_state(
            f"fixture-{fixture_id}-flight-complete",
            "/proxy/operations/status",
            headers,
            lambda state: operation_finished(state, operation_id=action_id),
            timeout_s=150.0,
        )
        self.dispatch_command(f"fixture-{fixture_id}-capture-hold", "px4.hold", {}, headers)
        self.wait_for_state(
            f"fixture-{fixture_id}-settled",
            "/proxy/vehicle/status",
            headers,
            lambda state: nav_is(state, "hold") and state.get("in_air") is True,
            timeout_s=30.0,
        )
        time.sleep(self.args.fixture_settle_s)

    def resolve_fixture(
        self,
        fixture_id: str,
        *,
        apply: bool = False,
        hold_duration_s: float = 0.0,
    ) -> dict[str, Any]:
        command = [
            self.args.fixture_resolver,
            "--fixture-id",
            fixture_id,
            "--workspace",
            str(self.workspace),
            "--profile",
            self.args.expected_profile,
        ]
        if apply:
            command.append("--apply")
        if hold_duration_s:
            command.extend(("--hold-duration-s", str(hold_duration_s)))
        result = subprocess.run(command, cwd=self.workspace, text=True, capture_output=True, check=False)
        artifact = self.artifacts / f"fixture-{fixture_id}.log"
        artifact.write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise SmokeFailure(f"fixture resolver failed for {fixture_id}; see {artifact}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"fixture resolver returned invalid JSON for {fixture_id}") from exc
        if not payload.get("success"):
            raise SmokeFailure(f"fixture resolution failed for {fixture_id}: {payload.get('message')}")
        target = payload["data"]
        if apply:
            if target.get("setup_only") is not True or not isinstance(target.get("gazebo_pose"), dict):
                raise SmokeFailure(
                    f"fixture application for {fixture_id} did not return authoritative Gazebo setup evidence"
                )
            return target
        if target.get("target_source") != "gazebo_ground_truth_mapped_to_live_ros_world":
            raise SmokeFailure(
                f"fixture resolution for {fixture_id} used non-authoritative source: "
                f"{target.get('target_source') or 'unknown'}"
            )
        return target

    def wait_for_mission_mode(self, name: str, mode_key: str, headers: dict[str, str], *, timeout_s: float) -> dict:
        return self.wait_for_state(
            name,
            "/proxy/mission/status",
            headers,
            lambda state: active_mission_mode(state) == mode_key,
            timeout_s=timeout_s,
        )

    def wait_for_state(
        self,
        name: str,
        path: str,
        headers: dict[str, str],
        predicate: Any,
        *,
        timeout_s: float,
        interval_s: float = 0.5,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        samples: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            self.heartbeat(headers)
            state = self.get_state(path, headers)
            samples.append({"at": datetime.now(timezone.utc).isoformat(), "state": state})
            if predicate(state):
                self.record_structured_artifact(name, {"status": "satisfied", "samples": samples})
                return state
            time.sleep(interval_s)
        self.record_structured_artifact(name, {"status": "timed_out", "samples": samples})
        raise SmokeFailure(f"timed out waiting for {name}; last state={json.dumps(samples[-1]['state'] if samples else {}, sort_keys=True)}")

    def wait_for_stable_state(
        self,
        name: str,
        path: str,
        headers: dict[str, str],
        predicate: Any,
        *,
        timeout_s: float,
        consecutive_samples: int,
        interval_s: float = 0.5,
    ) -> dict:
        if consecutive_samples < 1:
            raise ValueError("consecutive_samples must be positive")
        deadline = time.monotonic() + timeout_s
        samples: list[dict[str, Any]] = []
        consecutive = 0
        while time.monotonic() < deadline:
            self.heartbeat(headers)
            state = self.get_state(path, headers)
            samples.append({"at": datetime.now(timezone.utc).isoformat(), "state": state})
            consecutive = consecutive + 1 if predicate(state) else 0
            if consecutive >= consecutive_samples:
                self.record_structured_artifact(
                    name,
                    {"status": "satisfied", "consecutive_samples": consecutive, "samples": samples},
                )
                return state
            time.sleep(interval_s)
        self.record_structured_artifact(
            name,
            {"status": "timed_out", "consecutive_samples": consecutive, "samples": samples},
        )
        raise SmokeFailure(
            f"timed out waiting for stable {name}; "
            f"last state={json.dumps(samples[-1]['state'] if samples else {}, sort_keys=True)}"
        )

    def get_state(self, path: str, headers: dict[str, str]) -> dict:
        return self.http_json(
            "GET",
            f"{self.args.proxy_url}{path}",
            headers=headers,
            step_name="poll",
            record=False,
        )

    def heartbeat(self, headers: dict[str, str]) -> None:
        with self._heartbeat_lock:
            if time.monotonic() - self._last_heartbeat_at < 1.0:
                return
            self.http_json(
                "POST",
                f"{self.args.proxy_url}/proxy/session/heartbeat",
                headers=headers,
                step_name="heartbeat",
                record=False,
            )
            self._last_heartbeat_at = time.monotonic()

    def start_session_heartbeat(self, headers: dict[str, str]) -> None:
        """Maintain the short browser lease across blocking commands and tools."""
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()
        self._heartbeat_error = None

        def maintain_lease() -> None:
            while not self._heartbeat_stop.wait(1.0):
                try:
                    self.heartbeat(headers)
                except Exception as exc:
                    self._heartbeat_error = str(exc)

        self._heartbeat_thread = threading.Thread(
            target=maintain_lease,
            name="gui-v2-e2e-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop_session_heartbeat(self) -> None:
        thread = self._heartbeat_thread
        if thread is None:
            return
        self._heartbeat_stop.set()
        thread.join(timeout=3.0)
        self._heartbeat_thread = None
        if self._heartbeat_error:
            self.summary["heartbeat_warning"] = self._heartbeat_error

    def capture_operator_state(self, name: str, headers: dict[str, str]) -> None:
        domains = {}
        for domain_name, path in DEFAULT_READ_ENDPOINTS:
            if domain_name in {"commands-handlers"}:
                continue
            try:
                self.heartbeat(headers)
                domains[domain_name] = self.get_state(path, headers)
            except Exception as exc:
                domains[domain_name] = {"capture_error": str(exc)}
        self.record_structured_artifact(name, domains)

    def record_structured_artifact(self, name: str, payload: Any) -> Path:
        self.step_index += 1
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)
        path = self.artifacts / f"{self.step_index:02d}-{safe_name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.summary["steps"].append({"name": name, "status": "captured", "artifact": str(path)})
        print(f"[{self.step_index:02d}] {name}: captured")
        return path

    def capture_frontend_screenshot(self, name: str) -> None:
        if not self.args.frontend_url:
            return
        if not self.session_token or not self.endpoint_id:
            raise SmokeFailure("authenticated frontend screenshot requires an active runtime session")
        output = self.artifacts / f"{name}.png"
        identity = self.summary.get("runtime_identity", {})
        session = frontend_session_payload(
            token=self.session_token,
            endpoint_id=self.endpoint_id,
            runtime_name=str(identity.get("runtime_name") or "III Drone Sim Runtime"),
            runtime_id=identity.get("runtime_id"),
            system_id=identity.get("host_label"),
            profile=identity.get("profile"),
        )
        capture_authenticated_chromium(
            browser_binary=self.args.browser_binary,
            frontend_url=self.args.frontend_url,
            output=output,
            session=session,
        )
        self.summary["steps"].append({"name": name, "status": "captured", "artifact": str(output)})

    def recover_vehicle(self, headers: dict[str, str]) -> None:
        recovery: dict[str, Any] = {"attempted_at": datetime.now(timezone.utc).isoformat(), "commands": []}
        try:
            operation = self.get_state("/proxy/operations/status", headers)
            if operation.get("active_operation_id") or operation.get("latest", {}).get("operation_active") is True:
                result = self.dispatch_command(
                    "failure-operation-cancel",
                    "custom_operation.cancel",
                    {},
                    headers,
                    require_accepted=False,
                )
                recovery["commands"].append(result)
                self.wait_for_stable_state(
                    "failure-operation-stopped",
                    "/proxy/operations/status",
                    headers,
                    lambda state: state.get("active_operation_id") is None
                    and state.get("latest", {}).get("operation_active") is not True,
                    timeout_s=45.0,
                    consecutive_samples=3,
                )
        except Exception as exc:
            recovery["commands"].append({"command_id": "custom_operation.cancel", "error": str(exc)})
        for name, command_id, parameters in (
            ("failure-hold", "px4.hold", {}),
            ("failure-land", "px4.land", {}),
        ):
            try:
                state = self.get_state("/proxy/vehicle/status", headers)
                if command_id == "px4.land" and state.get("in_air") is not True:
                    recovery["commands"].append({"command_id": command_id, "skipped": "vehicle not in air"})
                    continue
                result = self.dispatch_command(name, command_id, parameters, headers, require_accepted=False)
                recovery["commands"].append(result)
                if command_id == "px4.land" and result.get("accepted"):
                    self.wait_for_stable_state(
                        "failure-landed",
                        "/proxy/vehicle/status",
                        headers,
                        vehicle_landed_and_disarmed,
                        timeout_s=120.0,
                        consecutive_samples=4,
                    )
            except Exception as exc:
                recovery["commands"].append({"command_id": command_id, "error": str(exc)})
        try:
            recording = self.get_state("/proxy/rosbag/status", headers)
            if recording.get("recording") is True:
                result = self.dispatch_command(
                    "failure-recording-stop",
                    "rosbag.stop",
                    {"hold_confirmed": True},
                    headers,
                    require_accepted=False,
                )
                recovery["commands"].append(result)
        except Exception as exc:
            recovery["commands"].append({"command_id": "rosbag.stop", "error": str(exc)})
        try:
            recovery["final_vehicle"] = self.get_state("/proxy/vehicle/status", headers)
        except Exception as exc:
            recovery["final_vehicle_error"] = str(exc)
        self.record_structured_artifact("failure-safe-recovery", recovery)

    def dispatch_command(
        self,
        name: str,
        command_id: str,
        parameters: dict[str, Any],
        headers: dict[str, str],
        *,
        require_accepted: bool = True,
    ) -> dict:
        self.heartbeat(headers)
        request_id = build_request_id(self.run_id, self.step_index + 1, name)
        payload = {
            "request_id": request_id,
            "client_label": "gui-v2-sim-e2e-smoke",
            "command_id": command_id,
            "parameters": parameters,
        }
        result = self.http_json(
            "POST",
            f"{self.args.proxy_url}/proxy/commands/actions/start",
            payload,
            headers=headers,
            step_name=name,
        )
        if require_accepted and not result.get("accepted"):
            raise SmokeFailure(f"{command_id} rejected: {json.dumps(result.get('rejection') or result, sort_keys=True)}")
        return result

    def dispatch_retryable_command(
        self,
        name: str,
        command_id: str,
        parameters: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout_s: float,
        interval_s: float = 1.0,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        attempt = 0
        last_rejection: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            attempt += 1
            result = self.dispatch_command(
                f"{name}-attempt-{attempt}",
                command_id,
                parameters,
                headers,
                require_accepted=False,
            )
            if result.get("accepted"):
                return result
            rejection = result.get("rejection") or result
            last_rejection = rejection
            if rejection.get("retryable") is not True:
                raise SmokeFailure(f"{command_id} rejected: {json.dumps(rejection, sort_keys=True)}")
            time.sleep(interval_s)
        raise SmokeFailure(
            f"{command_id} remained rejected for {timeout_s:.1f}s: "
            f"{json.dumps(last_rejection or {}, sort_keys=True)}"
        )

    def apply_configuration_edit_with_retry(
        self,
        name: str,
        edit: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout_s: float,
        interval_s: float = 1.0,
    ) -> dict:
        """Apply one edit while respecting the configuration revision CAS."""

        deadline = time.monotonic() + timeout_s
        attempt = 0
        last_rejection: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            state = self.get_state("/proxy/configuration/status", headers)
            current = configuration_parameter(state, str(edit["name"]))
            if current and numeric_values_match(current.get("current_value"), edit["value"]):
                return {"accepted": True, "result": {"already_applied": True}}
            attempt += 1
            result = self.dispatch_command(
                f"{name}-attempt-{attempt}",
                "configuration.apply",
                {"edits": [edit], "expected_revision": configuration_revision(state)},
                headers,
                require_accepted=False,
            )
            if result.get("accepted"):
                return result
            rejection = result.get("rejection") or result
            last_rejection = rejection
            stale_revision = (
                rejection.get("code") == "forbidden"
                and "stale expected revision" in str(rejection.get("message", ""))
            )
            if rejection.get("retryable") is not True and not stale_revision:
                raise SmokeFailure(
                    f"configuration.apply rejected: {json.dumps(rejection, sort_keys=True)}"
                )
            time.sleep(interval_s)
        raise SmokeFailure(
            f"configuration.apply remained rejected for {timeout_s:.1f}s: "
            f"{json.dumps(last_rejection or {}, sort_keys=True)}"
        )

    def wait_for_url(self, url: str, label: str) -> None:
        deadline = time.monotonic() + self.args.timeout_s
        last_error = ""
        while time.monotonic() < deadline:
            try:
                self.http_status("GET", url)
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1)
        raise SmokeFailure(f"timed out waiting for {label} at {url}: {last_error}")

    def http_status(self, method: str, url: str) -> int:
        request = Request(url, method=method, headers={"Accept": "*/*"})
        try:
            with urlopen(request, timeout=self.args.http_timeout_s) as response:
                return response.status
        except HTTPError as exc:
            raise SmokeFailure(f"{method} {url} returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise SmokeFailure(f"{method} {url} failed: {exc.reason}") from exc

    def http_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        step_name: str,
        record: bool = True,
    ) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Accept": "application/json"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(url, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=self.args.http_timeout_s) as response:
                raw = response.read()
                status = response.status
        except HTTPError as exc:
            raw = exc.read()
            self.record_http_step(step_name, method, url, exc.code, raw, body, record)
            raise SmokeFailure(f"{method} {url} returned HTTP {exc.code}: {raw.decode('utf-8', errors='replace')}") from exc
        except TimeoutError as exc:
            self.record_structured_artifact(
                f"{step_name}-transport-timeout",
                {"method": method, "url": url, "timeout_s": self.args.http_timeout_s, "request": body},
            )
            raise SmokeFailure(f"{method} {url} timed out after {self.args.http_timeout_s:.1f}s") from exc
        except URLError as exc:
            raise SmokeFailure(f"{method} {url} failed: {exc.reason}") from exc

        self.record_http_step(step_name, method, url, status, raw, body, record)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"{method} {url} returned non-JSON body") from exc

    def record_http_step(
        self,
        name: str,
        method: str,
        url: str,
        status: int,
        raw: bytes,
        request_body: dict[str, Any] | None,
        record: bool,
    ) -> None:
        if not record:
            return
        self.step_index += 1
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)
        path = self.artifacts / f"{self.step_index:02d}-{safe_name}.json"
        try:
            response_body: Any = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            response_body = raw.decode("utf-8", errors="replace")
        payload = {
            "name": name,
            "method": method,
            "url": url,
            "status": status,
            "request": request_body,
            "response": response_body,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.summary["steps"].append({"name": name, "status": status, "artifact": str(path)})
        print(f"[{self.step_index:02d}] {name}: HTTP {status}")

    def compose_up(self) -> None:
        # A prior interrupted smoke may leave same-project containers whose
        # published ports no longer match this invocation.  Clean only this
        # explicitly named test project before recreating it.
        self.compose_down()
        env = os.environ.copy()
        proxy_port = port_from_url(self.args.proxy_url, default="18780")
        frontend_port = port_from_url(self.args.frontend_url, default="5174")
        env.setdefault("III_GC_PROXY_PORT", proxy_port)
        env.setdefault("III_GC_FRONTEND_PORT", frontend_port)
        env.setdefault("III_GC_PROXY_PUBLIC_URL", self.args.proxy_url)
        cors_origins = merge_csv_values(
            env.get("III_GC_PROXY_CORS_ORIGINS", ""),
            localhost_origin_aliases(self.args.frontend_url),
        )
        env["III_GC_PROXY_CORS_ORIGINS"] = ",".join(cors_origins)
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "up",
            "-d",
            "--build",
        ]
        self.run_subprocess(cmd, env=env, name="compose-up")

    def compose_down(self) -> None:
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "down",
            "--remove-orphans",
        ]
        self.run_subprocess(cmd, name="compose-down", check=False)

    def capture_compose_logs(self) -> None:
        cmd = [
            "docker",
            "compose",
            "-p",
            self.args.compose_project,
            "-f",
            self.args.compose_file,
            "logs",
            "--no-color",
        ]
        result = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, check=False)
        (self.artifacts / "compose.log").write_text(result.stdout + result.stderr, encoding="utf-8")

    def run_subprocess(self, cmd: list[str], *, name: str, env: dict[str, str] | None = None, check: bool = True) -> None:
        artifact = self.artifacts / f"{name}.log"
        print(f"{name}: running {' '.join(cmd)}", flush=True)
        with artifact.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                cmd,
                cwd=self.workspace,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            returncode = process.wait()
        if check and returncode != 0:
            raise SmokeFailure(f"{name} failed with exit {returncode}; see {artifact}")

    def write_summary(self) -> None:
        (self.artifacts / "summary.json").write_text(json.dumps(self.summary, indent=2, sort_keys=True), encoding="utf-8")
        latest = Path(self.args.artifacts_dir).resolve() / "latest"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(self.artifacts, target_is_directory=True)
        except OSError:
            pass


def default_browser_password() -> str:
    explicit = os.environ.get("III_RUNTIME_API_BROWSER_PASSWORD")
    if explicit:
        return explicit
    token_file = os.environ.get("III_RUNTIME_API_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="ascii").strip()
        except OSError:
            token = ""
        if token:
            return token
    return "dev-password"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    workspace = Path(__file__).resolve().parents[2]
    parser.add_argument("--workspace", default=str(workspace), help="Workspace root.")
    parser.add_argument("--runtime-url", default=os.environ.get("III_RUNTIME_API_URL", "http://127.0.0.1:8765"))
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get(
            "III_GC_PROXY_URL",
            f"http://127.0.0.1:{os.environ.get('III_GC_PROXY_PORT', '18780')}",
        ),
    )
    parser.add_argument(
        "--frontend-url",
        default=os.environ.get(
            "III_GC_FRONTEND_URL",
            f"http://127.0.0.1:{os.environ.get('III_GC_FRONTEND_PORT', '15174')}",
        ),
    )
    parser.add_argument("--password", default=default_browser_password())
    parser.add_argument(
        "--expected-profile",
        choices=("sim", "hil"),
        default=os.environ.get("III_GUI_V2_E2E_EXPECTED_PROFILE", "sim"),
        help="Runtime profile expected and requested by the smoke workflow.",
    )
    parser.add_argument("--artifacts-dir", default=os.environ.get("III_GUI_V2_E2E_ARTIFACTS", "log/gui-v2-sim-e2e-smoke"))
    parser.add_argument("--timeout-s", type=float, default=float(os.environ.get("III_GUI_V2_E2E_TIMEOUT_SEC", "60")))
    parser.add_argument(
        "--http-timeout-s",
        type=float,
        default=float(os.environ.get("III_GUI_V2_E2E_HTTP_TIMEOUT_SEC", "190")),
        help="Per-request timeout; defaults above the GC proxy's 180 s runtime-operation budget.",
    )
    parser.add_argument("--start-compose", action="store_true", help="Start the GC compose stack before running checks.")
    parser.add_argument("--keep-compose", action="store_true", help="Leave compose services running after the smoke.")
    parser.add_argument("--compose-file", default="src/III-Drone-GC/docker-compose.prod.yml")
    parser.add_argument("--compose-project", default=os.environ.get("III_GUI_V2_E2E_COMPOSE_PROJECT", "iii-gc-e2e-smoke"))
    parser.add_argument("--run-mutating-workflows", action="store_true", help="Run bench-safe runtime, payload, perception, configuration, rosbag, and operation commands.")
    parser.add_argument(
        "--rosbag-observation-s",
        type=float,
        default=5.0,
        help="Seconds to capture live messages before stopping the bench-safe rosbag smoke.",
    )
    parser.add_argument("--run-flight-commands", action="store_true", help="Run simulated arm, takeoff, hold, and land commands. Requires --run-mutating-workflows.")
    parser.add_argument("--run-inspection-cycle", action="store_true", help="Run the complete simulated inspection/recharge/resume/Hold/land acceptance cycle.")
    parser.add_argument("--capture-frontend", action="store_true", help="Capture an authenticated final GUI screenshot without running a flight cycle.")
    parser.add_argument("--overview-fixture", default="mid_corridor_taken_off_conductors_visible")
    parser.add_argument("--inspection-start-fixture", default="low_entry_side")
    parser.add_argument("--min-powerline-lines", type=int, default=4)
    parser.add_argument("--inspection-observation-s", type=float, default=20.0)
    parser.add_argument("--charging-observation-s", type=float, default=10.0)
    parser.add_argument("--inspection-translation-speed-m-s", type=float, default=0.5)
    parser.add_argument("--inspection-yaw-rate-rad-s", type=float, default=0.5)
    parser.add_argument("--initial-battery-pct", type=float, default=100.0)
    parser.add_argument("--fixture-settle-s", type=float, default=0.1)
    parser.add_argument("--fixture-guard-s", type=float, default=8.0)
    parser.add_argument("--overview-guard-s", type=float, default=60.0)
    parser.add_argument("--fixture-resolver", default="scripts/workspace/resolve_sim_fixture.py")
    parser.add_argument("--browser-binary", default=os.environ.get("BROWSER_BINARY", "chromium"))
    return parser.parse_args(argv)


def active_mission_mode(state: dict[str, Any]) -> str | None:
    for mode in state.get("modes", []):
        if mode.get("active"):
            return mode.get("mode_key")
    return state.get("owned_mode") if state.get("mission_active") else None


def build_request_id(run_id: str, step_index: int, name: str) -> str:
    """Build an idempotency key unique to one runner invocation."""
    return f"smoke-{run_id}-{step_index:02d}-{name}"


def frontend_session_payload(
    *,
    token: str,
    endpoint_id: str,
    runtime_name: str,
    runtime_id: str | None,
    system_id: str | None,
    profile: str | None,
) -> dict[str, Any]:
    return {
        "token": token,
        "endpointId": endpoint_id,
        "runtimeName": runtime_name,
        "runtimeId": runtime_id,
        "systemId": system_id,
        "profile": profile,
    }


def capture_authenticated_chromium(
    *,
    browser_binary: str,
    frontend_url: str,
    output: Path,
    session: dict[str, Any],
    timeout_s: float = 30.0,
) -> None:
    """Capture the hydrated operator workspace through Chrome DevTools."""
    try:
        import websocket
    except ImportError as exc:
        raise SmokeFailure("authenticated screenshots require websocket-client") from exc
    resolved_browser = shutil.which(browser_binary)
    if resolved_browser is None:
        resolved_browser = next(
            (candidate for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable") if (candidate := shutil.which(name))),
            None,
        )
    if resolved_browser is None:
        raise SmokeFailure(f"browser binary is unavailable: {browser_binary}")

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        debug_port = port_socket.getsockname()[1]

    with tempfile.TemporaryDirectory(prefix="iii-gui-v2-chromium-") as profile_dir:
        process = subprocess.Popen(
            [
                resolved_browser,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--window-size=1440,900",
                f"--remote-debugging-port={debug_port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_dir}",
                frontend_url,
            ],
            cwd=output.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        connection = None
        try:
            deadline = time.monotonic() + timeout_s
            page = None
            while time.monotonic() < deadline:
                try:
                    with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as response:
                        pages = json.load(response)
                    page = select_chromium_page(pages, frontend_url)
                    if page:
                        break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.2)
            if not page:
                raise SmokeFailure("Chromium DevTools page did not become ready")

            connection = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5)
            command_id = 0

            def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal command_id
                command_id += 1
                connection.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
                while True:
                    response = json.loads(connection.recv())
                    if response.get("id") == command_id:
                        if "error" in response:
                            raise SmokeFailure(f"Chromium DevTools {method} failed: {response['error']}")
                        return response.get("result", {})

            cdp("Runtime.enable")
            cdp("Page.enable")
            storage_script = (
                "sessionStorage.setItem('iii-gc-v2-session', "
                + json.dumps(json.dumps(session, separators=(",", ":")))
                + "); localStorage.setItem('iii-gc-v2-last-endpoint', "
                + json.dumps(session["endpointId"])
                + ");"
            )
            cdp("Page.addScriptToEvaluateOnNewDocument", {"source": storage_script})
            cdp("Page.reload", {"ignoreCache": True})

            hydrated = False
            hydration_deadline = time.monotonic() + timeout_s
            while time.monotonic() < hydration_deadline:
                time.sleep(0.3)
                result = cdp(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "Boolean(sessionStorage.getItem('iii-gc-v2-session')) && "
                            "Boolean(document.querySelector('.app-shell:not(.app-shell--login)')) && "
                            "Boolean(document.querySelector('[aria-label=\"Runtime session\"]')) && "
                            "Boolean(document.querySelector('[aria-label=\"Diagnostic dashboard\"], "
                            "[aria-label=\"Mission command and current state\"]')) && "
                            "Boolean(document.querySelector('.page-context[role=\"status\"]')) && "
                            "!['disconnected', 'connecting'].includes("
                            "document.querySelector('.page-context').textContent.trim().toLowerCase())"
                        ),
                        "returnByValue": True,
                    },
                )
                if result.get("result", {}).get("value") is True:
                    hydrated = True
                    break
            if not hydrated:
                diagnostic = cdp(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "JSON.stringify({url: location.href, "
                            "session: sessionStorage.getItem('iii-gc-v2-session'), "
                            "text: document.body.innerText})"
                        ),
                        "returnByValue": True,
                    },
                )
                output.with_suffix(".failed.txt").write_text(
                    str(diagnostic.get("result", {}).get("value") or diagnostic),
                    encoding="utf-8",
                )
                failed_screenshot = cdp("Page.captureScreenshot", {"format": "png", "fromSurface": True})
                output.with_suffix(".failed.png").write_bytes(base64.b64decode(failed_screenshot["data"]))
                raise SmokeFailure("authenticated frontend did not hydrate before screenshot")

            cdp(
                "Runtime.evaluate",
                {
                    "expression": (
                        "document.fonts.ready.then(() => new Promise(resolve => "
                        "requestAnimationFrame(() => requestAnimationFrame(resolve))))"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            time.sleep(0.5)
            screenshot = cdp("Page.captureScreenshot", {"format": "png", "fromSurface": True})
            output.write_bytes(base64.b64decode(screenshot["data"]))
        finally:
            if connection is not None:
                connection.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def select_chromium_page(pages: list[dict[str, Any]], frontend_url: str) -> dict[str, Any] | None:
    expected = urlsplit(frontend_url)
    for page in pages:
        if page.get("type") != "page":
            continue
        actual = urlsplit(str(page.get("url") or ""))
        if (actual.scheme, actual.hostname, actual.port) == (expected.scheme, expected.hostname, expected.port):
            return page
    return None


def mission_active(state: dict[str, Any]) -> bool:
    return state.get("mission_state") == "active" or state.get("latest", {}).get("mission_active") is True


def operation_mode_active(state: dict[str, Any]) -> bool:
    latest = state.get("latest", {})
    return state.get("status") == "custom_operation_active" or latest.get("control_owner") == "custom_operation"


def operation_started_or_finished(state: dict[str, Any], operation_id: str) -> bool:
    if state.get("active_operation_id") == operation_id:
        return True
    return any(event.get("operation_id") == operation_id for event in state.get("latest", {}).get("operation_events", []))


def operation_finished(state: dict[str, Any], operation_id: str | None = None) -> bool:
    latest = state.get("latest", {})
    active = state.get("active_operation_id") or latest.get("operation_active") is True
    if active:
        return False
    events = latest.get("operation_events", [])
    if not events:
        return False
    if operation_id is not None:
        events = [event for event in events if event.get("operation_id") == operation_id]
        if not events:
            return False
    event = events[-1]
    status = str(event.get("status", "")).lower()
    if status in {"failed", "rejected", "cancelled", "aborted"}:
        raise SmokeFailure(f"custom operation ended in {status}: {event}")
    event_type = str(event.get("event_type", "")).lower()
    result = event.get("payload", {}).get("result")
    if event_type == "rejected":
        raise SmokeFailure(f"custom operation was rejected: {event}")
    if event_type == "result" and isinstance(result, dict):
        if result.get("success") is False:
            reason = result.get("error") or result.get("status") or "unknown failure"
            raise SmokeFailure(f"custom operation failed: {reason}")
        return result.get("success") is True
    return status in {"succeeded", "completed", "success"}


def mapper_active(state: dict[str, Any]) -> bool:
    latest = state.get("latest", {})
    label = str(state.get("pl_mapper_state") or latest.get("pl_mapper_state") or "").lower()
    return any(value in label for value in ("running", "active", "started"))


def live_line_count(state: dict[str, Any]) -> int:
    latest = state.get("latest", {})
    for value in (state.get("live_line_count"), latest.get("live_powerline_line_count"), latest.get("powerline_count")):
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def visible_live_line_count(state: dict[str, Any]) -> int:
    lines = state.get("live_geometry", {}).get("lines", [])
    if not isinstance(lines, list):
        return 0
    return sum(
        1
        for line in lines
        if isinstance(line, dict) and line.get("in_field_of_view") is True
    )


def overviews_complete(state: dict[str, Any]) -> bool:
    pylon = state.get("pylon_overview", {})
    powerline_valid = state.get("valid") is True or state.get("stored_overview_valid") is True
    return powerline_valid and pylon.get("valid") is True and pylon.get("pylon_count") == 2


def configuration_manifest_available(state: dict[str, Any]) -> bool:
    manifest = state.get("latest", {}).get("manifest", {})
    return (
        state.get("source_availability") == "available"
        and manifest.get("status", {}).get("configuration_server_available") is True
        and bool(manifest.get("nodes"))
    )


def configuration_parameter(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    for node in state.get("latest", {}).get("manifest", {}).get("nodes", []):
        for group in node.get("groups", []):
            for parameter in group.get("parameters", []):
                if parameter.get("name") == name:
                    return parameter
    return None


def configuration_revision(state: dict[str, Any]) -> int:
    revision = state.get("latest", {}).get("manifest", {}).get("status", {}).get("tuning_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise SmokeFailure("configuration manifest has no valid tuning revision")
    return revision


def numeric_values_match(actual: Any, expected: float) -> bool:
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and abs(float(actual) - expected) < 1.0e-6


def nav_is(state: dict[str, Any], expected: str) -> bool:
    return expected.lower() in str(state.get("nav_state") or state.get("flight_mode") or "").lower()


def vehicle_landed_and_disarmed(state: dict[str, Any]) -> bool:
    return (
        telemetry_field_matches(state, "in_air", False)
        and telemetry_field_matches(state, "armed", False)
        and state.get("in_air") is False
        and state.get("armed") is False
    )


def telemetry_field_matches(state: dict[str, Any], field_name: str, expected: Any) -> bool:
    evidence = state.get("telemetry_fields", {}).get(field_name, {})
    return (
        evidence.get("value") == expected
        and evidence.get("freshness") == "fresh"
        and evidence.get("source_availability") == "available"
        and evidence.get("disagreement") is not True
    )


def _battery_remaining(state: dict[str, Any]) -> float:
    value = state.get("battery_remaining")
    if not isinstance(value, (int, float)):
        raise SmokeFailure("battery remaining telemetry is unavailable")
    return float(value)


def simulated_battery_reset_visible(state: dict[str, Any], requested_pct: float) -> bool:
    """Confirm reset propagation despite intentionally accelerated SITL discharge."""
    minimum_pct = max(0.0, float(requested_pct) - 15.0)
    evidence = state.get("telemetry_fields", {}).get("battery_remaining", {})
    return (
        _battery_remaining(state) * 100.0 >= minimum_pct
        and evidence.get("freshness") == "fresh"
        and evidence.get("source_availability") == "available"
    )


def acknowledged_hil_battery_reset(payload: dict[str, Any]) -> bool:
    """Accept PX4's token proof when onboard ROS owns telemetry observation."""
    data = payload.get("data") or {}
    reset_token = data.get("reset_token")
    acknowledgement_token = data.get("acknowledgement_token")
    target = data.get("target_remaining_pct")
    initial = (data.get("initial_percentage_parameter") or {}).get("param_value")
    return (
        payload.get("success") is False
        and payload.get("message")
        == "battery reset was acknowledged but no battery status was observed"
        and isinstance(reset_token, int)
        and reset_token == acknowledgement_token
        and isinstance(target, (int, float))
        and isinstance(initial, (int, float))
        and abs(float(target) - float(initial)) <= 0.01
        and data.get("battery_after") is None
        and data.get("observed_remaining_pct") is None
    )


def mission_modes_selectable(vehicle: dict[str, Any], mission: dict[str, Any]) -> bool:
    """Require every registered mission mode to remain selectable in PX4."""
    mask = (
        vehicle.get("latest", {})
        .get("ros_uxrce", {})
        .get("raw", {})
        .get("vehicle_status", {})
        .get("can_set_nav_states_mask")
    )
    mode_ids = [
        mode.get("mode_id")
        for mode in mission.get("modes", [])
        if mode.get("registered") is True
    ]
    return (
        isinstance(mask, int)
        and bool(mode_ids)
        and all(isinstance(mode_id, int) and 0 <= mode_id < 32 and mask & (1 << mode_id) for mode_id in mode_ids)
    )


def assert_battery_depleted(before: dict[str, Any], after: dict[str, Any]) -> None:
    if _battery_remaining(after) >= _battery_remaining(before) - 0.001:
        raise SmokeFailure("simulated battery did not measurably deplete during inspection")


def assert_battery_charged(before: dict[str, Any], after: dict[str, Any]) -> None:
    if _battery_remaining(after) <= _battery_remaining(before) + 0.001:
        raise SmokeFailure("battery did not measurably charge while cable-charging mode was active")


def battery_increased(before: dict[str, Any], after: dict[str, Any], *, minimum_delta: float = 0.002) -> bool:
    """Return true after a small but telemetry-significant charge increase."""
    return _battery_remaining(after) >= _battery_remaining(before) + minimum_delta


def port_from_url(url: str | None, *, default: str) -> str:
    if not url:
        return default
    parsed = urlsplit(url)
    if parsed.port is not None:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    if parsed.scheme == "http":
        return "80"
    return default


def localhost_origin_aliases(url: str | None) -> list[str]:
    if not url:
        return ["http://127.0.0.1:5174", "http://localhost:5174"]
    parsed = urlsplit(url)
    scheme = parsed.scheme or "http"
    port = port_from_url(url, default="5174")
    hostname = parsed.hostname or "127.0.0.1"
    hosts = [hostname]
    if hostname == "127.0.0.1":
        hosts.append("localhost")
    elif hostname == "localhost":
        hosts.append("127.0.0.1")
    origins: list[str] = []
    for host in hosts:
        netloc = f"{host}:{port}"
        origin = urlunsplit((scheme, netloc, "", "", ""))
        if origin not in origins:
            origins.append(origin)
    return origins


def merge_csv_values(existing: str, required: list[str]) -> list[str]:
    values: list[str] = []
    for value in [*(item.strip() for item in existing.split(",") if item.strip()), *required]:
        if value not in values:
            values.append(value)
    return values


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    return SmokeRunner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
