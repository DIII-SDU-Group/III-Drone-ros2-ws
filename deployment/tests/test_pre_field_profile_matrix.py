from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment/scripts/run_pre_field_profile_matrix.py"
RELEASE_ID = "a" * 64


def _fake_iii(tmp_path: Path) -> Path:
    path = tmp_path / "iii-fake"
    path.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['FAKE_III_STATE'])
profile = state.read_text().strip() if state.exists() else 'real'
args = [value for value in sys.argv[1:] if value != '--output=json']
if args[:2] == ['system', 'boot']:
    profile = args[args.index('--profile') + 1]
    state.write_text(profile)
if args[:2] == ['system', 'status'] and profile == 'opti_track' and os.environ.get('FAIL_OPTI_STATUS'):
    value = {'schema':'iii.command-result/v1','outcome':'failed','code':'FAKE_FAIL','context':{'profile':profile,'release_id':'%s'}}
    print(json.dumps(value)); raise SystemExit(30)
value = {'schema':'iii.command-result/v1','outcome':'success','code':'FAKE_OK','context':{'profile':profile,'release_id':'%s'},'payload':{'active_release_id':'%s','profile':profile}}
print(json.dumps(value))
"""
        % (RELEASE_ID, RELEASE_ID, RELEASE_ID),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(tmp_path: Path, *extra: str, failure: bool = False):
    fake = _fake_iii(tmp_path)
    output = tmp_path / "evidence"
    environment = {
        **os.environ,
        "FAKE_III_STATE": str(tmp_path / "profile"),
    }
    if failure:
        environment["FAIL_OPTI_STATUS"] = "1"
    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--iii",
            str(fake),
            "--expected-release-id",
            RELEASE_ID,
            "--output-dir",
            str(output),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return process, output


def test_plan_is_read_only_and_declares_recovery(tmp_path: Path) -> None:
    process, output = _run(tmp_path)
    assert process.returncode == 0
    plan = json.loads(process.stdout)
    assert plan["schema"] == "iii.pre-field-profile-matrix-plan/v1"
    assert plan["mutations"]
    assert ["system", "boot", "--profile", "real"] == plan["recovery"][1][1:-1]
    assert not output.exists()


def test_apply_proves_one_release_and_returns_to_real(tmp_path: Path) -> None:
    process, output = _run(tmp_path, "--apply")
    assert process.returncode == 0, process.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "pass"
    assert report["returned_to_real"] is True
    assert [row["name"] for row in report["steps"]] == [
        "real-readiness-before",
        "real-stop",
        "opti-track-boot",
        "opti-track-status",
        "return-stop",
        "return-real-boot",
        "real-readiness-after",
    ]
    assert (tmp_path / "profile").read_text() == "real"
    assert all((output / row["log"]).is_file() for row in report["steps"])


def test_failure_still_returns_to_real_and_is_not_passed(tmp_path: Path) -> None:
    process, output = _run(tmp_path, "--apply", failure=True)
    assert process.returncode == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "fail"
    assert report["returned_to_real"] is True
    assert "opti-track-status" in report["failure"]
    assert (tmp_path / "profile").read_text() == "real"
