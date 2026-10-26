"""Offline tests for the SIM -> deploy -> HIL qualification campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_qualification_campaign as campaign  # noqa: E402


def identity(dirty_submodule: bool = False) -> dict:
    return {"workspace": {"sha": "8c7b5ab0123456789", "branch": "b", "dirty": False},
            "submodules": {"src/III-Drone-Core": {"sha": "x", "branch": "b", "dirty": dirty_submodule}}}


def args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = {"host": "192.168.1.251", "duration_sec": 1800, "required_cycles": 4,
              "run_dir": tmp_path / "campaign", "allow_dirty": False, "keep_hil": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class Stages:
    def __init__(self, fail: str | None = None, reject: str | None = None) -> None:
        self.fail, self.reject, self.calls = fail, reject, []

    def __call__(self, name: str, command: list[str], log: Path, timeout) -> int:
        self.calls.append((name, command, timeout))
        if name in {"sim", "hil"}:
            run_dir = Path(command[command.index("--run-dir") + 1])
            run_dir.mkdir(parents=True)
            (run_dir / "acceptance_report.json").write_text(json.dumps(
                {"accepted": name != self.reject, "failures": [] if name != self.reject else ["x"]}))
            if name == self.reject:
                return 1
        return 1 if name == self.fail else 0


def test_runs_every_stage_in_order_and_qualifies(tmp_path: Path) -> None:
    stages = Stages()
    report = campaign.run_campaign(args(tmp_path), stage_runner=stages, identity=identity)
    assert [c[0] for c in stages.calls] == ["sim", "sim_stop", "deploy", "hil", "hil_stop"]
    assert report["qualified"] and report["failed_stage"] is None
    sim, hil = stages.calls[0][1], stages.calls[3][1]
    assert "--strict-warnings" in sim and "--fresh-start" in sim and "--host" not in sim
    assert hil[hil.index("--host") + 1] == "192.168.1.251"
    assert stages.calls[0][2] is None and stages.calls[2][2] == 3600
    assert "--build --restart" in stages.calls[2][1][-1]
    saved = json.loads((tmp_path / "campaign" / "campaign_report.json").read_text())
    assert saved["qualified"] is True and len(saved["stages"]) == 5


def test_sim_rejection_stops_before_touching_hil(tmp_path: Path) -> None:
    stages = Stages(reject="sim")
    report = campaign.run_campaign(args(tmp_path), stage_runner=stages, identity=identity)
    assert [c[0] for c in stages.calls] == ["sim"]
    assert not report["qualified"] and report["failed_stage"] == "sim"
    assert report["stages"][0]["acceptance"]["failures"] == ["x"]


def test_hil_failures_still_stop_hil(tmp_path: Path) -> None:
    for failing in ("deploy", "hil"):
        stages = Stages(fail=failing if failing == "deploy" else None,
                        reject=failing if failing == "hil" else None)
        report = campaign.run_campaign(args(tmp_path, run_dir=tmp_path / failing),
                                       stage_runner=stages, identity=identity)
        names = [c[0] for c in stages.calls]
        assert names[-1] == "hil_stop" and report["failed_stage"] == failing
        assert not report["qualified"]


def test_deploy_failure_skips_the_hil_run(tmp_path: Path) -> None:
    stages = Stages(fail="deploy")
    campaign.run_campaign(args(tmp_path), stage_runner=stages, identity=identity)
    assert [c[0] for c in stages.calls] == ["sim", "sim_stop", "deploy", "hil_stop"]


def test_dirty_tree_is_refused_unless_allowed(tmp_path: Path) -> None:
    stages = Stages()
    report = campaign.run_campaign(args(tmp_path), stage_runner=stages,
                                   identity=lambda: identity(dirty_submodule=True))
    assert stages.calls == [] and not report["qualified"]
    assert "src/III-Drone-Core" in report["failure"]
    report = campaign.run_campaign(args(tmp_path, run_dir=tmp_path / "dirty", allow_dirty=True),
                                   stage_runner=stages, identity=lambda: identity(dirty_submodule=True))
    assert report["qualified"] and report["dirty_repositories"] == ["src/III-Drone-Core"]


def test_keep_hil_leaves_hil_running(tmp_path: Path) -> None:
    stages = Stages()
    report = campaign.run_campaign(args(tmp_path, keep_hil=True), stage_runner=stages, identity=identity)
    assert "hil_stop" not in [c[0] for c in stages.calls] and report["qualified"]
