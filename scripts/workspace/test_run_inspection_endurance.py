"""Offline tests for the inspection endurance acceptance evaluator."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_inspection_endurance as endurance  # noqa: E402

START = "2026-09-28T06:00:00+00:00"
DEADLINE = "2026-09-28T06:30:00+00:00"


def cycle(resumed: str, charging: str = "2026-09-28T06:05:00+00:00",
          leave: str = "2026-09-28T06:05:40+00:00") -> dict:
    return {"cable_charging_active_at": charging,
            "leave_cable_succeeded_activation_receipt_at": leave,
            "inspection_resumed_receipt_at": resumed}


def write_run(tmp_path: Path, *, cycles: list[dict], events: list[dict] | None = None,
              execution: dict | None = None, **observer_overrides) -> Path:
    observer = {"mission_outcome": "duration_elapsed", "mission_failures": [], "mode_failures": [],
                "mission_failure_latched": False, "cleanup_outcome": "safe_landed_disarmed",
                "mission_started_at": START, "duration_elapsed_at": DEADLINE,
                "active_duration_sec": 1800.0, "completed_cycles": cycles}
    observer.update(observer_overrides)
    default_events = [{"event": "charging_full_verified",
                       "evidence": {"battery_remaining": 0.99, "battery_age_sec": 0.01}}] * len(cycles)
    default_events += [{"event": "final_safe_landed_disarmed",
                        "cleanup_safety_evidence": {"safe_landed_disarmed": True}}]
    files = {"run_plan.json": {"target": "sim", "duration_sec": 1800, "required_cycles": 2},
             "execution_result.json": execution or {"driver_exit": 0, "observer_exit": 0, "probe_exit": 0},
             "mission_lifecycle_observation.json": observer,
             "driver_events.json": default_events if events is None else events}
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content))
    return tmp_path


def test_accepts_complete_in_window_run(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:20:00+00:00")])
    report = endurance.evaluate(run_dir)
    assert report["accepted"], report["failures"]
    assert report["completed_cycles_in_window"] == 2
    assert json.loads((run_dir / "acceptance_report.json").read_text())["accepted"] is True


def test_cycle_resumed_after_deadline_is_not_counted(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:30:00.500000+00:00")])
    report = endurance.evaluate(run_dir)
    assert not report["accepted"]
    assert report["completed_cycles_total"] == 2
    assert report["completed_cycles_in_window"] == 1


def test_reports_every_failure_without_short_circuit(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00",
                                                leave="2026-09-28T06:08:00+00:00")],
                        execution={"driver_exit": 1, "observer_exit": 0, "probe_exit": 0},
                        events=[{"event": "leave_cable_commanded"}],
                        mission_failure_latched=True)
    failures = endurance.evaluate(run_dir)["failures"]
    assert "driver_exit=1" in failures
    assert "mission failure latched" in failures
    assert "manual recharge/leave intent used" in failures
    assert any("charging_to_leave" in failure for failure in failures)
    assert "driver did not prove final landed/disarmed" in failures


def test_live_checks_use_injected_status_and_fault_counts(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:20:00+00:00")])
    target = endurance.Target("sim", "container", None)
    unsafe = {"freshness": "fresh", "latest": {"command_transport": {"armed": True, "in_air": False},
                                               "ros_uxrce": {"armed": False, "in_air": False}}}
    plan = json.loads((run_dir / "run_plan.json").read_text())
    plan["started_at"] = "2026-09-28T06:00:00+00:00"
    (run_dir / "run_plan.json").write_text(json.dumps(plan))
    start = 1790575200  # 2026-09-28T06:00:00Z
    lines = [f"[ERROR] [{start + 60}.1] [control.maneuver_controller]: CableAware terminal tracking failed: continuity envelope 12",
             f"[WARN] [{start + 61}.2] [mission.executor]: slow response 3 ms",
             f"[ERROR] [{start - 60}.0] [control.maneuver_controller]: before the run, ignored"]
    report = endurance.evaluate(run_dir, target=target, fetch_status=lambda host: unsafe,
                                log_lines=lambda t, d: lines, strict_warnings=True)
    assert not report["accepted"]
    assert report["log_counts"] == {"ERROR": 1, "WARN": 1, "FATAL": 0}
    assert any("continuity faults" in failure for failure in report["failures"])
    assert any("1 ERROR" in failure for failure in report["failures"])
    assert any("1 WARN" in failure for failure in report["failures"])
    assert "final Runtime API status is not fresh landed/disarmed" in report["failures"]


def test_hil_observer_commands_go_over_ssh_with_bounded_options() -> None:
    target = endurance.Target("hil", "container", "192.0.2.10")
    command = target.observer_shell("true")
    assert command[0] == "ssh" and "BatchMode=yes" in command and "ConnectTimeout=8" in command
    assert command[-2] == "iii@192.0.2.10"
    assert "setup_hil.bash" in command[-1]


def test_log_patterns_group_repeated_diagnostics() -> None:
    lines = [f"[ERROR] [100.0] [ctl]: Terminal hold stream fwp:{n}:1 failed (phase=2)" for n in range(5)]
    lines.append("[INFO] [100.0] [ctl]: ignored")
    summary = endurance.summarize_log_lines(lines, 0, 200)
    assert summary["counts"]["ERROR"] == 5
    assert len(summary["patterns"]) == 1 and summary["patterns"][0]["count"] == 5


def test_fresh_start_recreates_the_simulation_epoch_per_target() -> None:
    sim = [(command[1:], required) for command, required in endurance.fresh_start_commands("sim", None)]
    assert sim == [(["stack", "stop"], False),
                   (["stack", "start", "--headless", "--recreate-sim"], True)]
    hil = endurance.fresh_start_commands("hil", "192.0.2.10")
    assert [(command[1:], required) for command, required in hil] == [
        (["hil", "restart", "--headless", "--host", "192.0.2.10"], True)]


def test_identical_lines_from_duplicate_log_files_count_once() -> None:
    line = "[ERROR] [150.123456789] [ctl]: stream failed"
    summary = endurance.summarize_log_lines([line, line, line + "  "], 0, 200)
    assert summary["counts"]["ERROR"] == 1


def test_recorder_transport_loss_is_a_metric_not_a_quirk() -> None:
    lines = ["[WARN] [150.1] [rosbag2_recorder]: Number of messages lost on the transport layer: 20",
             "[WARN] [150.2] [other_node]: Number of messages lost on the transport layer: 3"]
    summary = endurance.summarize_log_lines(lines, 0, 200)
    assert summary["recording_lost_messages"] == 20
    # Only the recorder's statistic is exempt; the same text elsewhere counts.
    assert summary["counts"]["WARN"] == 1


def test_final_vehicle_status_is_captured_once_and_reused(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:20:00+00:00")])
    plan = json.loads((run_dir / "run_plan.json").read_text())
    plan["started_at"] = START
    (run_dir / "run_plan.json").write_text(json.dumps(plan))
    target = endurance.Target("sim", "container", None)
    safe = {"freshness": "fresh", "latest": {"command_transport": {"armed": False, "in_air": False},
                                             "ros_uxrce": {"armed": False, "in_air": False}}}
    calls = []
    fetch = lambda host: calls.append(host) or safe  # noqa: E731
    assert endurance.evaluate(run_dir, target=target, fetch_status=fetch, log_lines=lambda t, d: [])["accepted"]
    def unavailable(host):
        raise OSError("stack stopped")
    report = endurance.evaluate(run_dir, target=target, fetch_status=unavailable, log_lines=lambda t, d: [])
    assert report["accepted"] and report["final_vehicle_safe"] is True
    assert calls == ["127.0.0.1"]


def _hold_run(tmp_path: Path, events: list[dict]) -> Path:
    files = {"run_plan.json": {"target": "sim", "scenario": "hold", "hold_phase": "reach_cable",
                               "duration_sec": 1800, "required_cycles": 4, "started_at": START},
             "execution_result.json": {"driver_exit": 0, "observer_exit": 0, "probe_exit": 0,
                                       "finished_at": DEADLINE},
             "driver_events.json": events}
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content))
    return tmp_path


SAFE_STATUS = {"freshness": "fresh", "latest": {"command_transport": {"armed": False, "in_air": False},
                                                "ros_uxrce": {"armed": False, "in_air": False}}}
HANDOVER_EVENTS = [{"event": "operator_hold_handover_verified", "phase": "reach_cable", "settled_sec": 0.4},
                   {"event": "final_safe_landed_disarmed",
                    "cleanup_safety_evidence": {"safe_landed_disarmed": True}}]


def test_hold_scenario_accepts_a_silent_verified_handover(tmp_path: Path) -> None:
    run_dir = _hold_run(tmp_path, HANDOVER_EVENTS)
    report = endurance.evaluate(run_dir, target=endurance.Target("sim", "c", None),
                                fetch_status=lambda host: SAFE_STATUS,
                                log_lines=lambda t, d: [
                                    "[WARN] [1790575300.0] [rosbag2_recorder]: "
                                    "Number of messages lost on the transport layer: 4"])
    assert report["accepted"], report["failures"]
    assert report["handover"]["settled_sec"] == 0.4
    assert report["recording_lost_messages"] == 4


def test_hold_scenario_rejects_any_warning_and_missing_handover(tmp_path: Path) -> None:
    run_dir = _hold_run(tmp_path, HANDOVER_EVENTS[1:])
    report = endurance.evaluate(run_dir, target=endurance.Target("sim", "c", None),
                                fetch_status=lambda host: SAFE_STATUS,
                                log_lines=lambda t, d: [
                                    "[WARN] [1790575300.0] [control.mc]: fly_to_position: Maneuver failed"])
    assert not report["accepted"]
    assert "operator Hold handover was not verified" in report["failures"]
    assert any("node logs not silent" in failure for failure in report["failures"])


def _hil_run(tmp_path: Path, disarm: dict | None) -> Path:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:20:00+00:00")],
                        execution={"driver_exit": 0, "observer_exit": 0, "probe_exit": 0,
                                   "px4_monitor_exit": 0 if disarm and disarm["remained_disarmed"] else 1})
    (run_dir / "run_plan.json").write_text(json.dumps(
        {"target": "hil", "duration_sec": 1800, "required_cycles": 2, "physical_px4_monitor": True}))
    if disarm is not None:
        (run_dir / "physical_px4_disarm.json").write_text(json.dumps(disarm))
    return run_dir


def test_hil_acceptance_requires_the_physical_px4_disarm_record(tmp_path: Path) -> None:
    good = {"remained_disarmed": True, "failures": [], "observed_sec": 1900.0, "samples": 1890,
            "armed_samples": 0, "max_gap_sec": 1.2, "peers": ["10.41.10.2"]}
    (tmp_path / "good").mkdir()
    report = endurance.evaluate(_hil_run(tmp_path / "good", good))
    assert report["accepted"], report["failures"]
    assert report["physical_px4"]["armed_samples"] == 0

    (tmp_path / "missing").mkdir()
    report = endurance.evaluate(_hil_run(tmp_path / "missing", None))
    assert "missing physical_px4_disarm.json" in report["failures"]

    armed = dict(good, remained_disarmed=False, armed_samples=3,
                 failures=["physical PX4 reported armed in 3 samples"])
    (tmp_path / "armed").mkdir()
    report = endurance.evaluate(_hil_run(tmp_path / "armed", armed))
    assert "physical PX4: physical PX4 reported armed in 3 samples" in report["failures"]
    assert "px4_monitor_exit=1" in report["failures"]


def test_sim_acceptance_needs_no_physical_px4_record(tmp_path: Path) -> None:
    run_dir = write_run(tmp_path, cycles=[cycle("2026-09-28T06:10:00+00:00"),
                                          cycle("2026-09-28T06:20:00+00:00")])
    report = endurance.evaluate(run_dir)
    assert report["accepted"] and "physical_px4" not in report
