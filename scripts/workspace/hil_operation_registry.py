#!/usr/bin/env python3
"""Read-only scanner for the CLI and configuration-operation registries.

The HIL probe must not mistake a completed configuration reconciliation journal
for an active CLI operation.  This module deliberately has no subprocess or
write capability: it only resolves the two registry authorities, validates
their records, and emits a small machine-readable view of terminal state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping, Sequence


class RegistryScanError(RuntimeError):
    """The operation registry cannot be trusted for a safety decision."""


CLI_OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
RECONCILIATION_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
HEX_ID = re.compile(r"^[0-9a-f]{64}$")
CLI_RECORD_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}\.json$")
CLI_TERMINAL_STATES = {
    "completed",
    "warning",
    "rejected",
    "failed",
    "partial",
    "interrupted",
    "cancelled",
}
CLI_NONTERMINAL_STATES = {"planned", "running"}
CLI_STATES = CLI_TERMINAL_STATES | CLI_NONTERMINAL_STATES
RECONCILIATION_PHASES = {"prepared", "applying", "complete"}
RECONCILIATION_NONTERMINAL_PHASES = {"prepared", "applying"}
RECONCILIATION_FILES = {
    "reconciliation-journal.json",
    "reconciliation-review.json",
    "reconciliation-decisions.json",
}


def _absolute_without_resolving(value: str | os.PathLike[str]) -> Path:
    """Return an expanded absolute path while preserving symlink components."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def resolve_operation_roots(
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    """Resolve and deduplicate the CLI and configuration registry roots."""

    env = os.environ if environment is None else environment

    cli_root: str | os.PathLike[str] | None = env.get("III_OPERATION_STATE_DIR")
    if not cli_root:
        registry_root = env.get("III_REGISTRY_ROOT")
        if registry_root:
            cli_root = os.path.join(os.fspath(registry_root), "operations")
    if not cli_root:
        workspace = env.get("WORKSPACE_DIR")
        if workspace:
            cli_root = os.path.join(os.fspath(workspace), ".iii", "operations")
    if not cli_root:
        xdg_state = env.get("XDG_STATE_HOME")
        if xdg_state:
            cli_root = os.path.join(os.fspath(xdg_state), "iii", "operations")
        else:
            cli_root = os.path.join(
                os.path.expanduser("~"), ".local", "state", "iii", "operations"
            )

    config_root: str | os.PathLike[str] | None = env.get("III_OPERATIONS_ROOT")
    if not config_root:
        workspace = env.get("WORKSPACE_DIR")
        if workspace:
            config_root = os.path.join(os.fspath(workspace), ".iii", "operations")
        else:
            config_root = os.path.join(
                os.path.expanduser("~"), ".local", "state", "iii", "operations"
            )

    roots: list[Path] = []
    seen: set[str] = set()
    for value in (cli_root, config_root):
        if value is None:
            raise RegistryScanError("operation registry root resolution produced no path")
        path = _absolute_without_resolving(value)
        key = os.fspath(path)
        if key not in seen:
            roots.append(path)
            seen.add(key)
    return roots


