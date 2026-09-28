#!/usr/bin/env python3
"""Run and judge one canonical inspection-mission endurance run in SIM or HIL.

The runner is the repeatable form of the per-attempt scripts used during the
HIL/SIM endurance work. It expects the target stack to already be up
(`./iii-dev stack start` for SIM, `./iii-dev hil start` for HIL), then:

1. records the source identity (superproject and submodule HEADs, dirtiness);
2. starts the passive lifecycle observer and perception probe (SIM: in the
   devcontainer, HIL: on the Pi over SSH);
3. runs `hil_inspection_cycle_driver.py` in the devcontainer with automatic
   recharge/leave cycles;
4. stops the probe (and the observer on driver failure), syncs HIL evidence
   back from the Pi, and writes `execution_result.json`;
5. evaluates acceptance into `acceptance_report.json`.

`--evaluate-only RUN_DIR` re-runs step 5 on existing evidence. The runner never
arms or commands the vehicle itself; the driver owns flight commands and its
own landed/disarmed cleanup.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_WS = "/home/iii/ws"
PI_WS = "/home/iii/ws"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]
CONTINUITY_FAULT_MARKERS = ("continuity envelope", "committed rebased")
PROCESS_READY_TIMEOUT_SEC = 20.0
MAX_CHARGING_TO_LEAVE_SEC = 90.0
# The passive observer must outlast the driver's own bounded prelude (possible
# runtime re-registration, preflight, and up to 90 s CustomOperation ingress);
# the driver still fails on its own timeouts, so this hides nothing.
OBSERVER_PRELUDE_TIMEOUT_SEC = 420


class RunnerError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(command: list[str], *, timeout: float | None = 60, check: bool = True,
        capture: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=check, timeout=timeout, text=True,
                          capture_output=capture, **kwargs)


# --------------------------------------------------------------------------
# Target description


class Target:
    """Where the passive observers run and how commands reach them."""

    def __init__(self, name: str, container_id: str, pi_peer: str | None) -> None:
        self.name = name
        self.container_id = container_id
        self.pi_peer = pi_peer

    @property
    def setup_script(self) -> str:
        return "setup_hil.bash" if self.name == "hil" else "setup_dev.bash"

    @property
    def api_host(self) -> str:
        return self.pi_peer if self.name == "hil" else "127.0.0.1"

    def container_shell(self, script: str, env: dict[str, str] | None = None) -> list[str]:
        env_args: list[str] = []
        for key, value in (env or {}).items():
            env_args += ["--env", f"{key}={value}"]
        prelude = (f"source /opt/ros/jazzy/setup.bash && source {CONTAINER_WS}/install/setup.bash"
                   f" && source {CONTAINER_WS}/setup/{self.setup_script} && cd {CONTAINER_WS} && ")
        return ["docker", "exec", "--user", "iii", *env_args, self.container_id,
                "bash", "-lc", prelude + script]

    def observer_shell(self, script: str) -> list[str]:
        """Shell on the host that runs the passive observer/probe."""
        if self.name == "sim":
            return self.container_shell(script)
        prelude = (f"source /opt/ros/jazzy/setup.bash && source {PI_WS}/install/setup.bash"
                   f" && source {PI_WS}/setup/setup_hil.bash && cd {PI_WS} && ")
        return ["ssh", *SSH_OPTIONS, f"iii@{self.pi_peer}", "bash -lc " + shlex.quote(prelude + script)]

    def observer_python(self) -> str:
        # The Pi observer historically ran from the runtime venv; the probe
        # needs the system cv2/cv_bridge.
        return f"{PI_WS}/.venv/bin/python" if self.name == "hil" else "python3"


def discover_container() -> str:
    result = run(["docker", "ps", "--filter", f"label=devcontainer.local_folder={ROOT}",
                  "--format", "{{.ID}}"])
    ids = [line for line in result.stdout.split() if line]
    if len(ids) != 1:
        raise RunnerError(f"expected exactly one workspace devcontainer, found {len(ids)}")
    return ids[0]


def require_stack_ready(target_name: str, host: str | None) -> str | None:
    """Return the HIL Pi peer, or None for SIM. Raises if the stack is not ready."""
    if target_name == "sim":
        result = run([str(ROOT / "iii-dev"), "stack", "status"], check=False, timeout=120)
        if result.returncode != 0:
            raise RunnerError("SIM stack is not ready (./iii-dev stack status):\n"
                              + result.stdout + result.stderr)
        return None
    command = [str(ROOT / "iii-dev"), "hil", "status", "--json"]
    if host:
        command += ["--host", host]
    result = run(command, check=False, timeout=300)
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"hil status did not return JSON: {result.stdout}{result.stderr}") from exc
    if not status.get("ready") or status.get("profile") != "hil":
        raise RunnerError(f"HIL stack is not ready: state={status.get('state')!r}")
    peer = (status.get("pi_target") or {}).get("peer_ipv4")
    if not peer:
        raise RunnerError("HIL status did not report pi_target.peer_ipv4")
    return peer


def source_identity() -> dict[str, Any]:
    def head(path: Path) -> dict[str, Any]:
        sha = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
        branch = run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        dirty = bool(run(["git", "-C", str(path), "status", "--porcelain",
                          "--ignore-submodules=all", "--untracked-files=no"]).stdout.strip())
        return {"sha": sha, "branch": branch, "dirty": dirty}

    identity = {"workspace": head(ROOT), "submodules": {}}
    listing = run(["git", "-C", str(ROOT), "submodule", "status"]).stdout
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (ROOT / parts[1] / ".git").exists():
            identity["submodules"][parts[1]] = head(ROOT / parts[1])
    return identity


# --------------------------------------------------------------------------
# Execution


def wait_for(predicate: Callable[[], bool], timeout_sec: float, poll_sec: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_sec)
    return predicate()


def fresh_start_commands(target_name: str, host: str | None) -> list[tuple[list[str], bool]]:
    """Commands (and whether each must succeed) that begin a fresh epoch.

    SIM stops the whole stack first: `stack start --recreate-sim` alone
    recreates PX4/Gazebo but keeps an already booted III graph, which would
    carry ground estimates and retained owners across simulation epochs.
    """
    iii_dev = str(ROOT / "iii-dev")
    if target_name == "sim":
        return [([iii_dev, "stack", "stop"], False),
                ([iii_dev, "stack", "start", "--headless", "--recreate-sim"], True)]
    command = [iii_dev, "hil", "restart", "--headless"]
    return [(command + (["--host", host] if host else []), True)]


def execute(args: argparse.Namespace) -> Path:
    if args.fresh_start:
        # A previous run may have left the vehicle away from its spawn pose;
        # takeoff-relative ground estimates then start from the wrong place.
        for command, required in fresh_start_commands(args.target, args.host):
            print(f"[endurance] fresh start: {' '.join(command[1:])}", flush=True)
            result = run(command, check=False, timeout=1200)
            if result.returncode != 0 and required:
                raise RunnerError(f"fresh start failed ({result.returncode}):\n"
                                  f"{result.stdout[-2000:]}{result.stderr[-2000:]}")
    peer = require_stack_ready(args.target, args.host)
    target = Target(args.target, discover_container(), peer)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (args.run_dir or ROOT / "runtime" / "endurance" / f"{args.target}-{stamp}").resolve()
    rel = run_dir.relative_to(ROOT)
    run_dir.mkdir(parents=True, exist_ok=False)
    remote_dir = f"{PI_WS if target.name == 'hil' else CONTAINER_WS}/{rel}"

    plan = {
        "started_at": utc_now(), "target": target.name, "pi_peer": peer,
        "duration_sec": args.duration_sec, "required_cycles": args.required_cycles,
        "source_identity": source_identity(),
    }
    (run_dir / "run_plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(f"[endurance] {target.name} run -> {rel}", flush=True)

    scripts = ROOT / "scripts" / "workspace"
    if target.name == "hil":
        run(["ssh", *SSH_OPTIONS, f"iii@{peer}", f"mkdir -p {shlex.quote(remote_dir)}"])
        run(["scp", "-q", *SSH_OPTIONS, str(scripts / "observe_hil_mission_lifecycle.py"),
             str(scripts / "hil_perception_probe.py"), f"iii@{peer}:{remote_dir}/"], timeout=60)
        observer_src = f"{remote_dir}/observe_hil_mission_lifecycle.py"
        probe_src = f"{remote_dir}/hil_perception_probe.py"
    else:
        observer_src = f"{CONTAINER_WS}/scripts/workspace/observe_hil_mission_lifecycle.py"
        probe_src = f"{CONTAINER_WS}/scripts/workspace/hil_perception_probe.py"
    # A marker for "logs written during this run" on the observer host.
    marker = f"{remote_dir}/.run_started"

    observer_cmd = (
        f"touch {marker} && printf '%s\\n' $$ > {remote_dir}/observer.pid && exec "
        f"{target.observer_python()} {observer_src} --artifact-dir {remote_dir}"
        f" --duration-sec {args.duration_sec} --required-cycles {args.required_cycles}"
        f" --prelude-timeout-sec {OBSERVER_PRELUDE_TIMEOUT_SEC} --final-safe-grace-sec 600"
        f" > {remote_dir}/observer.log 2>&1")
    probe_cmd = (
        f"printf '%s\\n' $$ > {remote_dir}/probe.pid && exec /usr/bin/python3 {probe_src}"
        f" --artifact-dir {remote_dir} --duration-sec {args.duration_sec + 600}"
        f" > {remote_dir}/probe.log 2>&1")
    local_log = lambda name: open(run_dir / name, "w")  # noqa: E731
    observer = subprocess.Popen(target.observer_shell(observer_cmd), stdout=local_log("observer_runner.log"),
                                stderr=subprocess.STDOUT)
    probe = None if args.no_probe else subprocess.Popen(
        target.observer_shell(probe_cmd), stdout=local_log("probe_runner.log"), stderr=subprocess.STDOUT)

    def pid_published(name: str) -> bool:
        return run(target.observer_shell(f"test -s {remote_dir}/{name}"), check=False, timeout=30).returncode == 0

    def interrupt(name: str) -> None:
        run(target.observer_shell(
            f"pid=$(cat {remote_dir}/{name} 2>/dev/null); test -z \"$pid\" || kill -INT \"$pid\" 2>/dev/null || true"),
            check=False, timeout=30)

    ready = wait_for(lambda: observer.poll() is None and pid_published("observer.pid"), PROCESS_READY_TIMEOUT_SEC)
    if ready and probe is not None:
        ready = wait_for(lambda: probe.poll() is None and pid_published("probe.pid"), PROCESS_READY_TIMEOUT_SEC)
    if not ready:
        for name in ("observer.pid", "probe.pid"):
            interrupt(name)
        for process in (observer, probe):
            if process is not None:
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
        raise RunnerError(f"observer/probe did not start; see {rel}/observer_runner.log")

    driver_env = {"III_HIL_PI_ENDPOINT": peer} if target.name == "hil" else {}
    driver_cmd = (
        f"python3 scripts/workspace/hil_inspection_cycle_driver.py --artifact-dir {CONTAINER_WS}/{rel}"
        f" --duration-sec {args.duration_sec} --deadline-drain-guard-sec 180 --automatic-cycles"
        f" --automatic-recharge-timeout-sec 600 --charging-dwell-sec 0 --charge-until-full-timeout-sec 90")
    print("[endurance] driver running", flush=True)
    with open(run_dir / "driver.log", "w") as driver_log:
        driver_rc = subprocess.run(target.container_shell(driver_cmd, driver_env),
                                   stdout=driver_log, stderr=subprocess.STDOUT, check=False).returncode
    print(f"[endurance] driver exit {driver_rc}", flush=True)
    if driver_rc != 0:
        interrupt("observer.pid")
    interrupt("probe.pid")
    observer_rc = observer.wait(timeout=args.duration_sec + 1200)
    probe_rc = probe.wait(timeout=300) if probe is not None else 0

    execution: dict[str, Any] = {"driver_exit": driver_rc, "observer_exit": observer_rc,
                                 "probe_exit": probe_rc, "finished_at": utc_now()}
    if target.name == "hil":
        sync = run(["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTIONS),
                    f"iii@{peer}:{remote_dir}/", f"{run_dir}/"], check=False, timeout=600)
        execution["evidence_sync_exit"] = sync.returncode
    (run_dir / "execution_result.json").write_text(json.dumps(execution, indent=2) + "\n")
    return run_dir


# --------------------------------------------------------------------------
# Acceptance


LOG_LINE = re.compile(r"^\[(ERROR|WARN|FATAL)\] \[(\d+)\.\d+\] \[([^\]]+)\]: (.*)$")


def message_pattern(message: str) -> str:
    """Collapse run-specific numbers/ids so repeated diagnostics group together."""
    pattern = re.sub(r"0x[0-9a-fA-F]+", "0x#", message)
    pattern = re.sub(r"[0-9a-f]{8,}", "#", pattern)
    return re.sub(r"-?\d+(\.\d+)?", "#", pattern)[:240]


def summarize_log_lines(lines: list[str], since_epoch: float, until_epoch: float) -> dict[str, Any]:
    """Group ERROR/WARN/FATAL ROS log lines emitted inside [since, until]."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    counts = {"ERROR": 0, "WARN": 0, "FATAL": 0}
    markers = {marker: 0 for marker in CONTINUITY_FAULT_MARKERS}
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        # Nodes write each line to several files (process, per-pid, current);
        # the nanosecond stamp makes identical lines one event.
        if line in seen:
            continue
        seen.add(line)
        match = LOG_LINE.match(line)
        if not match:
            continue
        level, stamp, node, message = match.groups()
        if not since_epoch <= int(stamp) <= until_epoch:
            continue
        counts[level] += 1
        for marker in markers:
            if marker in message:
                markers[marker] += 1
        key = (level, node, message_pattern(message))
        group = groups.setdefault(key, {"level": level, "node": node, "pattern": key[2],
                                        "count": 0, "example": message[:400]})
        group["count"] += 1
    ordered = sorted(groups.values(), key=lambda g: (g["level"] != "FATAL", g["level"] != "ERROR", -g["count"]))
    return {"counts": counts, "continuity_markers": markers, "patterns": ordered}


