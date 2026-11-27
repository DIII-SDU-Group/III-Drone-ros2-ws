"""Run catalogue and node-log parsing for the paper's HIL soak results.

Everything here reads the Pi's ROS node logs (mission executor, maneuver
controller) and the runner's run directory; nothing needs ROS. The bag-based
signals live in bag_extract.py.

Landing phase: from a Reach Cable start to the next Cable Charging or
Inspection start, or to Reach Cable failing. Its outcome:
  captured  - a cable landing succeeded, or the gripper-closed check after a
              reported landing failure found the gripper closed (late capture)
  limit     - Reach Cable failed after `LANDING_ATTEMPT_LIMIT` aborted landings
  interrupted - Reach Cable failed for another reason (infrastructure)
  open      - the run ended inside the phase (left out of the statistics)
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import json
import math
import pathlib
import re
from typing import Iterable

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
LANDING_ATTEMPT_LIMIT = 3  # validation setting of reach_cable_tree (num_attempts)
STAMP = re.compile(r"^\[(DEBUG|INFO|WARN|ERROR|FATAL)\] \[(\d+\.\d+)\] \[([^\]]+)\]: (.*)$")
LOG_NODES = ("mission_executor", "maneuver_controller")

# Log signatures (substring match). Each names what the line proves.
SIG = {
    "mode_start": re.compile(r"TreeExecutor::StartExecution\(\): Starting execution of (\w+)\."),
    "landing_goal": "CableLandingManeuverActionNode::setManeuverGoal(): Setting goal",
    "landing_succeeded": "onResultReceived(): cable_landing: Maneuver action succeeded",
    "landing_aborted": "onFailure(): cable_landing: Maneuver action aborted",
    "retry_takeoff": "onGoalAccepted(): cable_takeoff_retry: Maneuver action goal accepted",
    "retry_takeoff_rejected": "cable_takeoff_retry: Maneuver goal rejected",
    "gripper_closed": "VerifyGripperClosedConditionNode::onTick(): Gripper is closed",
    "gripper_not_closed": "VerifyGripperClosedConditionNode::onTick(): Gripper is not closed",
    "reach_failed": "Mode Reach Cable failed",
    "vgate": "isTargetWithinGripperVGate(): Gripper V gate violated:",
    "above_target": "isWithinSafetyMargins(): Above-target distance violated",
    "lock_reacquisition": "attempting physical-line reacquisition",
    "perception_recovery": "onGoalAccepted(): hover_during_perception_reacquisition",
    "detection_repoll": re.compile(r"verify_powerline_detected: (Overview line id \d+ not matched|Failed to transform)"),
    "arm_request": "ModeExecutorActionNode::setGoal(): Setting action request arm",
    "disarm_request": "ModeExecutorActionNode::setGoal(): Setting action request disarm",
    "push_not_established": "push against the cable was not established",
    "leave_succeeded": "Mode Leave Cable completed with result Success",
    "charging_succeeded": "Mode Cable Charging completed with result Success",
    "node_accepted": re.compile(r"ManeuverActionNode::onGoalAccepted\(\): (\w+): "),
    "node_succeeded": re.compile(r"ManeuverActionNode::onResultReceived\(\): (\w+): Maneuver action succeeded"),
    "node_failed": re.compile(r"ManeuverActionNode::onFailure\(\): (\w+): "),
}


def utc(text: str) -> float:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


@dataclasses.dataclass
class Run:
    condition: str
    run: int
    start: float
    end: float
    running: bool
    run_dir: pathlib.Path
    node_logs: pathlib.Path
    bag_roots: list[pathlib.Path]
    rtf: pathlib.Path | None
    pi_cpu: pathlib.Path | None
    pi_mem: pathlib.Path | None
    ended_by: str

    @property
    def hours(self) -> float:
        return (self.end - self.start) / 3600.0

    def bag_dirs(self) -> list[pathlib.Path]:
        """Mission bag folders whose start time lies in the run window."""
        found = []
        for root in self.bag_roots:
            for d in sorted(root.glob("inspection_*")):
                m = re.match(r"inspection_(\d{8})_(\d{6})$", d.name)
                if not m or not d.is_dir():
                    continue
                t = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
                if self.start - 120 <= t <= self.end:
                    found.append(d)
        return found


@dataclasses.dataclass
class Catalogue:
    config: dict
    runs: list[Run]

    def by_condition(self, condition: str) -> list[Run]:
        return [r for r in self.runs if r.condition == condition]

    def run(self, number: int) -> Run:
        return next(r for r in self.runs if r.run == number)

    @property
    def output(self) -> pathlib.Path:
        return ROOT / self.config["output"]

    @property
    def cache(self) -> pathlib.Path:
        return ROOT / self.config["cache"]


def load_catalogue(path: pathlib.Path | None = None, now: float | None = None) -> Catalogue:
    config = yaml.safe_load((path or pathlib.Path(__file__).with_name("runs.yaml")).read_text())
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    runs = []
    for condition, spec in config["conditions"].items():
        for item in spec["runs"]:
            run_dir = ROOT / item["run_dir"]
            plan = json.loads((run_dir / "run_plan.json").read_text())
            opt = lambda key: ROOT / item[key] if item.get(key) else None  # noqa: E731
            runs.append(Run(
                condition=condition, run=int(item["run"]), start=utc(plan["started_at"]),
                end=utc(item["end"]) if item.get("end") else now, running=not item.get("end"),
                run_dir=run_dir, node_logs=ROOT / item["node_logs"],
                bag_roots=[ROOT / p for p in item["bag_roots"]],
                rtf=opt("rtf"), pi_cpu=opt("pi_cpu"), pi_mem=opt("pi_mem"),
                ended_by=item.get("ended_by", ""),
            ))
    return Catalogue(config, runs)


# --------------------------------------------------------------------------- logs

def read_log_lines(run: Run) -> list[tuple[float, str, str]]:
    """(epoch, level, message) of the mission executor and maneuver controller
    inside the run window, time-sorted."""
    lines = []
    for node in LOG_NODES:
        for path in (run.node_logs / node).rglob(f"{node}_*.log"):
            created = re.search(r"_(\d{13})\.log$", path.name)
            if created and int(created.group(1)) / 1000.0 > run.end:
                continue
            if path.stat().st_mtime < run.start:
                continue
            with open(path, errors="replace") as handle:
                for line in handle:
                    m = STAMP.match(line)
                    if m:
                        t = float(m.group(2))
                        if run.start <= t <= run.end:
                            lines.append((t, m.group(1), m.group(4).rstrip("\n")))
    lines.sort(key=lambda item: item[0])
    # Duplicate copies of a log (rsync partials) would double-count lines.
    deduped, previous = [], None
    for item in lines:
        if item != previous:
            deduped.append(item)
        previous = item
    return deduped


def matches(signature, message: str):
    if isinstance(signature, str):
        return signature in message
    return signature.search(message)


@dataclasses.dataclass
class LandingPhase:
    run: int
    condition: str
    start: float
    end: float
    attempts: int = 0
    outcome: str = "open"          # captured | limit | interrupted | open
    late_capture: bool = False
    vgate: int = 0
    above_target: int = 0
    retry_takeoffs: int = 0
    retry_takeoffs_rejected: int = 0
    gripper_checks: int = 0         # gripper-closed verifications after a landing failure
    lock_reacquisitions: int = 0
    perception_recoveries: int = 0
    detection_repolls: int = 0
    aborts: int = 0
    aborts_vgate: int = 0           # abort preceded (<= 2 s) by a V-gate violation
    aborts_above_target: int = 0    # ... by an above-target violation

    @property
    def category(self) -> str:
        if self.outcome == "captured":
            return {1: "1st attempt", 2: "2nd attempt", 3: "3rd attempt"}.get(self.attempts, f"{self.attempts} attempts")
        if self.outcome == "limit":
            return "limit reached"
        return self.outcome


@dataclasses.dataclass
class ModeInterval:
    mode: str
    start: float
    end: float


@dataclasses.dataclass
class RunLog:
    run: Run
    lines: list[tuple[float, str, str]]
    phases: list[LandingPhase]
    modes: list[ModeInterval]
    counts: dict[str, int]

    def cycles(self) -> list[dict]:
        """Inspection start to the next Inspection start, with the landing
        phase inside it (if any)."""
        starts = [m.start for m in self.modes if m.mode == "inspection_demo"]
        out = []
        for a, b in zip(starts, starts[1:]):
            inside = [p for p in self.phases if a <= p.start < b]
            out.append({"run": self.run.run, "start": a, "end": b, "duration_s": b - a,
                        "phase": inside[0] if inside else None})
        return out


def parse_run(run: Run) -> RunLog:
    lines = read_log_lines(run)
    counts = {key: 0 for key in SIG}
    modes: list[ModeInterval] = []
    phases: list[LandingPhase] = []
    current: LandingPhase | None = None
    gripper_check_open = False
    last_trigger: tuple[float, str] | None = None
    for t, _level, msg in lines:
        for key, signature in SIG.items():
            if key in ("mode_start", "node_accepted", "node_succeeded", "node_failed"):
                continue
            if matches(signature, msg):
                counts[key] += 1
        m = SIG["mode_start"].search(msg)
        if m:
            if modes:
                modes[-1].end = t
            modes.append(ModeInterval(m.group(1), t, run.end))
            if current is not None and m.group(1) in ("cable_charging", "inspection_demo", "reach_cable"):
                current.end = t
                if current.outcome == "open" and m.group(1) == "cable_charging":
                    current.outcome = "captured"  # charging only follows a capture
                phases.append(current)
                current = None
            if m.group(1) == "reach_cable":
                current = LandingPhase(run.run, run.condition, t, run.end)
            continue
        if current is None:
            continue
        if SIG["landing_goal"] in msg:
            current.attempts += 1
            gripper_check_open = False
        elif SIG["landing_succeeded"] in msg:
            current.outcome = "captured"
        elif SIG["landing_aborted"] in msg:
            gripper_check_open = True
            current.aborts += 1
            if last_trigger is not None and t - last_trigger[0] <= 2.0:
                if last_trigger[1] == "vgate":
                    current.aborts_vgate += 1
                else:
                    current.aborts_above_target += 1
            last_trigger = None
        elif SIG["retry_takeoff"] in msg:
            current.retry_takeoffs += 1
        elif SIG["retry_takeoff_rejected"] in msg:
            current.retry_takeoffs_rejected += 1
        elif SIG["gripper_closed"] in msg and gripper_check_open:
            current.late_capture = True
            current.outcome = "captured"
            current.gripper_checks += 1
            gripper_check_open = False
        elif SIG["gripper_not_closed"] in msg and gripper_check_open:
            current.gripper_checks += 1
            gripper_check_open = False
        elif SIG["vgate"] in msg:
            current.vgate += 1
            last_trigger = (t, "vgate")
        elif SIG["above_target"] in msg:
            current.above_target += 1
            last_trigger = (t, "above_target")
        elif SIG["lock_reacquisition"] in msg:
            current.lock_reacquisitions += 1
        elif SIG["perception_recovery"] in msg:
            current.perception_recoveries += 1
        elif matches(SIG["detection_repoll"], msg):
            current.detection_repolls += 1
        elif SIG["reach_failed"] in msg:
            current.end = t
            if current.outcome != "captured":
                current.outcome = "limit" if current.attempts >= LANDING_ATTEMPT_LIMIT else "interrupted"
            phases.append(current)
            current = None
    if current is not None:
        phases.append(current)  # still open at the window end
    return RunLog(run, lines, phases, modes, counts)


def node_intervals(lines: Iterable[tuple[float, str, str]], start: float, end: float) -> list[dict]:
    """Behaviour-tree maneuver node executions (accepted -> succeeded/failed)
    inside [start, end], for the execution trace."""
    open_nodes: dict[str, float] = {}
    out = []
    for t, _level, msg in lines:
        if t < start or t > end:
            continue
        m = SIG["node_accepted"].search(msg)
        if m:
            open_nodes[m.group(1)] = t
            continue
        for key, result in (("node_succeeded", "succeeded"), ("node_failed", "failed")):
            m = SIG[key].search(msg)
            if m:
                name = m.group(1)
                begin = open_nodes.pop(name, t)
                if "rejected" in msg:
                    result = "rejected"
                out.append({"node": name, "start": begin, "end": t, "result": result})
                break
    for name, begin in open_nodes.items():
        out.append({"node": name, "start": begin, "end": end, "result": "running"})
    return sorted(out, key=lambda item: item["start"])


def driver_events(run: Run) -> list[dict]:
    path = run.run_dir / "driver_events.json"
    return json.loads(path.read_text()) if path.exists() else []


def read_tsv(path: pathlib.Path | None, run: int) -> dict[str, list[tuple[float, float]]]:
    """name -> [(epoch, value)] for one run of a soak TSV (utc, run, name, value)."""
    series: dict[str, list[tuple[float, float]]] = {}
    if path is None or not path.exists():
        return series
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[1] != str(run):
            continue
        try:
            series.setdefault(parts[2], []).append((utc(parts[0]), float(parts[3])))
        except ValueError:
            continue
    return series


def read_rtf(path: pathlib.Path | None, run: int) -> list[tuple[float, float, float]]:
    """(epoch, worst 5 s factor, 60 s factor) for one run of rtf.tsv."""
    rows = []
    if path is None or not path.exists():
        return rows
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1] == str(run):
            try:
                rows.append((utc(parts[0]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    return rows


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def first_index_at_or_after(times: list[float], t: float) -> int:
    return bisect.bisect_left(times, t)
