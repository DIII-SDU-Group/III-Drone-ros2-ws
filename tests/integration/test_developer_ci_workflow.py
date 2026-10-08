from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def test_ci_retains_only_developer_integrity_gates() -> None:
    workflow = (WORKFLOWS / "dependency-governance.yml").read_text(encoding="utf-8")

    for retired_reference in (
        "verify_promotion_source.py",
        "deployment[test]",
        "iii verify deployment",
        "iii docs check",
    ):
        assert retired_reference not in workflow

    for active_gate in (
        "verify_submodule_lock.sh",
        "python -m pip install --disable-pip-version-check pytest==8.3.5",
        "test_submodule_lock_scripts.py",
        "verify_iii_submodule_branch_policy_ci.sh",
    ):
        assert active_gate in workflow

    for retired_workflow in (
        "qualified-release.yml",
        "release-status.yml",
        "refresh-submodule-pointers.yml",
    ):
        assert not (WORKFLOWS / retired_workflow).exists()


def test_ci_runs_the_curated_iii_suite_without_px4() -> None:
    workflow = (WORKFLOWS / "iii-tests.yml").read_text(encoding="utf-8")

    assert "./scripts/workspace/run_iii_test_suite.sh" in workflow
    assert "grep -vx PX4-Autopilot" in workflow
    assert "submodules: recursive" not in workflow
