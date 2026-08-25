"""Materialize and validate stable decision-clause traceability."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .backlog import Backlog, BacklogError


MATRIX_SCHEMA = "iii.deployment-verification-matrix/v1"
CLAUSE_BASELINE_SCHEMA = "iii.deployment-clause-baseline/v1"


def _environment(text: str) -> str:
    lower = text.lower()
    physical = ("physical", "raspberry pi", "aircraft", "hardware", "flight", "sd card", "field wlan")
    target = ("arm64", "systemd", "ansible", "ssh", "receiver", "qgroundcontrol", "px4")
    if any(word in lower for word in physical):
        return "physical"
    if any(word in lower for word in target):
        return "target-equivalent"
    return "host-independent"


def materialize(backlog: Backlog) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for clause in backlog.clauses:
        owners = backlog.owners[clause.decision]
        environment = _environment(clause.text)
        rows.append(
            {
                **asdict(clause),
                "owners": list(owners),
                "level": environment,
                "owner_command": "python -m pytest deployment/tests",
                "required_evidence": (
                    "signed-local-acceptance" if environment != "host-independent" else "junit"
                ),
                "blocking": True,
                "latest_result": {"status": "not_run", "evidence": []},
            }
        )
    for task in sorted(backlog.tasks.values(), key=lambda task: task.id):
        for index, acceptance in enumerate(task.acceptance, start=1):
            environment = _environment(acceptance)
            rows.append(
                {
                    "id": f"{task.id}.a{index}",
                    "task": task.id,
                    "title": task.title,
                    "text": acceptance,
                    "digest": __import__("hashlib").sha256(acceptance.encode()).hexdigest(),
                    "owners": [task.id],
                    "level": environment,
                    "owner_command": "python -m pytest deployment/tests",
                    "required_evidence": (
                        "signed-local-acceptance" if environment != "host-independent" else "junit"
                    ),
                    "blocking": True,
                    "latest_result": {"status": "not_run", "evidence": []},
                }
            )
    return {
        "schema": MATRIX_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }


def clause_baseline(backlog: Backlog) -> dict[str, Any]:
    return {
        "schema": CLAUSE_BASELINE_SCHEMA,
        "clauses": [
            {"id": clause.id, "decision": clause.decision, "digest": clause.digest, "text": clause.text}
            for clause in backlog.clauses
        ],
    }


def verify_clause_baseline(backlog: Backlog, baseline_path: Path) -> list[str]:
    expected = clause_baseline(backlog)
    try:
        actual = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read clause baseline: {exc}"]
    if actual == expected:
        return []
    expected_by_id = {row["id"]: row for row in expected["clauses"]}
    actual_by_id = {row.get("id"): row for row in actual.get("clauses", []) if isinstance(row, dict)}
    errors: list[str] = []
    for identifier in sorted(expected_by_id.keys() | actual_by_id.keys()):
        if identifier not in actual_by_id:
            errors.append(f"{identifier}: new clause requires an explicit baseline update/mapping")
        elif identifier not in expected_by_id:
            errors.append(f"{identifier}: removed clause requires an explicit baseline update/mapping")
        elif actual_by_id[identifier] != expected_by_id[identifier]:
            errors.append(f"{identifier}: clause text changed; update the reviewed baseline/mapping")
    return errors


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        __import__("os").fsync(handle.fileno())
    temporary.replace(path)