def collect_log_lines(target: Target, run_dir: Path) -> list[str]:
    """ERROR/WARN/FATAL lines from ROS node logs written during the run."""
    rel = run_dir.relative_to(ROOT)
    base = PI_WS if target.name == "hil" else CONTAINER_WS
    script = (f"find {base}/runtime_logs/{target.name} -name '*.log' -newer {base}/{rel}/.run_started"
              f" -print0 2>/dev/null | xargs -0 -r grep -h -E '^\\[(ERROR|WARN|FATAL)\\]' | head -n 200000 || true")
    result = run(target.observer_shell(script), check=False, timeout=300)
    return result.stdout.splitlines()


def fetch_vehicle_status(host: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{host}:8765/vehicle/status", timeout=10) as response:
        return json.load(response)


def evaluate(run_dir: Path, *, target: Target | None = None, strict_warnings: bool = False,
             fetch_status: Callable[[str], dict[str, Any]] = fetch_vehicle_status,
             log_lines: Callable[[Target, Path], list[str]] = collect_log_lines
             ) -> dict[str, Any]:
    """Judge a run. Every failed check is listed; nothing short-circuits."""
    plan = json.loads((run_dir / "run_plan.json").read_text())
    duration = plan["duration_sec"]
    required = plan["required_cycles"]
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    def load(name: str) -> Any:
        path = run_dir / name
        if not path.exists():
            failures.append(f"missing {name}")
            return None
        return json.loads(path.read_text())

    execution = load("execution_result.json") or {}
    for key in ("driver_exit", "observer_exit", "probe_exit", "evidence_sync_exit"):
        if key in execution:
            check(execution[key] == 0, f"{key}={execution[key]}")
    observer = load("mission_lifecycle_observation.json") or {}
    events = load("driver_events.json") or []

    check(observer.get("mission_outcome") == "duration_elapsed",
          f"mission_outcome={observer.get('mission_outcome')!r}")
    check(not observer.get("mission_failures"), f"mission_failures={observer.get('mission_failures')}")
    check(not observer.get("mode_failures"), f"mode_failures={observer.get('mode_failures')}")
    check(observer.get("mission_failure_latched") is False, "mission failure latched")
    check(observer.get("cleanup_outcome") == "safe_landed_disarmed",
          f"cleanup_outcome={observer.get('cleanup_outcome')!r}")

    # Only cycles whose Inspection resumption was received inside the
    # duration window count; the observer keeps watching past the deadline.
    window_end = observer.get("duration_elapsed_at")
    if not window_end and observer.get("mission_started_at"):
        window_end = (datetime.fromisoformat(observer["mission_started_at"]).timestamp() + duration)
        window_end = datetime.fromtimestamp(window_end, timezone.utc).isoformat()
    check(bool(window_end), "observer reported no mission start/deadline")
    completed = observer.get("completed_cycles") or []
    in_window = [cycle for cycle in completed
                 if window_end and cycle.get("inspection_resumed_receipt_at")
                 and datetime.fromisoformat(cycle["inspection_resumed_receipt_at"])
                 <= datetime.fromisoformat(window_end)]
    check(len(in_window) >= required, f"in-window cycles {len(in_window)} < {required}")
    cycles = []
    for index, cycle in enumerate(in_window, 1):
        entry: dict[str, Any] = {"cycle": index, "inspection_resumed_receipt_at": cycle.get("inspection_resumed_receipt_at")}
        try:
            charging = (datetime.fromisoformat(cycle["leave_cable_succeeded_activation_receipt_at"])
                        - datetime.fromisoformat(cycle["cable_charging_active_at"])).total_seconds()
            entry["charging_to_leave_sec"] = charging
            check(0 < charging <= MAX_CHARGING_TO_LEAVE_SEC, f"cycle {index} charging_to_leave={charging:.1f}s")
        except (KeyError, TypeError, ValueError):
            failures.append(f"cycle {index} missing charging/leave timestamps")
        cycles.append(entry)

    names = [event.get("event") for event in events]
    check("inspection_recharge_commanded" not in names and "leave_cable_commanded" not in names,
          "manual recharge/leave intent used")
    charges = [event for event in events if event.get("event") == "charging_full_verified"]
    check(len(charges) >= len(in_window), f"full-charge proofs {len(charges)} < cycles {len(in_window)}")
    for event in charges:
        evidence = event.get("evidence", {})
        check(evidence.get("battery_remaining", 0) >= 0.98, f"charge below 0.98: {evidence.get('battery_remaining')}")
        check(0 <= evidence.get("battery_age_sec", 99) <= 2, f"stale battery proof: {evidence.get('battery_age_sec')}")
    cleanup_events = [event for event in events if event.get("event") == "final_safe_landed_disarmed"]
    check(bool(cleanup_events) and cleanup_events[-1].get("cleanup_safety_evidence", {}).get("safe_landed_disarmed") is True,
          "driver did not prove final landed/disarmed")

    report: dict[str, Any] = {"evaluated_at": utc_now(), "target": plan["target"],
                              "duration_sec": duration, "required_cycles": required,
                              "active_duration_sec": observer.get("active_duration_sec"),
                              "completed_cycles_total": len(completed),
                              "completed_cycles_in_window": len(in_window), "cycles": cycles,
                              "full_charge_proofs": len(charges)}
    if target is not None:
        since = datetime.fromisoformat(plan["started_at"]).timestamp()
        until = datetime.fromisoformat(execution.get("finished_at", utc_now())).timestamp()
        findings = summarize_log_lines(log_lines(target, run_dir), since, until)
        (run_dir / "log_findings.json").write_text(json.dumps(findings, indent=2) + "\n")
        report["log_counts"] = findings["counts"]
        report["continuity_faults"] = findings["continuity_markers"]
        report["log_patterns"] = [{k: g[k] for k in ("level", "node", "count", "pattern")}
                                  for g in findings["patterns"][:40]]
        check(all(count == 0 for count in findings["continuity_markers"].values()),
              f"continuity faults {findings['continuity_markers']}")
        check(findings["counts"]["ERROR"] == 0 and findings["counts"]["FATAL"] == 0,
              f"node logs contain {findings['counts']['ERROR']} ERROR / {findings['counts']['FATAL']} FATAL lines")
        if strict_warnings:
            check(findings["counts"]["WARN"] == 0, f"node logs contain {findings['counts']['WARN']} WARN lines")
        try:
            vehicle = fetch_status(target.api_host)
            latest = vehicle.get("latest", {})
            safe = vehicle.get("freshness") == "fresh" and all(
                latest.get(key, {}).get("armed") is False and latest.get(key, {}).get("in_air") is False
                for key in ("command_transport", "ros_uxrce"))
            report["final_vehicle_safe"] = safe
            check(safe, "final Runtime API status is not fresh landed/disarmed")
        except OSError as exc:
            failures.append(f"final Runtime API status unavailable: {exc}")

    report["failures"] = failures
    report["accepted"] = not failures
    (run_dir / "acceptance_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=["sim", "hil"])
    parser.add_argument("--duration-sec", type=int, default=1800)
    parser.add_argument("--required-cycles", type=int, default=4)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--host", help="HIL Pi target override (as for ./iii-dev hil --host)")
    parser.add_argument("--no-probe", action="store_true", help="skip the passive perception probe")
    parser.add_argument("--fresh-start", action="store_true",
                        help="recreate the simulation epoch first (SIM: stack start --recreate-sim, HIL: hil restart)")
    parser.add_argument("--evaluate-only", type=Path, metavar="RUN_DIR")
    parser.add_argument("--strict-warnings", action="store_true",
                        help="also fail on any WARN line in node logs during the run")
    args = parser.parse_args(argv)
    try:
        if args.evaluate_only:
            run_dir = args.evaluate_only.resolve()
            plan = json.loads((run_dir / "run_plan.json").read_text())
            target = Target(plan["target"], discover_container(), plan.get("pi_peer"))
        else:
            if not args.target:
                parser.error("--target is required unless --evaluate-only is given")
            run_dir = execute(args)
            plan = json.loads((run_dir / "run_plan.json").read_text())
            target = Target(plan["target"], discover_container(), plan.get("pi_peer"))
        report = evaluate(run_dir, target=target, strict_warnings=args.strict_warnings)
    except (RunnerError, subprocess.SubprocessError, OSError) as exc:
        print(f"[endurance] error: {exc}", file=sys.stderr)
        return 2
    summary = {key: report[key] for key in ("accepted", "target", "active_duration_sec",
                                            "completed_cycles_in_window", "full_charge_proofs")}
    print(json.dumps(summary))
    for failure in report["failures"]:
        print(f"[endurance] FAIL: {failure}", file=sys.stderr)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
