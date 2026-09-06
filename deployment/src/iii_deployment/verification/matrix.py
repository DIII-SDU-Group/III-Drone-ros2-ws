"""Materialize and audit stable deployment verification traceability."""

from __future__ import annotations

import hashlib
import fnmatch
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from xml.etree import ElementTree

from iii_deployment.contracts import canonical_json, content_identity

from .backlog import Backlog, BacklogError, Task


MATRIX_SCHEMA = "iii.deployment-verification-matrix/v1"
POLICY_SCHEMA = "iii.deployment-verification-policy/v1"
CLAUSE_BASELINE_SCHEMA = "iii.deployment-clause-baseline/v1"
CLAUSE_MIGRATIONS_SCHEMA = "iii.deployment-clause-migrations/v1"
RESULT_SCHEMA = "iii.deployment-verification-result/v1"


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]+", text.lower())
        if token
        not in {
            "and",
            "are",
            "can",
            "does",
            "every",
            "for",
            "from",
            "has",
            "have",
            "into",
            "its",
            "must",
            "not",
            "only",
            "that",
            "the",
            "their",
            "then",
            "this",
            "through",
            "with",
        }
    }


def _best_reference(text: str, values: Iterable[str], prefix: str) -> str:
    candidates = list(values)
    if not candidates:
        raise BacklogError(f"{prefix} has no candidates")
    source = _tokens(text)
    ranked = sorted(
        enumerate(candidates, start=1),
        key=lambda item: (
            -len(source & _tokens(item[1])),
            -len(source | _tokens(item[1])),
            item[0],
        ),
    )
    return f"{prefix}{ranked[0][0]}"


def _environment(text: str) -> str:
    lower = text.lower()
    physical = (
        "physical",
        "raspberry pi",
        "aircraft",
        "hardware",
        "flight",
        "sd card",
        "field wlan",
        "power interruption",
    )
    target = (
        "arm64",
        "systemd",
        "ansible",
        "ssh",
        "receiver",
        "qgroundcontrol",
        "px4",
        "ubuntu",
    )
    if any(word in lower for word in physical):
        return "physical"
    if any(word in lower for word in target):
        return "target-equivalent"
    return "host-independent"


