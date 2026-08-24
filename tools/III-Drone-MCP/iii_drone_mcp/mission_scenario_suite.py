from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback
from typing import Any

import rclpy
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from iii_drone_interfaces.msg import MissionModeStatus, StringStamped
from iii_drone_mcp.agent_tools import DroneAgentTools, ToolResult
from iii_drone_mcp.simulation_observation import load_geometry


DEFAULT_SCENARIOS = [
    "low_inside_corridor",
    "high_inside_corridor",
    "low_entry_side",
    "high_entry_side",
    "above_entry_side",
    "above_mid",
    "above_opposite_side",
    "high_opposite_side",
    "low_opposite_side",
]

CRITICAL_NODES = [
    "charger_gripper",
    "custom_operation",
    "maneuver_controller",
    "mission_executor",
    "pl_mapper",
    "powerline_overview_provider",
    "rosbag_recorder",
    "tf",
    "trajectory_generator",
]


class SuiteFailure(RuntimeError):
    pass


def mission_attempt_completed(
    *,
    saw_reach_active: bool,
    saw_leave_success: bool,
    mission_status: dict[str, Any],
) -> bool:
    return (
        saw_reach_active
        and saw_leave_success
        and not bool(mission_status.get("mission_active", True))
    )


@dataclass
class ScenarioAttempt:
    scenario_id: str
    round_index: int
    attempt_index: int
    artifact_dir: Path
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    success: bool = False
    failure: str | None = None
    workflow_id: str | None = None
    environment_started_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class MissionScenarioSuite:
    def __init__(
        self,
        *,
        workspace_root: Path,
        artifact_root: Path,
        rounds_required: int,
        scenarios: list[str],
        headless: bool,
        restart_per_scenario: bool,
        max_attempts_per_scenario: int,
        mission_timeout_sec: float,
        keep_environment: bool,
        fail_fast: bool,
    ) -> None:
        self.workspace_root = workspace_root
        self.artifact_root = artifact_root
        self.rounds_required = rounds_required
        self.scenarios = scenarios
        self.headless = headless
        self.restart_per_scenario = restart_per_scenario
        self.max_attempts_per_scenario = max_attempts_per_scenario
        self.mission_timeout_sec = mission_timeout_sec
        self.keep_environment = keep_environment
        self.fail_fast = fail_fast
        self.tools = DroneAgentTools(
            artifact_dir=artifact_root / "mcp",
        )
        self.summary: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "workspace_root": str(workspace_root),
            "artifact_root": str(artifact_root),
            "rounds_required": rounds_required,
            "scenarios": scenarios,
            "headless": headless,
            "restart_per_scenario": restart_per_scenario,
            "attempts": [],
            "consecutive_successful_rounds": 0,
            "success": False,
        }

    def close(self) -> None:
        self.tools.close()

    def run(self) -> None:
        self._validate_scenarios()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        consecutive_rounds = 0
        round_index = 1
        while consecutive_rounds < self.rounds_required:
            self._log(f"Starting round {round_index}; consecutive successful rounds={consecutive_rounds}")
            round_success = True
            for scenario_id in self.scenarios:
                scenario_success = self._run_scenario_until_success(round_index, scenario_id)
                if not scenario_success:
                    round_success = False
                    break
            if round_success:
                consecutive_rounds += 1
                self._log(f"Round {round_index} passed; consecutive successful rounds={consecutive_rounds}")
            else:
                if self.fail_fast:
                    raise SuiteFailure(f"round {round_index} failed and fail-fast is enabled")
                consecutive_rounds = 0
                self._log(f"Round {round_index} failed; consecutive success counter reset")
            self.summary["consecutive_successful_rounds"] = consecutive_rounds
            self._write_summary()
            round_index += 1
        self.summary["success"] = True
        self.summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._write_summary()

    def _validate_scenarios(self) -> None:
        geometry = load_geometry(self.workspace_root)
        available = {item.get("id") for item in geometry.data.get("mission_start_positions", [])}
        missing = [scenario for scenario in self.scenarios if scenario not in available]
        if missing:
            raise SuiteFailure(f"missing mission_start_positions: {', '.join(missing)}")

    def _run_scenario_until_success(self, round_index: int, scenario_id: str) -> bool:
        for attempt_index in range(1, self.max_attempts_per_scenario + 1):
            artifact_dir = self.artifact_root / f"round_{round_index:02d}" / scenario_id / f"attempt_{attempt_index:02d}"
            attempt = ScenarioAttempt(
                scenario_id=scenario_id,
                round_index=round_index,
                attempt_index=attempt_index,
                artifact_dir=artifact_dir,
            )
            self.summary["attempts"].append(self._attempt_dict(attempt))
            self._write_summary()
            try:
                self._run_single_attempt(attempt)
                attempt.success = True
                attempt.finished_at = datetime.now(timezone.utc).isoformat()
                self._record_attempt(attempt)
                self._log(f"PASS round={round_index} scenario={scenario_id} attempt={attempt_index}")
                return True
            except Exception as exc:
                attempt.success = False
                attempt.failure = str(exc)
                attempt.finished_at = datetime.now(timezone.utc).isoformat()
                attempt.events.append({"event": "exception", "message": str(exc), "traceback": traceback.format_exc()})
                self._collect_failure_artifacts(attempt)
                self._record_attempt(attempt)
                self._log(f"FAIL round={round_index} scenario={scenario_id} attempt={attempt_index}: {exc}")
                self._recover_after_failure()
        return False

    def _run_single_attempt(self, attempt: ScenarioAttempt) -> None:
        attempt.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.tools.artifact_dir = attempt.artifact_dir / "mcp"
        self.tools.artifact_dir.mkdir(parents=True, exist_ok=True)

        if self.restart_per_scenario:
            self._reset_environment(attempt)
        else:
            self._ensure_environment_started(attempt)

        self._assert_tool_success(attempt, "preflight_px4_status", self.tools.px4("status", timeout_sec=10.0))
        self._assert_tool_success(attempt, "preflight_px4_safety", self.tools.px4_safety(timeout_sec=5.0))
        self._assert_tool_success(
            attempt,
            "preflight_node_safety",
            self.tools.critical_node_safety(
                entities=CRITICAL_NODES,
                since_iso=attempt.environment_started_at,
                timeout_sec=5.0,
            ),
        )

        workflow_id = f"suite_r{attempt.round_index:02d}_{attempt.scenario_id}_a{attempt.attempt_index:02d}_{int(time.time())}"
        attempt.workflow_id = workflow_id
        workflow = self.tools.start_mission_deploy_workflow(
            workflow_id=workflow_id,
            artifact_dir=str(attempt.artifact_dir / "mission_deploy"),
            mission_start_position_id=attempt.scenario_id,
            force_update_overview=True,
            min_powerline_lines=4,
            mission_mode="reach_cable",
            position_tolerance_m=0.2,
            position_settle_sec=1.0,
            fly_send_timeout_sec=30.0,
            fly_wait_timeout_sec=240.0,
            position_timeout_sec=60.0,
            px4_timeout_sec=30.0,
            custom_mode_timeout_sec=30.0,
            overview_timeout_s=20,
            overview_service_timeout_sec=30.0,
            overview_query_timeout_sec=30.0,
            overview_store_attempts=3,
            overview_retry_delay_sec=1.0,
            powerline_timeout_sec=30.0,
        )
        self._assert_tool_success(attempt, "workflow_start", workflow)
        self._wait_workflow_complete(attempt)
        self._wait_mission_success(attempt)
        self._assert_tool_success(attempt, "post_px4_safety", self.tools.px4_safety(timeout_sec=5.0))
        self._assert_tool_success(
            attempt,
            "post_node_safety",
            self.tools.critical_node_safety(
                entities=CRITICAL_NODES,
                since_iso=attempt.environment_started_at,
                timeout_sec=5.0,
            ),
        )
        self._assert_no_active_operations(attempt)
        self._assert_no_active_rosbag(attempt)

    def _reset_environment(self, attempt: ScenarioAttempt) -> None:
        self._call_allow_failure(attempt, "operation_cancel_all", self.tools.cancel_all_operation_goals(timeout_sec=5.0, reason="scenario reset"))
        self._call_allow_failure(attempt, "rosbag_stop", self.tools.rosbag_record(command="stop", timeout_sec=10.0))
        self._call_allow_failure(attempt, "system_shutdown", self.tools.system("shutdown", timeout_sec=90.0))
        self._assert_tool_success(attempt, "system_daemon_restart", self.tools.system("daemon_restart", timeout_sec=30.0))
        self._assert_tool_success(
            attempt,
            "simulation_restart",
            self.tools.simulation("restart", timeout_sec=180.0, headless=self.headless, wait_ready=True, ready_timeout_sec=180.0),
        )
        self._assert_tool_success(attempt, "system_boot", self.tools.system("boot", timeout_sec=120.0))
        self._assert_tool_success(attempt, "system_start", self.tools.system("start", timeout_sec=300.0))
        attempt.environment_started_at = datetime.now(timezone.utc).isoformat()

    def _ensure_environment_started(self, attempt: ScenarioAttempt) -> None:
        self._assert_tool_success(
            attempt,
            "simulation_start",
            self.tools.simulation("start", timeout_sec=180.0, headless=self.headless, wait_ready=True, ready_timeout_sec=180.0),
        )
        self._assert_tool_success(attempt, "system_boot", self.tools.system("boot", timeout_sec=120.0))
        self._assert_tool_success(attempt, "system_start", self.tools.system("start", timeout_sec=300.0))
        attempt.environment_started_at = datetime.now(timezone.utc).isoformat()

    def _wait_workflow_complete(self, attempt: ScenarioAttempt) -> None:
        if not attempt.workflow_id:
            raise SuiteFailure("workflow id missing")
        deadline = time.monotonic() + 360.0
        while time.monotonic() < deadline:
            status = self.tools.mission_deploy_workflow_status(workflow_id=attempt.workflow_id, tail_log_lines=80)
            self._record_event(attempt, "workflow_status", status)
            state = (status.data or {}).get("state") if isinstance(status.data, dict) else None
            if state == "succeeded":
                return
            if state in {"failed", "cancelled", "unknown"}:
                raise SuiteFailure(f"mission deploy workflow {state}: {status.message}")
            time.sleep(2.0)
        raise SuiteFailure("mission deploy workflow timed out")

    def _wait_mission_success(self, attempt: ScenarioAttempt) -> None:
        deadline = time.monotonic() + self.mission_timeout_sec
        last_mode: dict[str, Any] = {}
        last_mission: dict[str, Any] = {}
        latest_mission: dict[str, Any] = {"message": None}
        latest_modes: dict[str, Any] = {
            "reach_cable": None,
            "cable_charging": None,
            "leave_cable": None,
        }
        saw_reach_active = False
        saw_leave_success = False
        qos_profile = self._transient_local_qos()
        subscriptions = [
            self.tools.node.create_subscription(
                MissionModeStatus,
                "/mission/status",
                lambda message: latest_mission.__setitem__("message", message),
                qos_profile,
            ),
            self.tools.node.create_subscription(
                StringStamped,
                "/mission/modes/reach_cable/status",
                lambda message: latest_modes.__setitem__("reach_cable", self._parse_mode_status_message(message)),
                qos_profile,
            ),
            self.tools.node.create_subscription(
                StringStamped,
                "/mission/modes/cable_charging/status",
                lambda message: latest_modes.__setitem__("cable_charging", self._parse_mode_status_message(message)),
                qos_profile,
            ),
            self.tools.node.create_subscription(
                StringStamped,
                "/mission/modes/leave_cable/status",
                lambda message: latest_modes.__setitem__("leave_cable", self._parse_mode_status_message(message)),
                qos_profile,
            ),
        ]
        try:
            while time.monotonic() < deadline:
                safety = self.tools.px4_safety(timeout_sec=2.0)
                self._record_event(attempt, "mission_px4_safety_sample", safety, compact=True)
                if not safety.success:
                    raise SuiteFailure(f"PX4 safety failed during mission: {safety.message}")

                rclpy.spin_once(self.tools.node, timeout_sec=0.1)
                mission_msg = latest_mission["message"]
                if mission_msg is not None:
                    last_mission = self.tools._message_to_plain_dict(mission_msg)

                mode_statuses = dict(latest_modes)
                last_mode = {key: value for key, value in mode_statuses.items() if value}
                if mode_statuses["reach_cable"] and mode_statuses["reach_cable"].get("active"):
                    saw_reach_active = True
                for key, value in mode_statuses.items():
                    if value and value.get("tree_finished") and not value.get("tree_success"):
                        raise SuiteFailure(f"mission mode {key} finished unsuccessfully: {value}")
                leave_status = mode_statuses["leave_cable"]
                if (
                    leave_status
                    and leave_status.get("tree_finished")
                    and leave_status.get("tree_success")
                ):
                    saw_leave_success = True
                if saw_leave_success:
                    try:
                        final_status = self.tools.mission_status(timeout_sec=2.0)
                    except Exception as exc:
                        final_status = ToolResult(
                            False,
                            {"error": str(exc)},
                            "mission terminal status query failed; retrying",
                        )
                    self._record_event(attempt, "mission_terminal_status", final_status, compact=True)
                    if final_status.success and isinstance(final_status.data, dict):
                        last_mission = dict(final_status.data)
                if mission_attempt_completed(
                    saw_reach_active=saw_reach_active,
                    saw_leave_success=saw_leave_success,
                    mission_status=last_mission,
                ):
                    self._record_event(
                        attempt,
                        "mission_success",
                        ToolResult(True, {"mission": last_mission, "modes": last_mode}, "mission completed"),
                    )
                    return
                time.sleep(1.0)
        finally:
            for subscription in subscriptions:
                self.tools.node.destroy_subscription(subscription)
        raise SuiteFailure(f"mission did not complete before timeout; mission={last_mission}; modes={last_mode}")

    def _read_mode_status(self, topic: str) -> dict[str, Any] | None:
        msg = self._take_transient_message(topic, StringStamped, 0.2, required=False)
        return self._parse_mode_status_message(msg)

    @staticmethod
    def _parse_mode_status_message(msg: StringStamped | None) -> dict[str, Any] | None:
        if msg is None or not msg.data:
            return None
        try:
            return json.loads(msg.data)
        except json.JSONDecodeError:
            return {"raw": msg.data}

    @staticmethod
    def _transient_local_qos() -> QoSProfile:
        return QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

    def _take_message(self, topic: str, message_type: Any, timeout_sec: float, *, required: bool) -> Any:
        return self.tools._take_message(topic, message_type, timeout_sec, required=required)

    def _take_transient_message(self, topic: str, message_type: Any, timeout_sec: float, *, required: bool) -> Any:
        return self.tools._take_transient_local_message(topic, message_type, timeout_sec, required=required)

    def _assert_no_active_operations(self, attempt: ScenarioAttempt) -> None:
        active = self.tools.active_operation_goal()
        self._record_event(attempt, "operation_active", active)
        if not active.success:
            raise SuiteFailure(f"operation activity check failed: {active.message}")
        data = active.data if isinstance(active.data, dict) else {}
        if data.get("active"):
            raise SuiteFailure(f"operation goal still active: {data}")

    def _assert_no_active_rosbag(self, attempt: ScenarioAttempt) -> None:
        status = self.tools.rosbag_record(command="status", timeout_sec=5.0)
        self._record_event(attempt, "rosbag_status", status)
        if not status.success:
            raise SuiteFailure(f"rosbag status failed: {status.message}")
        data = status.data if isinstance(status.data, dict) else {}
        if data.get("active") or data.get("recording"):
            raise SuiteFailure(f"rosbag still active: {data}")

    def _recover_after_failure(self) -> None:
        try:
            self.tools.cancel_all_operation_goals(timeout_sec=5.0, reason="scenario failure")
        except Exception:
            pass
        try:
            self.tools.rosbag_record(command="stop", timeout_sec=10.0)
        except Exception:
            pass
        try:
            self.tools.operation_safety_stop(mode="hold", force_clear_queue=True, disarm_after_land=False, timeout_sec=15.0)
        except Exception:
            pass

    def _collect_failure_artifacts(self, attempt: ScenarioAttempt) -> None:
        diagnostics = attempt.artifact_dir / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        system_status = self.tools.system("status", timeout_sec=10.0)
        self._record_event(attempt, "system_status", system_status, compact=True)
        system_booted = self.tools._system_status_booted(system_status)

        if system_booted:
            for entity in CRITICAL_NODES:
                try:
                    result = self.tools.logs(
                        command="capture",
                        entity_id=entity,
                        history=True,
                        save=True,
                        tail_lines=3000,
                        timeout_sec=8.0,
                    )
                    self._record_event(attempt, f"log_{entity}", result, compact=True)
                except Exception as exc:
                    attempt.events.append({"event": f"log_{entity}_failed", "message": str(exc)})
        else:
            self._record_event(
                attempt,
                "node_logs_skipped",
                ToolResult(True, {"system_booted": False}, "system graph was not booted"),
                compact=True,
            )

        calls = [
            ("px4_safety", lambda: self.tools.px4_safety(timeout_sec=5.0)),
            ("simulation_status", lambda: self.tools.simulation("status", timeout_sec=10.0)),
        ]
        if system_booted:
            calls.append(
                ("critical_nodes", lambda: self.tools.critical_node_safety(entities=CRITICAL_NODES, timeout_sec=8.0))
            )
        for name, call in calls:
            try:
                self._record_event(attempt, name, call(), compact=True)
            except Exception as exc:
                attempt.events.append({"event": f"{name}_failed", "message": str(exc)})

    def _assert_tool_success(self, attempt: ScenarioAttempt, event: str, result: ToolResult) -> None:
        self._record_event(attempt, event, result)
        if not result.success:
            raise SuiteFailure(f"{event} failed: {result.message}")

    def _call_allow_failure(self, attempt: ScenarioAttempt, event: str, result: ToolResult) -> None:
        self._record_event(attempt, event, result, compact=True)

    def _record_event(self, attempt: ScenarioAttempt, event: str, result: ToolResult, *, compact: bool = False) -> None:
        data = result.data
        if compact and isinstance(data, dict):
            data = {key: data.get(key) for key in ("success", "verdict", "derived", "message", "state", "returncode", "failures") if key in data}
        attempt.events.append(
            {
                "event": event,
                "success": bool(result.success),
                "message": result.message,
                "data": data,
                "time": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _record_attempt(self, attempt: ScenarioAttempt) -> None:
        path = attempt.artifact_dir / "attempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._attempt_dict(attempt), indent=2, default=str), encoding="utf-8")
        self.summary["attempts"][-1] = self._attempt_dict(attempt)
        self._write_summary()

    def _attempt_dict(self, attempt: ScenarioAttempt) -> dict[str, Any]:
        return {
            "scenario_id": attempt.scenario_id,
            "round_index": attempt.round_index,
            "attempt_index": attempt.attempt_index,
            "artifact_dir": str(attempt.artifact_dir),
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
            "success": attempt.success,
            "failure": attempt.failure,
            "workflow_id": attempt.workflow_id,
            "environment_started_at": attempt.environment_started_at,
            "events": attempt.events,
        }

    def _write_summary(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        (self.artifact_root / "summary.json").write_text(json.dumps(self.summary, indent=2, default=str), encoding="utf-8")

    def _log(self, message: str) -> None:
        line = f"[{datetime.now(timezone.utc).isoformat()}] {message}"
        print(line, flush=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with (self.artifact_root / "suite.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _scenario_ids_from_geometry(workspace_root: Path) -> list[str]:
    geometry = load_geometry(workspace_root)
    ids = [
        str(item.get("id"))
        for item in geometry.data.get("mission_start_positions", [])
        if "mission_start_scenario" in (item.get("intended_use") or [])
    ]
    preferred = [scenario for scenario in DEFAULT_SCENARIOS if scenario in ids]
    extras = sorted(scenario for scenario in ids if scenario not in preferred)
    return preferred + extras


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the III-Drone mission-start scenario suite until N clean rounds pass.")
    parser.add_argument("--workspace-root", default=os.environ.get("III_DRONE_WORKSPACE_ROOT", "/home/iii/ws"))
    parser.add_argument("--artifact-root", default=None)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--scenario", action="append", help="Scenario id to run. Can be repeated; defaults to mission_start_scenario fixtures.")
    parser.add_argument("--headless", action="store_true", help="Run Gazebo/PX4 backend without GUI/QGC.")
    parser.add_argument("--no-restart-per-scenario", action="store_true", help="Reuse the environment between scenarios.")
    parser.add_argument("--max-attempts-per-scenario", type=int, default=3)
    parser.add_argument("--mission-timeout-sec", type=float, default=540.0)
    parser.add_argument("--keep-environment", action="store_true")
    parser.add_argument("--fail-fast", action="store_true", help="Exit after the first failed round instead of retrying forever.")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).resolve()
    run_id = time.strftime("%Y%m%d_%H%M%S")
    artifact_root = Path(args.artifact_root or Path("/tmp/iii_drone/mission_scenario_suite") / run_id).resolve()
    if args.clean and artifact_root.exists():
        shutil.rmtree(artifact_root)
    scenarios = args.scenario or _scenario_ids_from_geometry(workspace_root)
    if not scenarios:
        raise SystemExit("no scenarios selected")

    suite = MissionScenarioSuite(
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        rounds_required=args.rounds,
        scenarios=scenarios,
        headless=bool(args.headless),
        restart_per_scenario=not bool(args.no_restart_per_scenario),
        max_attempts_per_scenario=args.max_attempts_per_scenario,
        mission_timeout_sec=args.mission_timeout_sec,
        keep_environment=bool(args.keep_environment),
        fail_fast=bool(args.fail_fast),
    )
    try:
        suite.run()
    except Exception as exc:
        suite.summary["success"] = False
        suite.summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        suite.summary["fatal_error"] = str(exc)
        suite.summary["fatal_traceback"] = traceback.format_exc()
        suite._write_summary()
        if not args.keep_environment:
            suite._recover_after_failure()
        print(f"FAIL {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    finally:
        suite.close()
    print(f"Artifacts: {artifact_root}")


if __name__ == "__main__":
    main()
