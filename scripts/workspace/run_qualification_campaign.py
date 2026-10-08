#!/usr/bin/env python3
"""Qualify one commit: SIM endurance, then deploy, then HIL endurance.

This is the gated form of the SIM-before-HIL qualification loop. Stages run in
order and the campaign stops at the first failure:

1. `sim`: fresh SIM stack, strict endurance run (`run_inspection_endurance.py`);
2. `sim_stop`: stop the SIM stack so HIL owns the workstation PX4 SITL;
3. `deploy`: `iii deploy dev --host HOST --build --restart` (workstation
   cross-build and direct synchronization to the Pi);
4. `hil`: fresh HIL restart, strict endurance run, including the physical
   PX4 disarm record;
5. `hil_stop`: always stop HIL afterwards (unless `--keep-hil`).

The result is `campaign_report.json` in the campaign directory, recorded
against the exact source identity. The campaign refuses a dirty tracked tree
unless `--allow-dirty` is given, because a qualification must name a commit.
It never arms or commands the vehicle itself.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_inspection_endurance as endurance  # noqa: E402

ROOT = endurance.ROOT
STAGE_TIMEOUT_SEC = {"sim_stop": 900, "deploy": 3600, "hil_stop": 900}


def dirty_repositories(identity: dict[str, Any]) -> list[str]:
    dirty = ["."] if identity["workspace"]["dirty"] else []
    return dirty + sorted(path for path, head in identity["submodules"].items() if head["dirty"])


def stage_commands(args: argparse.Namespace, campaign_dir: Path) -> list[tuple[str, list[str]]]:
    python = sys.executable
    runner = str(ROOT / "scripts" / "workspace" / "run_inspection_endurance.py")
    iii_dev = str(ROOT / "iii-dev")

    def endurance_run(target: str) -> list[str]:
        command = [python, runner, "--target", target, "--fresh-start", "--strict-warnings",
                   "--duration-sec", str(args.duration_sec),
                   "--required-cycles", str(args.required_cycles),
                   "--run-dir", str(campaign_dir / target)]
        return command + (["--host", args.host] if target == "hil" else [])

    # `iii` is the host CLI; the HIL setup profile puts it on PATH.
    deploy = ["bash", "-c", f"source {ROOT}/setup/setup_hil.bash >/dev/null 2>&1 && "
              f"exec iii deploy dev --host {args.host} --build --restart"]
    return [("sim", endurance_run("sim")),
            ("sim_stop", [iii_dev, "stack", "stop"]),
            ("deploy", deploy),
            ("hil", endurance_run("hil")),
            ("hil_stop", [iii_dev, "hil", "stop", "--host", args.host])]


def run_stage(name: str, command: list[str], log_path: Path, timeout: float | None) -> int:
    with open(log_path, "w") as log:
        log.write(f"$ {' '.join(command)}\n")
        log.flush()
        try:
            return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                                  timeout=timeout, check=False).returncode
        except subprocess.TimeoutExpired:
            log.write(f"\n[campaign] {name} exceeded {timeout} s\n")
            return 124


def acceptance_summary(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "acceptance_report.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    return {key: report.get(key) for key in (
        "accepted", "active_duration_sec", "completed_cycles_in_window", "full_charge_proofs",
        "log_counts", "recording_lost_messages", "final_vehicle_safe", "physical_px4", "failures")}


def run_campaign(args: argparse.Namespace, *,
                 stage_runner: Callable[[str, list[str], Path, float | None], int] = run_stage,
                 identity: Callable[[], dict[str, Any]] = endurance.source_identity,
                 now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    source = identity()
    dirty = dirty_repositories(source)
    sha = source["workspace"]["sha"][:10]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    campaign_dir = (args.run_dir or ROOT / "runtime" / "qualification" / f"{stamp}-{sha}").resolve()
    campaign_dir.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"started_at": endurance.utc_now(), "source_identity": source,
                              "dirty_repositories": dirty, "duration_sec": args.duration_sec,
                              "required_cycles": args.required_cycles, "host": args.host,
                              "stages": [], "qualified": False}

    def write() -> None:
        (campaign_dir / "campaign_report.json").write_text(json.dumps(report, indent=2) + "\n")

    if dirty and not args.allow_dirty:
        report["failure"] = f"tracked changes in {dirty}; commit first or pass --allow-dirty"
        write()
        return report

    failed = None
    for name, command in stage_commands(args, campaign_dir):
        if name == "hil_stop" and args.keep_hil:
            continue
        # HIL is always stopped once it may have been started, even after a failure.
        if failed is not None and not (name == "hil_stop" and failed in {"deploy", "hil"}):
            continue
        print(f"[campaign] {name} ...", flush=True)
        started = now()
        timeout = None if name in {"sim", "hil"} else STAGE_TIMEOUT_SEC[name]
        rc = stage_runner(name, command, campaign_dir / f"{name}.log", timeout)
        stage: dict[str, Any] = {"stage": name, "exit": rc, "elapsed_sec": round(now() - started, 1)}
        if name in {"sim", "hil"}:
            stage["acceptance"] = acceptance_summary(campaign_dir / name)
            stage["passed"] = rc == 0 and bool(stage["acceptance"] and stage["acceptance"]["accepted"])
        else:
            # A stack that is already stopped is fine before HIL.
            stage["passed"] = rc == 0 or name == "sim_stop"
        report["stages"].append(stage)
        print(f"[campaign] {name}: {'passed' if stage['passed'] else 'FAILED'} "
              f"(exit {rc}, {stage['elapsed_sec']} s)", flush=True)
        if not stage["passed"] and failed is None:
            failed = name
        write()

    report["finished_at"] = endurance.utc_now()
    report["failed_stage"] = failed
    report["qualified"] = failed is None and {s["stage"] for s in report["stages"]} >= {"sim", "deploy", "hil"}
    write()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", required=True, help="HIL Pi address (as for ./iii-dev hil --host)")
    parser.add_argument("--duration-sec", type=int, default=1800)
    parser.add_argument("--required-cycles", type=int, default=4)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--allow-dirty", action="store_true",
                        help="qualify a tree with tracked uncommitted changes (recorded as such)")
    parser.add_argument("--keep-hil", action="store_true", help="leave HIL running afterwards")
    args = parser.parse_args(argv)
    report = run_campaign(args)
    print(json.dumps({"qualified": report["qualified"], "failed_stage": report.get("failed_stage"),
                      "failure": report.get("failure"),
                      "stages": [{k: s.get(k) for k in ("stage", "passed", "elapsed_sec")}
                                 for s in report["stages"]]}))
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    sys.exit(main())
