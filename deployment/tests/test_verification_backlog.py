from __future__ import annotations

import json
from pathlib import Path

import pytest

from iii_deployment.verification.backlog import BacklogError, parse_backlog
from iii_deployment.verification.matrix import clause_baseline, verify_clause_baseline


ROOT = Path(__file__).resolve().parents[2]
BACKLOG = ROOT / "codex-backlogs" / "deployment-infrastructure-redesign.md"
BASELINE = ROOT / "deployment" / "verification" / "clause-baseline.json"


def test_all_decisions_and_tasks_are_parseable() -> None:
    backlog = parse_backlog(BACKLOG)
    assert {clause.decision for clause in backlog.clauses} == {f"Q{i}" for i in range(1, 133)}
    assert "P0.T0" in backlog.tasks
    assert "P5.T6" in backlog.tasks
    assert all(task.acceptance and task.tests for task in backlog.tasks.values())


def test_clause_baseline_is_current() -> None:
    backlog = parse_backlog(BACKLOG)
    assert verify_clause_baseline(backlog, BASELINE) == []


def test_clause_change_requires_reviewed_baseline(tmp_path: Path) -> None:
    backlog = parse_backlog(BACKLOG)
    baseline = clause_baseline(backlog)
    baseline["clauses"][0]["text"] += " drift"
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    errors = verify_clause_baseline(backlog, path)
    assert errors and "Q1.c1" in errors[0]


def test_unknown_coverage_owner_is_rejected(tmp_path: Path) -> None:
    source = BACKLOG.read_text(encoding="utf-8").replace(
        "| Q1 | P0.T2, P3.T5, P5.T1 |", "| Q1 | P9.T9 |"
    )
    path = tmp_path / "backlog.md"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(BacklogError, match="unknown owner task P9.T9"):
        parse_backlog(path)

