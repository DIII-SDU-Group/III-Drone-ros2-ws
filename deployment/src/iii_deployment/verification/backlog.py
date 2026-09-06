"""Strict parser for deployment decisions, tasks, and coverage ownership."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Iterable


DECISION_START = re.compile(
    r"^- \*\*Q(?P<number>[1-9][0-9]*) — (?P<title>.+?):\*\*\s*(?P<body>.*)$"
)
TASK_START = re.compile(r"^#### (?P<id>P[0-9]+\.T[0-9]+): (?P<title>.+)$")
COVERAGE_ROW = re.compile(r"^\| Q(?P<number>[1-9][0-9]*) \| (?P<owners>[^|]+) \|$")
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=(?:[A-Z0-9`]|Do\b|Never\b))")


class BacklogError(ValueError):
    pass


@dataclass(frozen=True)
class Clause:
    id: str
    decision: str
    title: str
    text: str
    digest: str


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    acceptance: tuple[str, ...]
    tests: tuple[str, ...]


@dataclass(frozen=True)
class Backlog:
    clauses: tuple[Clause, ...]
    tasks: dict[str, Task]
    owners: dict[str, tuple[str, ...]]

    def audit(self) -> list[str]:
        errors: list[str] = []
        decisions = {clause.decision for clause in self.clauses}
        expected = {f"Q{number}" for number in range(1, 133)}
        for missing in sorted(expected - decisions, key=_numeric_id):
            errors.append(f"{missing}: decision is missing")
        for extra in sorted(decisions - expected, key=_numeric_id):
            errors.append(f"{extra}: unexpected decision")
        if len(self.owners) != 132:
            errors.append(f"coverage index has {len(self.owners)} rows, expected 132")
        for decision in sorted(expected, key=_numeric_id):
            owners = self.owners.get(decision)
            if not owners:
                errors.append(f"{decision}: no focused implementation owner")
                continue
            for owner in owners:
                task = self.tasks.get(owner)
                if task is None:
                    errors.append(f"{decision}: unknown owner task {owner}")
                elif not task.acceptance or not task.tests:
                    errors.append(f"{decision}: owner {owner} lacks acceptance/tests")
        ids = [clause.id for clause in self.clauses]
        if len(ids) != len(set(ids)):
            errors.append("duplicate clause identifiers")
        return errors


def _numeric_id(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"[QP](\d+)(?:\.T(\d+))?", value)
    if not match:
        return (10**9, 10**9)
    return (int(match.group(1)), int(match.group(2) or 0))


def _clean_markdown(lines: Iterable[str]) -> str:
    parts: list[str] = []
    for line in lines:
        value = line.strip()
        value = re.sub(r"^\d+\.\s+", "", value)
        value = re.sub(r"^-\s+", "", value)
        if value:
            parts.append(value)
    return " ".join(parts)


def _split_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for sentence in SENTENCE_BOUNDARY.split(text):
        for part in re.split(
            r";\s+(?=(?:do\b|never\b|require\b|reject\b|keep\b|allow\b|the\b))",
            sentence,
            flags=re.I,
        ):
            cleaned = part.strip()
            if cleaned:
                clauses.append(cleaned)
    return clauses


def _parse_decisions(lines: list[str]) -> tuple[Clause, ...]:
    decisions: list[tuple[int, str, list[str]]] = []
    current: tuple[int, str, list[str]] | None = None
    in_log = False
    for line in lines:
        if line == "### Grilling decision log":
            in_log = True
            continue
        if in_log and line.startswith("### "):
            break
        if not in_log:
            continue
        match = DECISION_START.match(line)
        if match:
            if current is not None:
                decisions.append(current)
            current = (
                int(match.group("number")),
                match.group("title"),
                [match.group("body")],
            )
        elif current is not None:
            current[2].append(line)
    if current is not None:
        decisions.append(current)

    clauses: list[Clause] = []
    for number, title, body_lines in decisions:
        text = _clean_markdown(body_lines)
        for index, clause_text in enumerate(_split_clauses(text), start=1):
            digest = hashlib.sha256(clause_text.encode("utf-8")).hexdigest()
            clauses.append(
                Clause(f"Q{number}.c{index}", f"Q{number}", title, clause_text, digest)
            )
    return tuple(clauses)


def _parse_tasks(lines: list[str]) -> dict[str, Task]:
    tasks: dict[str, Task] = {}
    current_id: str | None = None
    current_title = ""
    section: str | None = None
    acceptance: list[str] = []
    tests: list[str] = []

    def finish() -> None:
        nonlocal current_id, current_title, section, acceptance, tests
        if current_id is not None:
            if current_id in tasks:
                raise BacklogError(f"duplicate task {current_id}")
            tasks[current_id] = Task(
                current_id, current_title, tuple(acceptance), tuple(tests)
            )
        current_id = None
        current_title = ""
        section = None
        acceptance = []
        tests = []

    for line in lines:
        match = TASK_START.match(line)
        if match:
            finish()
            current_id = match.group("id")
            current_title = match.group("title")
            continue
        if current_id is None:
            continue
        if line == "Acceptance:":
            section = "acceptance"
            continue
        if line == "Tests:":
            section = "tests"
            continue
        if line.startswith("### ") or line.startswith("## "):
            finish()
            continue
        item = re.match(r"^- \[[ x]\] (.+)$", line)
        if item and section == "acceptance":
            acceptance.append(item.group(1).strip())
        elif section == "acceptance" and acceptance and line.startswith("      "):
            acceptance[-1] += " " + line.strip()
        elif section == "tests" and line.strip():
            if line.startswith("- "):
                tests.append(line[2:].strip())
            elif tests and line.startswith("  "):
                tests[-1] += " " + line.strip()
    finish()
    return tasks


def _parse_coverage(lines: list[str]) -> dict[str, tuple[str, ...]]:
    owners: dict[str, tuple[str, ...]] = {}
    for line in lines:
        match = COVERAGE_ROW.match(line)
        if not match:
            continue
        decision = f"Q{match.group('number')}"
        if decision in owners:
            raise BacklogError(f"duplicate coverage row {decision}")
        values = tuple(value.strip() for value in match.group("owners").split(","))
        if not values or any(
            not re.fullmatch(r"P[0-9]+\.T[0-9]+", value) for value in values
        ):
            raise BacklogError(f"invalid coverage owner list for {decision}")
        if len(values) != len(set(values)):
            raise BacklogError(f"duplicate coverage owner for {decision}")
        owners[decision] = values
    return owners


def parse_backlog(path: Path) -> Backlog:
    lines = path.read_text(encoding="utf-8").splitlines()
    backlog = Backlog(
        _parse_decisions(lines), _parse_tasks(lines), _parse_coverage(lines)
    )
    errors = backlog.audit()
    if errors:
        raise BacklogError("\n".join(errors))
    return backlog