def _ensure_real_regular(path: Path, *, label: str) -> None:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise RegistryScanError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode):
        raise RegistryScanError(f"{label} is a symbolic link")
    if not stat.S_ISREG(observed.st_mode):
        raise RegistryScanError(f"{label} is not a regular file")


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    _ensure_real_regular(path, label=label)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistryScanError(f"cannot read {label}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryScanError(f"{label} is malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryScanError(f"{label} must contain a JSON object")
    return value


def _canonical(value: Mapping[str, Any], *, newline: bool = False) -> bytes:
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if newline:
        text += "\n"
    return text.encode("utf-8")


def _cli_plan_id(plan: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_id"}
    # CLI OperationStore.content_id intentionally uses json.dumps defaults,
    # including ensure_ascii=True and no terminating newline.
    return hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _newline_identity(value: Mapping[str, Any], field: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_canonical(unsigned, newline=True)).hexdigest()


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise RegistryScanError(f"{label} must be a string")
    return value


def _require_hex(value: Any, *, label: str) -> str:
    value = _require_string(value, label=label)
    if HEX_ID.fullmatch(value) is None:
        raise RegistryScanError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _validate_cli(directory: Path, names: set[str]) -> dict[str, str]:
    if not {"plan.json", "state.json"}.issubset(names):
        missing = sorted({"plan.json", "state.json"} - names)
        raise RegistryScanError(
            f"CLI operation {directory.name} is missing {', '.join(missing)}"
        )
    if names & RECONCILIATION_FILES:
        raise RegistryScanError(
            f"CLI operation {directory.name} mixes CLI and reconciliation records"
        )
    for name in names:
        if CLI_RECORD_NAME.fullmatch(name) is None:
            raise RegistryScanError(
                f"CLI operation {directory.name} contains unsupported record {name}"
            )

    plan = _read_object(directory / "plan.json", label=f"{directory}/plan.json")
    state = _read_object(directory / "state.json", label=f"{directory}/state.json")
    if plan.get("schema") != "iii.cli-operation-plan/v1":
        raise RegistryScanError(f"{directory}/plan.json has an unsupported schema")
    if state.get("schema") != "iii.cli-operation-state/v1":
        raise RegistryScanError(f"{directory}/state.json has an unsupported schema")

    operation_id = _require_string(plan.get("operation_id"), label="CLI plan operation_id")
    if CLI_OPERATION_ID.fullmatch(operation_id) is None:
        raise RegistryScanError("CLI plan operation_id is malformed")
    if directory.name != operation_id:
        raise RegistryScanError("CLI operation directory and plan operation_id disagree")
    state_operation_id = _require_string(
        state.get("operation_id"), label="CLI state operation_id"
    )
    if state_operation_id != operation_id:
        raise RegistryScanError("CLI plan and state operation_id disagree")

    plan_id = _require_hex(plan.get("plan_id"), label="CLI plan_id")
    if plan_id != _cli_plan_id(plan):
        raise RegistryScanError("CLI plan content identity mismatch")
    state_plan_id = _require_string(state.get("plan_id"), label="CLI state plan_id")
    if state_plan_id != plan_id:
        raise RegistryScanError("CLI plan and state plan_id disagree")
    state_name = _require_string(state.get("state"), label="CLI state")
    if state_name not in CLI_STATES:
        raise RegistryScanError(f"CLI operation has unsupported state {state_name!r}")
    return {
        "kind": "cli",
        "operation_id": operation_id,
        "status": state_name,
    }


def _safe_completed_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RegistryScanError("reconciliation completed_paths contains an unsafe path")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or value != candidate.as_posix()
        or "\x00" in value
    ):
        raise RegistryScanError("reconciliation completed_paths contains an unsafe path")
    return value


def _validate_reconciliation(directory: Path, names: set[str]) -> dict[str, str]:
    allowed = RECONCILIATION_FILES
    if "reconciliation-journal.json" not in names:
        raise RegistryScanError(
            f"reconciliation directory {directory.name} is missing reconciliation-journal.json"
        )
    unknown = names - allowed
    if unknown:
        raise RegistryScanError(
            f"reconciliation directory {directory.name} contains unsupported records: "
            + ", ".join(sorted(unknown))
        )
    journal = _read_object(
        directory / "reconciliation-journal.json",
        label=f"{directory}/reconciliation-journal.json",
    )
    expected_keys = {
        "schema",
        "journal_id",
        "operation_id",
        "plan_id",
        "initial_state_id",
        "phase",
        "completed_paths",
        "state_id",
    }
    if set(journal) != expected_keys:
        raise RegistryScanError("reconciliation journal keys are not exact")
    if journal.get("schema") != "iii.configuration-reconciliation-journal/v1":
        raise RegistryScanError("reconciliation journal has an unsupported schema")
    journal_id = _require_hex(journal.get("journal_id"), label="reconciliation journal_id")
    operation_id = _require_string(
        journal.get("operation_id"), label="reconciliation operation_id"
    )
    if RECONCILIATION_OPERATION_ID.fullmatch(operation_id) is None:
        raise RegistryScanError("reconciliation operation_id is malformed")
    plan_id = _require_hex(journal.get("plan_id"), label="reconciliation plan_id")
    _require_hex(journal.get("initial_state_id"), label="reconciliation initial_state_id")
    state_id = _require_hex(journal.get("state_id"), label="reconciliation state_id")
    if journal_id != _newline_identity(journal, "journal_id"):
        raise RegistryScanError("reconciliation journal content identity mismatch")
    phase = _require_string(journal.get("phase"), label="reconciliation phase")
    if phase not in RECONCILIATION_PHASES:
        raise RegistryScanError(f"reconciliation journal has unsupported phase {phase!r}")
    completed_paths = journal.get("completed_paths")
    if not isinstance(completed_paths, list):
        raise RegistryScanError("reconciliation completed_paths must be a list")
    for item in completed_paths:
        _safe_completed_path(item)

    expected_names = {operation_id, f"{operation_id}-{plan_id[:16]}"}
    if directory.name not in expected_names:
        raise RegistryScanError("reconciliation directory and journal identity disagree")

    review: dict[str, Any] | None = None
    if "reconciliation-review.json" in names:
        review = _read_object(
            directory / "reconciliation-review.json",
            label=f"{directory}/reconciliation-review.json",
        )
        if review.get("schema") != "iii.configuration-reintroduction-review/v1":
            raise RegistryScanError("reconciliation review has an unsupported schema")
        review_id = _require_hex(review.get("review_id"), label="reconciliation review_id")
        if review_id != _newline_identity(review, "review_id"):
            raise RegistryScanError("reconciliation review content identity mismatch")
        if review.get("operation_id") != operation_id or review.get("plan_id") != plan_id:
            raise RegistryScanError("reconciliation review is cross-bound")

    if "reconciliation-decisions.json" in names:
        if review is None:
            raise RegistryScanError("reconciliation decisions require a review")
        decisions = _read_object(
            directory / "reconciliation-decisions.json",
            label=f"{directory}/reconciliation-decisions.json",
        )
        if decisions.get("schema") != "iii.configuration-reintroduction-decisions/v1":
            raise RegistryScanError("reconciliation decisions have an unsupported schema")
        decision_id = _require_hex(
            decisions.get("decision_id"), label="reconciliation decision_id"
        )
        if decision_id != _newline_identity(decisions, "decision_id"):
            raise RegistryScanError("reconciliation decisions content identity mismatch")
        if (
            decisions.get("operation_id") != operation_id
            or decisions.get("plan_id") != plan_id
            or decisions.get("review_id") != review.get("review_id")
        ):
            raise RegistryScanError("reconciliation decisions are cross-bound")

    return {
        "kind": "reconciliation",
        "operation_id": operation_id,
        "status": phase,
    }


def _scan_directory(directory: Path) -> dict[str, str]:
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise RegistryScanError(f"cannot inspect operation directory {directory}: {exc}") from exc
    names: set[str] = set()
    for entry in entries:
        if entry.name.startswith("."):
            raise RegistryScanError(
                f"operation directory {directory.name} contains hidden entry {entry.name}"
            )
        try:
            mode = entry.lstat().st_mode
        except OSError as exc:
            raise RegistryScanError(f"cannot inspect {entry}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise RegistryScanError(f"operation directory contains symbolic link {entry}")
        if not stat.S_ISREG(mode):
            raise RegistryScanError(f"operation directory contains unsafe entry {entry}")
        names.add(entry.name)

    if names & {"plan.json", "state.json"}:
        return _validate_cli(directory, names)
    if names & RECONCILIATION_FILES:
        return _validate_reconciliation(directory, names)
    raise RegistryScanError(f"operation directory {directory.name} has unknown content")


def scan_operation_roots(roots: Sequence[Path]) -> dict[str, Any]:
    """Validate each registry root and return terminal/nonterminal records."""

    normalized: list[Path] = []
    seen: set[str] = set()
    for supplied in roots:
        root = _absolute_without_resolving(supplied)
        key = os.fspath(root)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(root)

    terminal: list[dict[str, str]] = []
    nonterminal: list[dict[str, str]] = []
    for root in normalized:
        try:
            root_mode = root.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RegistryScanError(f"cannot inspect registry root {root}: {exc}") from exc
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            raise RegistryScanError(f"operation registry root is not a real directory: {root}")
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise RegistryScanError(f"cannot inspect registry root {root}: {exc}") from exc
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                mode = entry.lstat().st_mode
            except OSError as exc:
                raise RegistryScanError(f"cannot inspect registry entry {entry}: {exc}") from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise RegistryScanError(f"registry contains unsafe entry {entry}")
            record = _scan_directory(entry)
            result = {
                **record,
                "path": str(entry.absolute()),
                "root": str(root),
            }
            if record["status"] in CLI_NONTERMINAL_STATES | RECONCILIATION_NONTERMINAL_PHASES:
                nonterminal.append(result)
            else:
                terminal.append(result)

    def sort_key(item: Mapping[str, str]) -> tuple[str, str]:
        return (item["root"], item["path"])

    terminal.sort(key=sort_key)
    nonterminal.sort(key=sort_key)
    return {
        "schema": "hil-operation-registry-scan/v1",
        "roots": [str(root) for root in normalized],
        "terminal": terminal,
        "nonterminal": nonterminal,
    }


def main() -> int:
    try:
        result = scan_operation_roots(resolve_operation_roots())
    except RegistryScanError as exc:
        print(f"registry scan refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 3 if result["nonterminal"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