def load_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacklogError(
            f"cannot read deployment verification policy: {exc}"
        ) from exc
    if value.get("schema") != POLICY_SCHEMA:
        raise BacklogError("unsupported deployment verification policy")
    impact = value.get("change_impact_policy")
    if (
        not isinstance(impact, dict)
        or impact.get("schema") != "iii.change-impact-policy/v1"
        or not re.fullmatch(r"[0-9a-f]{64}", str(impact.get("content_id", "")))
        or not impact.get("path")
    ):
        raise BacklogError(
            "verification policy must bind the Q121 change-impact policy"
        )
    try:
        workspace = path.resolve().parents[2]
        impact_value = json.loads(
            (workspace / impact["path"]).read_text(encoding="utf-8")
        )
    except (IndexError, OSError, json.JSONDecodeError) as exc:
        raise BacklogError(
            f"cannot read bound Q121 change-impact policy: {exc}"
        ) from exc
    if (
        impact_value.get("schema") != impact["schema"]
        or content_identity(impact_value) != impact["content_id"]
    ):
        raise BacklogError("Q121 change-impact policy identity drift")
    levels = value.get("levels")
    if set(levels or {}) != {"host-independent", "target-equivalent", "physical"}:
        raise BacklogError("verification policy must define all three execution levels")
    for name, level in levels.items():
        command = level.get("owner_command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise BacklogError(f"{name}: owner_command must be non-empty argv")
        if level.get("required_evidence") not in {"junit", "signed-local-acceptance"}:
            raise BacklogError(f"{name}: unsupported evidence class")
        if not isinstance(level.get("ci"), bool):
            raise BacklogError(f"{name}: ci must be boolean")
        categories = level.get("impact_categories")
        if (
            not isinstance(categories, list)
            or not categories
            or not all(
                isinstance(category, str) and category for category in categories
            )
        ):
            raise BacklogError(f"{name}: impact_categories must be non-empty strings")
    scenarios = value.get("q131_scenarios")
    expected = {
        "factory",
        "release",
        "field",
        "failure",
        "configuration",
        "evidence",
        "offline",
        "documentation",
        "retirement",
    }
    identifiers = {item.get("id") for item in scenarios or [] if isinstance(item, dict)}
    if identifiers != expected or len(scenarios or []) != len(expected):
        raise BacklogError("Q131 policy must define each unique cutover scenario")
    for override in value.get("level_overrides", []):
        if (
            not isinstance(override, dict)
            or not isinstance(override.get("pattern"), str)
            or override.get("level") not in levels
            or not override.get("rationale")
        ):
            raise BacklogError(
                "verification level overrides require pattern, level, and rationale"
            )
    for override in value.get("command_overrides", []):
        command = override.get("owner_command") if isinstance(override, dict) else None
        if (
            not isinstance(override, dict)
            or not isinstance(override.get("pattern"), str)
            or not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
            or not override.get("rationale")
        ):
            raise BacklogError(
                "verification command overrides require pattern, argv, and rationale"
            )
    return value


def _task_refs(task: Task, text: str) -> tuple[str, str]:
    acceptance = _best_reference(text, task.acceptance, f"{task.id}.a")
    test = _best_reference(text, task.tests, f"{task.id}.t")
    return acceptance, test


def _definition_row(
    *,
    identifier: str,
    text: str,
    digest: str,
    title: str,
    owners: Iterable[str],
    backlog: Backlog,
    policy: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    owner_values = tuple(owners)
    acceptance_refs: list[str] = []
    test_refs: list[str] = []
    for owner in owner_values:
        acceptance, test = _task_refs(backlog.tasks[owner], text)
        acceptance_refs.append(acceptance)
        test_refs.append(test)
    level_name = _environment(text)
    for override in policy.get("level_overrides", []):
        if fnmatch.fnmatch(identifier, override["pattern"]):
            level_name = override["level"]
            break
    level = policy["levels"][level_name]
    owner_command = list(level["owner_command"])
    for override in policy.get("command_overrides", []):
        if fnmatch.fnmatch(identifier, override["pattern"]):
            owner_command = list(override["owner_command"])
            break
    row: dict[str, Any] = {
        "id": identifier,
        "title": title,
        "text": text,
        "digest": digest,
        "owners": list(owner_values),
        "acceptance_refs": acceptance_refs,
        "test_refs": test_refs,
        "level": level_name,
        "owner_command": owner_command,
        "required_evidence": level["required_evidence"],
        "impact_categories": list(level["impact_categories"]),
        "ci": level["ci"],
        "blocking": True,
    }
    if extra:
        row.update(extra)
    return row


def materialize(backlog: Backlog, policy: Mapping[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for clause in backlog.clauses:
        rows.append(
            _definition_row(
                identifier=clause.id,
                text=clause.text,
                digest=clause.digest,
                title=clause.title,
                owners=backlog.owners[clause.decision],
                backlog=backlog,
                policy=policy,
                extra={"kind": "decision-clause", "decision": clause.decision},
            )
        )
    for task in sorted(backlog.tasks.values(), key=lambda item: item.id):
        for index, acceptance in enumerate(task.acceptance, start=1):
            rows.append(
                _definition_row(
                    identifier=f"{task.id}.a{index}",
                    text=acceptance,
                    digest=hashlib.sha256(acceptance.encode("utf-8")).hexdigest(),
                    title=task.title,
                    owners=(task.id,),
                    backlog=backlog,
                    policy=policy,
                    extra={"kind": "task-acceptance", "task": task.id},
                )
            )
    for scenario in sorted(policy["q131_scenarios"], key=lambda item: item["id"]):
        text = scenario["description"]
        rows.append(
            _definition_row(
                identifier=f"Q131.cutover.{scenario['id']}",
                text=text,
                digest=hashlib.sha256(canonical_json(scenario)).hexdigest(),
                title=f"Q131 {scenario['id']} cutover scenario",
                owners=("P5.T0", "P5.T6"),
                backlog=backlog,
                policy=policy,
                extra={
                    "kind": "cutover-scenario",
                    "decision": "Q131",
                    "scenario": scenario["id"],
                    "required_candidate_fields": list(policy["candidate_set_fields"]),
                },
            )
        )
    definition = {
        "schema": MATRIX_SCHEMA,
        "policy_id": content_identity(policy),
        "rows": rows,
    }
    return {**definition, "matrix_id": content_identity(definition)}


def read_matrix(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacklogError(
            f"cannot read deployment verification matrix: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise BacklogError("deployment verification matrix must be one object")
    return value


def audit_matrix(
    backlog: Backlog,
    policy: Mapping[str, Any],
    matrix: Mapping[str, Any],
) -> list[str]:
    expected = materialize(backlog, policy)
    if matrix == expected:
        return []
    errors: list[str] = []
    if matrix.get("schema") != MATRIX_SCHEMA:
        errors.append("unsupported deployment verification matrix schema")
    if matrix.get("policy_id") != expected["policy_id"]:
        errors.append("deployment verification matrix policy identity drift")
    if matrix.get("matrix_id") != expected["matrix_id"]:
        errors.append("deployment verification matrix identity drift")
    actual_rows = {
        row.get("id"): row
        for row in matrix.get("rows", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    expected_rows = {row["id"]: row for row in expected["rows"]}
    for identifier in sorted(expected_rows.keys() - actual_rows.keys()):
        errors.append(f"{identifier}: verification row is missing")
    for identifier in sorted(actual_rows.keys() - expected_rows.keys()):
        errors.append(f"{identifier}: verification row is stale")
    for identifier in sorted(expected_rows.keys() & actual_rows.keys()):
        if expected_rows[identifier] != actual_rows[identifier]:
            errors.append(f"{identifier}: verification definition drift")
    if len(actual_rows) != len(matrix.get("rows", [])):
        errors.append("deployment verification matrix has duplicate or invalid row IDs")
    return errors


def clause_baseline(backlog: Backlog) -> dict[str, Any]:
    return {
        "schema": CLAUSE_BASELINE_SCHEMA,
        "clauses": [
            {
                "id": clause.id,
                "decision": clause.decision,
                "digest": clause.digest,
                "text": clause.text,
            }
            for clause in backlog.clauses
        ],
    }


def load_clause_migrations(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacklogError(f"cannot read clause migrations: {exc}") from exc
    if value.get("schema") != CLAUSE_MIGRATIONS_SCHEMA:
        raise BacklogError("unsupported clause migration mapping")
    rows = value.get("migrations")
    if not isinstance(rows, list):
        raise BacklogError("clause migrations must be a list")
    keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("rationale"):
            raise BacklogError("each clause migration needs a rationale")
        key = (
            row.get("old_id", ""),
            row.get("old_digest", ""),
            row.get("new_id", ""),
            row.get("new_digest", ""),
        )
        if key in keys:
            raise BacklogError("duplicate clause migration")
        keys.add(key)
    return value


def verify_clause_baseline(
    backlog: Backlog,
    baseline_path: Path,
    migrations_path: Path | None = None,
) -> list[str]:
    expected = clause_baseline(backlog)
    try:
        actual = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read clause baseline: {exc}"]
    if actual == expected:
        return []
    expected_by_id = {row["id"]: row for row in expected["clauses"]}
    actual_by_id = {
        row.get("id"): row for row in actual.get("clauses", []) if isinstance(row, dict)
    }
    migrations: set[tuple[str, str, str, str]] = set()
    if migrations_path is not None:
        try:
            value = load_clause_migrations(migrations_path)
            migrations = {
                (
                    row.get("old_id", ""),
                    row.get("old_digest", ""),
                    row.get("new_id", ""),
                    row.get("new_digest", ""),
                )
                for row in value["migrations"]
            }
        except BacklogError as exc:
            return [str(exc)]
    errors: list[str] = []
    all_ids = expected_by_id.keys() | actual_by_id.keys()
    for identifier in sorted(all_ids):
        old = actual_by_id.get(identifier)
        new = expected_by_id.get(identifier)
        if old == new:
            continue
        key = (
            old.get("id", "") if old else "",
            old.get("digest", "") if old else "",
            new.get("id", "") if new else "",
            new.get("digest", "") if new else "",
        )
        if key in migrations:
            continue
        if old is None:
            errors.append(
                f"{identifier}: new clause requires an explicit migration mapping"
            )
        elif new is None:
            errors.append(
                f"{identifier}: removed clause requires an explicit migration mapping"
            )
        else:
            errors.append(
                f"{identifier}: clause text changed; add a reviewed migration mapping"
            )
    return errors


def evaluate_rows(
    matrix: Mapping[str, Any], evidence: Iterable[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for record in evidence:
        for row in record.get("rows", []):
            current = results.get(row["id"])
            if current is None or record["finished_at"] > current["finished_at"]:
                results[row["id"]] = {**row, "finished_at": record["finished_at"]}
    evaluated: list[dict[str, Any]] = []
    for definition in matrix["rows"]:
        result = results.get(definition["id"])
        evaluated.append(
            {
                "id": definition["id"],
                "level": definition["level"],
                "blocking": definition["blocking"],
                "status": result["status"] if result else "not_run",
                "reason": (
                    result.get("reason")
                    if result
                    else "no authenticated evidence supplied"
                ),
                "evidence": list(result.get("evidence", [])) if result else [],
            }
        )
    return evaluated


def verification_result(
    matrix: Mapping[str, Any], evidence: Iterable[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    records = list(evidence)
    candidate_ids = {
        record.get("candidate_set", {}).get("candidate_set_id") for record in records
    }
    candidate_ids.discard(None)
    if len(candidate_ids) > 1:
        raise BacklogError(
            "verification evidence mixes candidate sets; re-run every required row against one exact candidate"
        )
    rows = evaluate_rows(matrix, records)
    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("pass", "warn", "fail", "skipped", "not_run")
    }
    value = {
        "schema": RESULT_SCHEMA,
        "matrix_id": matrix["matrix_id"],
        "candidate_set_id": next(iter(candidate_ids), None),
        "counts": counts,
        "complete": counts["fail"] == 0
        and counts["not_run"] == 0
        and counts["skipped"] == 0,
        "rows": rows,
    }
    return {**value, "result_id": content_identity(value)}


def junit_xml(result: Mapping[str, Any]) -> bytes:
    suite = ElementTree.Element(
        "testsuite",
        name="iii.deployment-verification-matrix",
        tests=str(len(result["rows"])),
        failures=str(result["counts"]["fail"]),
        skipped=str(result["counts"]["skipped"] + result["counts"]["not_run"]),
    )
    for row in result["rows"]:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            classname=f"deployment.{row['level']}",
            name=row["id"],
        )
        if row["status"] == "fail":
            child = ElementTree.SubElement(
                case, "failure", message=row["reason"] or "failed"
            )
            child.text = "\n".join(row["evidence"])
        elif row["status"] in {"not_run", "skipped"}:
            ElementTree.SubElement(
                case, "skipped", message=row["reason"] or row["status"]
            )
        elif row["status"] == "warn":
            output = ElementTree.SubElement(case, "system-out")
            output.text = row["reason"] or "warning"
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        __import__("os").fsync(handle.fileno())
    temporary.replace(path)
