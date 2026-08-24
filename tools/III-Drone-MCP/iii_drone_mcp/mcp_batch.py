from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import signal
import sys
import traceback
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

if os.environ.get("III_DRONE_MCP_KEEP_RMW") != "1":
    os.environ["RMW_IMPLEMENTATION"] = os.environ.get("III_DRONE_MCP_RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
    os.environ["FASTDDS_BUILTIN_TRANSPORTS"] = os.environ.get("III_DRONE_MCP_FASTDDS_BUILTIN_TRANSPORTS", "UDPv4")


def _load_calls(path: str | None) -> list[dict[str, Any]]:
    raw = sys.stdin.read() if path in {None, "-"} else Path(path).read_text()
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise SystemExit("batch input must be a JSON list")
    for index, item in enumerate(parsed):
        if not isinstance(item, dict) or "tool" not in item:
            raise SystemExit(f"batch item {index} must be an object with a tool field")
    return parsed


def main() -> None:
    from iii_drone_mcp.agent_tools import DroneAgentTools
    from iii_drone_mcp.mcp_call import tools_as_specs
    from iii_drone_mcp.mcp_server import _reexec_as_runtime_user_if_needed

    _reexec_as_runtime_user_if_needed()

    parser = argparse.ArgumentParser(description="Run multiple III-Drone MCP tool calls in one process")
    parser.add_argument("path", nargs="?", help="JSON file containing a list of calls; reads stdin when omitted")
    parser.add_argument("--artifact-dir", default=os.environ.get("III_DRONE_MCP_ARTIFACT_DIR", "/tmp/iii_drone/iii_drone_agent"))
    parser.add_argument("--px4-system-address", default="udpin://0.0.0.0:14540")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--per-call-timeout-sec", type=float, default=300.0)
    parser.add_argument("--log-stderr", action="store_true", help="Keep middleware logs on stderr instead of writing them to an artifact log")
    parser.add_argument("--strict-safety", action="store_true", help="Write strict PX4/node safety verdicts and return nonzero if invariants fail")
    parser.add_argument("--always-run-cleanup", action="store_true", help="After an early failure, still run remaining cleanup-tagged or known cleanup calls")
    args = parser.parse_args()

    stderr_path: Path | None = None
    if not args.log_stderr:
        stderr_path = _redirect_stderr(args.artifact_dir, "mcp_batch_stderr.log")

    tools = DroneAgentTools(
        artifact_dir=args.artifact_dir,
        px4_system_address=args.px4_system_address,
    )
    outputs: list[dict[str, Any]] = []
    variables: dict[str, Any] = {}
    safety_monitor = _SafetyMonitor(tools, Path(args.artifact_dir), enabled=args.strict_safety)
    progress_path = Path(args.artifact_dir) / "mcp_batch_progress.jsonl"
    exit_code = 0
    try:
        specs = tools_as_specs(tools)
        calls = _load_calls(args.path)
        executed_indices: set[int] = set()

        def run_call(index: int, call: dict[str, Any], *, cleanup_after_failure: bool = False) -> dict[str, Any]:
            tool_name = str(call["tool"])
            arguments = _interpolate(call.get("arguments") or {}, variables)
            spec = specs.get(tool_name)
            progress_event = {"event": "start", "index": index, "tool": tool_name}
            if cleanup_after_failure:
                progress_event["cleanup_after_failure"] = True
            _append_progress(progress_path, progress_event)
            if spec is None:
                result = {"index": index, "tool": tool_name, "success": False, "message": f"unknown tool: {tool_name}"}
            else:
                try:
                    hard_timeout_sec = _hard_timeout_sec(arguments, args.per_call_timeout_sec)
                    with _call_timeout(hard_timeout_sec):
                        tool_result = spec(arguments)
                    result = {
                        "index": index,
                        "tool": tool_name,
                        "success": bool(tool_result.success),
                        "message": tool_result.message,
                        "data": tool_result.data,
                    }
                except Exception as exc:
                    result = {
                        "index": index,
                        "tool": tool_name,
                        "success": False,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
            if "expect_success" in call:
                actual_success = bool(result["success"])
                expected_success = bool(call["expect_success"])
                result["tool_success"] = actual_success
                result["expected_success"] = expected_success
                result["success"] = actual_success == expected_success
                result["expectation_matched"] = bool(result["success"])
                if not result["success"]:
                    result["message"] = (
                        f"expected tool success={expected_success} but got success={actual_success}: "
                        f"{result.get('message', '')}"
                    )
            if cleanup_after_failure:
                result["cleanup_after_failure"] = True
            outputs.append(result)
            safety_monitor.after_call(index, tool_name, arguments, result)
            for variable_name, field_path in (call.get("save") or {}).items():
                variables[str(variable_name)] = _get_field_path(result, str(field_path))
            finish_event = {
                "event": "finish",
                "index": index,
                "tool": tool_name,
                "success": bool(result["success"]),
                "message": result.get("message", ""),
            }
            if cleanup_after_failure:
                finish_event["cleanup_after_failure"] = True
            _append_progress(progress_path, finish_event)
            executed_indices.add(index)
            return result

        failure_index: int | None = None
        for index, call in enumerate(calls):
            result = run_call(index, call)
            if not result["success"]:
                exit_code = 1
                if not args.continue_on_error:
                    failure_index = index
                    break

        should_run_cleanup = (args.always_run_cleanup or args.strict_safety) and not args.continue_on_error and failure_index is not None
        if should_run_cleanup:
            for index, call in enumerate(calls):
                if index in executed_indices or index <= failure_index:
                    continue
                arguments = _interpolate(call.get("arguments") or {}, variables)
                if _is_cleanup_call(str(call["tool"]), arguments) or bool(call.get("cleanup")) or bool(call.get("always_run")):
                    result = run_call(index, call, cleanup_after_failure=True)
                    if not result["success"]:
                        exit_code = 1
        safety_summary = safety_monitor.finalize()
        stderr_summary = _write_stderr_summary(Path(args.artifact_dir), stderr_path)
        if safety_summary is not None and stderr_summary is not None:
            artifact_path = stderr_summary.get("artifact_path")
            if artifact_path and artifact_path not in safety_summary["artifact_paths"]:
                safety_summary["artifact_paths"].append(artifact_path)
            safety_summary["stderr_summary"] = stderr_summary
            _write_json(Path(args.artifact_dir) / "mcp_batch_safety_summary.json", safety_summary)
        if safety_summary is not None:
            outputs.append(
                {
                    "index": len(outputs),
                    "tool": "safety.summary",
                    "success": bool(safety_summary["success"]),
                    "message": safety_summary["verdict"],
                    "data": safety_summary,
                }
            )
            if not safety_summary["success"]:
                exit_code = 1
        print(json.dumps(outputs, indent=2, default=str))
        _write_json(Path(args.artifact_dir) / "mcp_batch_variables.json", variables)
    finally:
        tools.close()
    raise SystemExit(exit_code)


class _SafetyMonitor:
    _CRITICAL_ENTITIES = ("maneuver_controller", "mission_executor")
    _NODE_CRASH_RE = re.compile(
        r"RUN END: entity=(?P<entity>\\S+) .*? returncode=(?P<returncode>-?\\d+) time=(?P<time>\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}) \\+0000"
    )
    _LOG_TIMESTAMP_RE = re.compile(r"^\[[A-Z]+\]\s+\[(?P<timestamp>\d+(?:\.\d+)?)\]")
    _MISSION_LOG_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
        ("mission_mode_failure", "mission_executor", re.compile(r"Mode\s+.+?\s+failed with result", re.IGNORECASE)),
        ("mission_action_rejected", "mission_executor", re.compile(r"Goal was rejected by server", re.IGNORECASE)),
        ("mission_action_rejected", "mission_executor", re.compile(r"ManeuverActionNode::onFailure\(\):.*rejected", re.IGNORECASE)),
        ("mission_action_rejected", "maneuver_controller", re.compile(r"ManeuverServer::handleGoal\(\):.*rejecting goal", re.IGNORECASE)),
    )

    def __init__(self, tools: Any, artifact_dir: Path, *, enabled: bool):
        self.tools = tools
        self.artifact_dir = artifact_dir
        self.enabled = enabled
        self.started_at = datetime.now(timezone.utc)
        self.cleanup_started_at: datetime | None = None
        self.events: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.ever_in_air = False
        self.cleanup_started = False
        self._reported_node_failures: set[tuple[str, str, str]] = set()
        self._reported_mission_failures: set[tuple[str, str, str]] = set()

    def after_call(self, index: int, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        if not self.enabled:
            return

        if self._is_cleanup_call(tool_name, arguments):
            self.cleanup_started = True
            if self.cleanup_started_at is None:
                self.cleanup_started_at = datetime.now(timezone.utc)

        sample = self._sample_px4(index, tool_name)
        if sample:
            self.samples.append(sample)
            status = sample.get("px4_status") or {}
            health = sample.get("px4_health") or {}
            health_data = health.get("data") or {}
            in_air = status.get("in_air")
            flight_mode = str(status.get("flight_mode") or "").upper()

            if in_air is True:
                self.ever_in_air = True

            if health_data.get("failsafe") is True:
                self._record_event("px4_failsafe", index, tool_name, "PX4 vehicle_status.failsafe became true", sample)

            if flight_mode in {"RETURN_TO_LAUNCH", "RTL"}:
                self._record_event("px4_failsafe", index, tool_name, f"PX4 entered {flight_mode}", sample)

            if self.ever_in_air and not self.cleanup_started and in_air is False:
                self._record_event("unexpected_landing", index, tool_name, "vehicle landed before cleanup/land step", sample)

        if not self.cleanup_started:
            for event in self._critical_node_failures(index, tool_name):
                self._record_event("node_crash", index, tool_name, event["message"], event)
            for event in self._critical_mission_failures(index, tool_name):
                self._record_event(event["type"], index, tool_name, event["message"], event)

        if not result.get("success", False) and not self.cleanup_started:
            self._record_event(
                "mission_failed",
                index,
                tool_name,
                f"tool failed before cleanup: {result.get('message', '')}",
                {"result": result},
            )

    def finalize(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        for event in self._critical_node_failures(-1, "finalize"):
            self._record_event("node_crash", -1, "finalize", event["message"], event)
        for event in self._critical_mission_failures(-1, "finalize"):
            self._record_event(event["type"], -1, "finalize", event["message"], event)

        verdict = "passed"
        success = True
        if self.events:
            success = False
            verdict = str(self.events[0]["type"])

        px4_events = None
        px4_warning_events: list[dict[str, Any]] = []
        px4_critical_events: list[dict[str, Any]] = []
        try:
            extracted = self.tools.px4_ulog_events(filename="px4_ulog_events.json", max_events=200)
            px4_events = {
                "success": bool(extracted.success),
                "message": extracted.message,
                "data": extracted.data,
            }
            if extracted.success and isinstance(extracted.data, dict):
                px4_warning_events = [
                    event
                    for event in extracted.data.get("classified_events", [])
                    if isinstance(event, dict) and event.get("severity") == "warning"
                ]
                px4_critical_events = [
                    event
                    for event in extracted.data.get("classified_events", [])
                    if isinstance(event, dict) and event.get("severity") == "critical"
                ]
                for event in px4_critical_events:
                    event_message = str(event.get("message") or event.get("line") or "")
                    self._record_event(
                        "px4_ulog_critical",
                        -1,
                        "finalize",
                        f"critical PX4 ULog event: {event_message}",
                        event,
                    )
        except Exception as exc:
            px4_events = {"success": False, "message": str(exc), "data": None}

        if self.events:
            success = False
            verdict = str(self.events[0]["type"])

        artifact_paths = []
        if self.artifact_dir.exists():
            for path in sorted(self.artifact_dir.rglob("*")):
                if path.is_file() and path.name != "mcp_batch_safety_summary.json":
                    artifact_paths.append(str(path))

        summary = {
            "success": success,
            "verdict": verdict,
            "started_at": self.started_at.isoformat(),
            "event_count": len(self.events),
            "first_failure": self.events[0] if self.events else None,
            "events": self.events,
            "sample_count": len(self.samples),
            "latest_sample": self.samples[-1] if self.samples else None,
            "px4_ulog_events": px4_events,
            "px4_warning_event_count": len(px4_warning_events),
            "px4_warning_events": px4_warning_events,
            "px4_critical_event_count": len(px4_critical_events),
            "px4_critical_events": px4_critical_events,
            "artifact_paths": artifact_paths,
        }
        _write_json(self.artifact_dir / "mcp_batch_safety_summary.json", summary)
        return summary

    def _sample_px4(self, index: int, tool_name: str) -> dict[str, Any] | None:
        try:
            status = self.tools.px4("status", timeout_sec=2.0)
        except Exception as exc:
            return {"index": index, "tool": tool_name, "px4_status_error": str(exc)}

        try:
            health = self.tools.px4("health", timeout_sec=1.0, stable_sec=0.0)
            health_payload = {
                "success": bool(health.success),
                "message": health.message,
                "data": health.data,
            }
        except Exception as exc:
            health_payload = {"success": False, "message": str(exc), "data": None}

        return {
            "index": index,
            "tool": tool_name,
            "px4_status": status.data if status.success else None,
            "px4_status_message": status.message,
            "px4_health": health_payload,
        }

    def _critical_node_failures(self, index: int, tool_name: str) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for entity in self._CRITICAL_ENTITIES:
            try:
                result = self.tools.logs(command="capture", entity_id=entity, history=True, timeout_sec=5.0)
            except Exception:
                continue
            stdout = ""
            if isinstance(result.data, dict):
                stdout = str(result.data.get("stdout", ""))
            for match in self._NODE_CRASH_RE.finditer(stdout):
                returncode = int(match.group("returncode"))
                if returncode == 0:
                    continue
                event_time = datetime.strptime(match.group("time"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if event_time < self.started_at:
                    continue
                key = (entity, str(returncode), match.group("time"))
                if key in self._reported_node_failures:
                    continue
                self._reported_node_failures.add(key)
                failures.append(
                    {
                        "index": index,
                        "tool": tool_name,
                        "entity": entity,
                        "returncode": returncode,
                        "time": event_time.isoformat(),
                        "message": f"critical node {entity} exited with returncode {returncode}",
                    }
                )
        return failures

    def _critical_mission_failures(self, index: int, tool_name: str) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        logs_by_entity: dict[str, str] = {}
        for _, entity, _ in self._MISSION_LOG_PATTERNS:
            if entity in logs_by_entity:
                continue
            try:
                result = self.tools.logs(command="capture", entity_id=entity, history=True, timeout_sec=5.0)
            except Exception:
                logs_by_entity[entity] = ""
                continue
            logs_by_entity[entity] = str((result.data or {}).get("stdout", "")) if isinstance(result.data, dict) else ""

        for event_type, entity, pattern in self._MISSION_LOG_PATTERNS:
            for line in logs_by_entity.get(entity, "").splitlines():
                if not pattern.search(line):
                    continue
                timestamp = self._extract_log_timestamp(line)
                if timestamp is None:
                    continue
                event_time = datetime.fromtimestamp(timestamp, timezone.utc)
                if event_time < self.started_at:
                    continue
                if self.cleanup_started_at is not None and event_time >= self.cleanup_started_at:
                    continue
                key = (entity, event_type, line)
                if key in self._reported_mission_failures:
                    continue
                self._reported_mission_failures.add(key)
                failures.append(
                    {
                        "type": event_type,
                        "index": index,
                        "tool": tool_name,
                        "entity": entity,
                        "time": event_time.isoformat(),
                        "line": line,
                        "message": f"{event_type} in {entity}: {line}",
                    }
                )
        return failures

    @classmethod
    def _extract_log_timestamp(cls, line: str) -> float | None:
        match = cls._LOG_TIMESTAMP_RE.search(line)
        if match is None:
            return None
        try:
            return float(match.group("timestamp"))
        except ValueError:
            return None

    def _record_event(self, event_type: str, index: int, tool_name: str, message: str, data: Any) -> None:
        self.events.append(
            {
                "type": event_type,
                "index": index,
                "tool": tool_name,
                "message": message,
                "time": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }
        )

    @staticmethod
    def _is_cleanup_call(tool_name: str, arguments: dict[str, Any]) -> bool:
        return _is_cleanup_call(tool_name, arguments)


def _is_cleanup_call(tool_name: str, arguments: dict[str, Any]) -> bool:
    command = str(arguments.get("command", "")).lower()
    if tool_name == "px4" and command in {"land", "disarm", "return_to_launch"}:
        return True
    if tool_name == "mission.executor_action" and str(arguments.get("request", "")).lower() in {"land", "disarm"}:
        return True
    if tool_name == "system" and command in {"shutdown", "stop"}:
        return True
    if tool_name == "simulation" and command == "stop":
        return True
    return False


def _redirect_stderr(artifact_dir: str, filename: str) -> Path:
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, filename)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    os.dup2(fd, 2)
    os.close(fd)
    return Path(path)


def _write_stderr_summary(artifact_dir: Path, stderr_path: Path | None) -> dict[str, Any] | None:
    if stderr_path is None or not stderr_path.exists():
        return None
    summary = _summarize_stderr(stderr_path)
    artifact = artifact_dir / "mcp_batch_stderr_summary.json"
    summary["artifact_path"] = str(artifact)
    _write_json(artifact, summary)
    return summary


def _summarize_stderr(stderr_path: Path) -> dict[str, Any]:
    lines = stderr_path.read_text(errors="replace").splitlines()
    known_warning_counts = {
        "rclpy_service_response_timeout": 0,
        "fastdds_multicast_network_unreachable": 0,
    }
    unknown_lines: list[str] = []
    skip_next_service_context = False

    for line in lines:
        if "RuntimeWarning: failed to send response (timeout): client will not receive response" in line:
            known_warning_counts["rclpy_service_response_timeout"] += 1
            skip_next_service_context = True
            continue
        if "Exception sending a multicast message:Network is unreachable" in line:
            known_warning_counts["fastdds_multicast_network_unreachable"] += 1
            continue
        if skip_next_service_context and "service_send_response" in line:
            skip_next_service_context = False
            continue
        skip_next_service_context = False
        if line.strip():
            unknown_lines.append(line)

    return {
        "stderr_path": str(stderr_path),
        "line_count": len(lines),
        "known_warning_counts": known_warning_counts,
        "unknown_line_count": len(unknown_lines),
        "unknown_line_samples": unknown_lines[:20],
        "has_unknown_stderr": bool(unknown_lines),
    }


def _hard_timeout_sec(arguments: dict[str, Any], default_timeout_sec: float) -> float:
    requested = arguments.get("timeout_sec")
    if requested is None:
        return default_timeout_sec
    try:
        return max(5.0, float(requested) + 15.0)
    except (TypeError, ValueError):
        return default_timeout_sec


@contextmanager
def _call_timeout(timeout_sec: float):
    if os.name != "posix" or timeout_sec <= 0:
        yield
        return

    def on_timeout(_signum, _frame):
        raise TimeoutError(f"MCP batch tool call exceeded hard timeout of {timeout_sec}s")

    previous_handler = signal.signal(signal.SIGALRM, on_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _append_progress(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(event, default=str) + "\n")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _interpolate(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            variable_name = value[2:-1]
            if variable_name not in variables:
                raise KeyError(f"missing batch variable: {variable_name}")
            return variables[variable_name]
        result = value
        for variable_name, variable_value in variables.items():
            result = result.replace("${" + variable_name + "}", str(variable_value))
        return result
    if isinstance(value, list):
        return [_interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item, variables) for key, item in value.items()}
    return value


def _get_field_path(data: Any, field_path: str) -> Any:
    current = data
    for part in field_path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"field path not found: {field_path}")
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(f"field path not traversable: {field_path}")
    return current


if __name__ == "__main__":
    main()
