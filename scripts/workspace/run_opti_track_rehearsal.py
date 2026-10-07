#!/usr/bin/env python3
"""Run and judge the OptiTrack rehearsal in SIM.

The rehearsal flies the `opti_track` runtime profile against PX4 SITL with a
vision-only estimator and the simulated lab gateway (see
tools/simulation/opti_track_rehearsal.sh). It exercises what the lab session
relies on: profile restrictions, the motion-capture readiness gate, and the
four OptiTrack missions, each to a landed and disarmed aircraft.

Usage (on the host, from the workspace root):
    scripts/workspace/run_opti_track_rehearsal.py [--keep-running] [--no-start]

The verdict is written to runtime/rehearsal/<run>/report.json; the exit status
is 0 only when every scenario passed and the node logs hold no unexpected
WARN, ERROR or FATAL line.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_WS = "/home/iii/ws"
API = "http://127.0.0.1:8765"
ENVIRONMENT = f"{CONTAINER_WS}/tools/simulation/opti_track_rehearsal.sh"
# Designed warnings: selecting a mission that is not field-qualified yet, and
# the relay reporting the pose outage this rehearsal injects.
EXPECTED_WARNINGS = (
    re.compile(r"EXPERIMENTAL mission catalog entry selected"),
    re.compile(r"OptiTrackPoseRelayNode::onHealthTimer\(\): pose stale"),
)
# Samples the recorder loses on best-effort streams are a recording-completeness
# metric, reported separately (as in run_inspection_endurance.py).
RECORDING_LOSS = re.compile(r"\[rosbag2_recorder\]: Number of messages lost on the transport layer: (\d+)")


class RehearsalError(RuntimeError):
    pass


def container_id() -> str:
    result = subprocess.run(
        ["docker", "ps", "--filter", f"label=devcontainer.local_folder={ROOT}", "--format", "{{.ID}}"],
        text=True, capture_output=True, check=True,
    )
    identifiers = result.stdout.split()
    if len(identifiers) != 1:
        raise RehearsalError(f"expected one running devcontainer for {ROOT}, found {len(identifiers)}")
    return identifiers[0]


def in_container(
    container: str, command: str, *, timeout: float = 600.0, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", "-i", "--user", "iii", container, "bash", "-lc", command],
        text=True, capture_output=True, timeout=timeout, check=False, input=stdin or "",
    )


def get(path: str) -> Any:
    with urlopen(Request(f"{API}{path}", headers={"Accept": "application/json"}), timeout=10) as response:
        return json.loads(response.read())


def command(command_id: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({
        "request_id": f"rehearsal-{uuid4()}",
        "command_id": command_id,
        "client_label": "opti-track-rehearsal",
        "parameters": parameters or {},
    }).encode()
    request = Request(f"{API}/cli/commands", data=body, method="POST",
                      headers={"Accept": "application/json", "Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def accepted(command_id: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    response = command(command_id, parameters)
    if response.get("accepted") is not True:
        raise RehearsalError(f"{command_id} rejected: {response.get('message')}")
    return response


def rejected(command_id: str, parameters: dict[str, Any], expected: str) -> str:
    response = command(command_id, parameters)
    message = str(response.get("message"))
    if response.get("accepted") is not False or expected not in message:
        raise RehearsalError(f"{command_id} should be rejected with '{expected}', got: {response}")
    return message


def vehicle() -> dict[str, Any]:
    return get("/cli/vehicle/status")


def mission() -> dict[str, Any]:
    return get("/mission/status")


def wait_for(description: str, predicate: Callable[[], Any], timeout: float, period: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (URLError, OSError, KeyError, ValueError) as exc:
            last_error = exc
        time.sleep(period)
    raise RehearsalError(f"timed out after {timeout:.0f} s waiting for {description}"
                         + (f" (last error: {last_error})" if last_error else ""))


def landed_and_disarmed() -> bool:
    state = vehicle()
    return state["freshness"] == "fresh" and state["armed"] is False and state["in_air"] is False


def airborne_in(nav_state: str) -> bool:
    state = vehicle()
    return state["armed"] is True and state["in_air"] is True and state["nav_state"] == nav_state


def vision_ready() -> bool:
    vision = vehicle().get("external_vision") or {}
    return vision.get("ready") is True


def runtime_settled() -> bool:
    """Every preflight gate but the air state passes (the default mission starts airborne)."""
    items = mission()["preflight"]["items"]
    settled = all(item["passed"] for item in items if item["hard_gate"] and item["key"] != "air_state")
    operation = get("/operations/status")
    return settled and operation.get("freshness") == "fresh"


def mode(mode_key: str) -> dict[str, Any]:
    for entry in mission()["modes"]:
        if entry["mode_key"] == mode_key:
            return entry
    raise KeyError(mode_key)


def preflight_ready() -> bool:
    return mission()["preflight"]["ready"] is True


def select(catalog_id: str, entry_mode: str, *, ground_start: bool = True) -> None:
    wait_for("a landed, disarmed aircraft", landed_and_disarmed, 60)
    accepted("mission.catalog.select", {"catalog_id": catalog_id})

    def ready() -> bool:
        state = mission()
        return (state["active_spec_id"] == catalog_id and state["required_modes_registered"] is True
                and state["latest"].get("owned_mode") == entry_mode
                and state["latest"].get("activation_allowed") is True)

    wait_for(f"{catalog_id} ready with entry mode {entry_mode}", ready, 60)
    if ground_start:
        wait_for("the mission preflight", preflight_ready, 60)


def land() -> None:
    accepted("px4.land")
    wait_for("landing and disarm", landed_and_disarmed, 90)


def take_off_to_hold(altitude_m: float) -> None:
    accepted("px4.arm")
    # Both PX4 links must agree that the aircraft is armed before the takeoff.
    wait_for("arming", lambda: vehicle()["armed"] is True and vehicle()["degraded_reason"] is None, 15)
    time.sleep(1.0)
    accepted("px4.takeoff", {"altitude_m": altitude_m})
    wait_for("the pilot hover (PX4 Hold) after takeoff", lambda: airborne_in("hold"), 60)
    # Let the hover settle, as a pilot would before handing over.
    time.sleep(5.0)


# --- scenarios -----------------------------------------------------------------

def restrictions() -> dict[str, Any]:
    """The profile refuses the canonical mission and cable-only commands."""
    return {
        "canonical_mission": rejected("mission.catalog.select", {"catalog_id": "inspection-production"},
                                      "not available in the opti_track profile"),
        "cable_intent": rejected("mission.recharge_now", {}, "not available in the opti_track profile"),
    }


def motion_capture_gate(container: str) -> dict[str, Any]:
    """A pose outage on the ground closes the readiness gate; it reopens by itself."""
    select("opti-track-cycle", "ot_cycle_takeoff")
    result = in_container(
        container,
        f"source {CONTAINER_WS}/setup/setup_dev.bash && "
        "ros2 param set /simulated_lab_mocap_gateway one_shot_dropout_s 8.0",
        timeout=60,
    )
    if result.returncode != 0:
        raise RehearsalError(f"could not inject the pose outage: {result.stderr.strip()}")
    wait_for("the readiness gate to close", lambda: not vision_ready(), 10, period=0.2)
    message = str(command("mission.activate", {"mode_key": "ot_cycle_takeoff"}).get("message"))
    if vehicle()["armed"] is not False:
        raise RehearsalError("the aircraft armed during a motion-capture outage")
    wait_for("the readiness gate to reopen", vision_ready, 30)
    wait_for("the mission preflight after the outage", lambda: mission()["preflight"]["ready"] is True, 30)
    return {"activation_during_outage": message}


def cycle(proceed: bool) -> dict[str, Any]:
    """M3: ground start, takeoff, Proceed (or its 60 s timeout), land, disarm."""
    select("opti-track-cycle", "ot_cycle_takeoff")
    accepted("mission.activate", {"mode_key": "ot_cycle_takeoff"})
    wait_for("OT Takeoff airborne", lambda: airborne_in("mission") and mode("ot_cycle_takeoff")["active"], 60)
    if proceed:
        wait_for("the hover that waits for Proceed",
                 lambda: mode("ot_cycle_takeoff")["tree_running"], 30)
        time.sleep(8.0)
        accepted("mission.proceed")
        wait_for("OT Shuttle", lambda: mode("ot_cycle_shuttle")["active"], 30)
    # OT Land lasts a few seconds; its result, not its activity, is the proof.
    wait_for("landing and disarm by the mission", landed_and_disarmed, 240)
    landing = wait_for("the OT Land result", lambda: mode("ot_cycle_land").get("tree_finished") and mode("ot_cycle_land"), 15)
    if landing.get("tree_success") is not True:
        raise RehearsalError("OT Land did not finish successfully")
    return {"proceed": proceed}


def pilot_handover(catalog_id: str, mode_key: str, timeout: float) -> dict[str, Any]:
    """M1/M2: take over from a pilot hover, run the tree, hand back to PX4 Hold."""
    select(catalog_id, mode_key, ground_start=False)
    take_off_to_hold(1.4)
    wait_for("the mission preflight in the pilot hover", preflight_ready, 60)
    accepted("mission.activate", {"mode_key": mode_key})
    wait_for(f"{mode_key} active", lambda: airborne_in("mission") and mode(mode_key)["active"], 30)
    wait_for(f"{mode_key} handing back to PX4 Hold", lambda: airborne_in("hold"), timeout)
    finished = wait_for(f"the {mode_key} result", lambda: mode(mode_key).get("tree_finished") and mode(mode_key), 15)
    if finished.get("tree_success") is not True:
        raise RehearsalError(f"{mode_key} did not finish successfully")
    land()
    return {"mode": mode_key}


def mode_loop(loops: int) -> dict[str, Any]:
    """M4: ground start, then the endless four-mode loop until the operator's Hold."""
    order = ["otl_lower_stop", "otl_upper_stop", "otl_lower_blend", "otl_upper_blend"]
    select("opti-track-mode-loop", "otl_takeoff")
    accepted("mission.activate", {"mode_key": "otl_takeoff"})
    visited: list[str] = []
    for _ in range(loops):
        for mode_key in order:
            wait_for(f"{mode_key} (after {visited[-1] if visited else 'takeoff'})",
                     lambda key=mode_key: mode(key)["active"], 180)
            if visited and mode(visited[-1]).get("tree_success") is not True:
                raise RehearsalError(f"{visited[-1]} did not finish successfully")
            visited.append(mode_key)
    # The loop repeats: the first mode must come round again.
    wait_for("the loop to restart", lambda: mode(order[0])["active"], 180)
    accepted("px4.hold")
    wait_for("PX4 Hold after the operator's Hold", lambda: airborne_in("hold"), 30)
    land()
    return {"visited": visited}


# --- judging -------------------------------------------------------------------

def log_findings(container: str, since_epoch: float) -> tuple[list[dict[str, Any]], int]:
    script = (
        "import json,os,re,sys\n"
        f"root='{CONTAINER_WS}/runtime_logs/opti_track'; since={since_epoch}\n"
        "level=re.compile(r'\\[(WARN|ERROR|FATAL)\\] \\[(\\d+\\.\\d+)\\]'); out=[]; seen=set()\n"
        "for d,_,fs in os.walk(root):\n"
        "  for f in fs:\n"
        "    p=os.path.join(d,f)\n"
        "    if os.path.getmtime(p)<since: continue\n"
        "    for line in open(p,errors='replace'):\n"
        "      m=level.search(line)\n"
        "      if not m or float(m.group(2))<since or line in seen: continue\n"
        "      seen.add(line)\n"
        "      out.append({'node':os.path.basename(d),'level':m.group(1),'line':line.strip()[:400]})\n"
        "print(json.dumps(out))\n"
    )
    result = in_container(container, "python3 -", timeout=120, stdin=script)
    if result.returncode != 0:
        raise RehearsalError(f"could not read the node logs: {result.stderr.strip()}")
    findings = []
    recording_lost_messages = 0
    for item in json.loads(result.stdout):
        loss = RECORDING_LOSS.search(item["line"])
        if loss:
            recording_lost_messages += int(loss.group(1))
        elif not any(pattern.search(item["line"]) for pattern in EXPECTED_WARNINGS):
            findings.append(item)
    return findings, recording_lost_messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-start", action="store_true", help="use the rehearsal environment that is already running")
    parser.add_argument("--keep-running", action="store_true", help="leave the environment up afterwards")
    parser.add_argument("--loops", type=int, default=2, help="mode-loop rounds to observe (default 2)")
    args = parser.parse_args(argv)

    run_dir = ROOT / "runtime" / "rehearsal" / datetime.now(timezone.utc).strftime("sim-%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)
    container = container_id()
    started = time.time()
    report: dict[str, Any] = {"run": run_dir.name, "scenarios": [], "accepted": False}

    scenarios: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("profile restrictions", restrictions),
        ("motion-capture readiness gate", lambda: motion_capture_gate(container)),
        ("M3 cycle with Proceed", lambda: cycle(True)),
        ("M3 cycle without Proceed", lambda: cycle(False)),
        ("M1 hover from a pilot hover", lambda: pilot_handover("opti-track-hover", "ot_hover", 120)),
        ("M2 maneuvers from a pilot hover", lambda: pilot_handover("opti-track-maneuvers", "ot_maneuvers", 600)),
        ("M4 mode loop", lambda: mode_loop(args.loops)),
    ]
    try:
        if not args.no_start:
            print("[rehearsal] starting the environment", flush=True)
            result = in_container(container, f"{ENVIRONMENT} start", timeout=900)
            (run_dir / "environment_start.log").write_text(result.stdout + result.stderr)
            if result.returncode != 0:
                raise RehearsalError(f"environment start failed; see {run_dir / 'environment_start.log'}")
        identity = get("/identity")
        if identity.get("profile") != "opti_track":
            raise RehearsalError(f"Runtime API profile is {identity.get('profile')!r}, not opti_track")
        wait_for("motion-capture positioning", vision_ready, 120)
        wait_for("a landed, disarmed aircraft", landed_and_disarmed, 60)
        wait_for("a settled runtime", runtime_settled, 120)
        for name, scenario in scenarios:
            print(f"[rehearsal] {name}", flush=True)
            entry: dict[str, Any] = {"name": name, "passed": False}
            report["scenarios"].append(entry)
            begun = time.monotonic()
            try:
                entry["detail"] = scenario()
                entry["passed"] = True
            except Exception as exc:  # each failure is reported; later scenarios need a known state
                entry["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[rehearsal] FAIL: {name}: {entry['error']}", flush=True)
                break
            finally:
                entry["duration_s"] = round(time.monotonic() - begun, 1)
        report["final_vehicle"] = {key: vehicle().get(key) for key in ("armed", "in_air", "nav_state", "freshness")}
        report["log_findings"], report["recording_lost_messages"] = log_findings(container, started)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[rehearsal] error: {report['error']}", flush=True)
    finally:
        if not args.keep_running and not args.no_start:
            in_container(container, f"{ENVIRONMENT} stop", timeout=300)

    final = report.get("final_vehicle") or {}
    report["accepted"] = bool(
        "error" not in report
        and len(report["scenarios"]) == len(scenarios)
        and all(entry["passed"] for entry in report["scenarios"])
        and final.get("armed") is False and final.get("in_air") is False
        and not report.get("log_findings")
    )
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    for finding in report.get("log_findings", [])[:20]:
        print(f"[rehearsal] log {finding['level']} {finding['node']}: {finding['line'][:200]}")
    print(json.dumps({"accepted": report["accepted"], "run": str(run_dir.relative_to(ROOT)),
                      "scenarios": [(entry["name"], entry["passed"]) for entry in report["scenarios"]],
                      "log_findings": len(report.get("log_findings", []))}))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
