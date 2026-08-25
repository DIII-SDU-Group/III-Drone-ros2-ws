"""Versioned command-result contract shared by every deployment entry point."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import shlex
from typing import Any, Mapping, Sequence


RESULT_SCHEMA = "iii.command-result/v1"


class Outcome(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    REJECTED = "rejected"
    FAILED = "failed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    USAGE_ERROR = "usage_error"
    INTERNAL_ERROR = "internal_error"

    @property
    def exit_code(self) -> int:
        return {
            Outcome.SUCCESS: 0,
            Outcome.WARNING: 10,
            Outcome.REJECTED: 20,
            Outcome.FAILED: 30,
            Outcome.PARTIAL: 31,
            Outcome.INTERRUPTED: 130,
            Outcome.USAGE_ERROR: 64,
            Outcome.INTERNAL_ERROR: 70,
        }[self]


@dataclass(frozen=True)
class NextAction:
    command: tuple[str, ...]
    reason: str
    mutating: bool = False
    prerequisites: tuple[str, ...] = ()
    confirmation_required: bool = False

    def __post_init__(self) -> None:
        if not self.command or any(not part for part in self.command):
            raise ValueError("next action command must contain non-empty arguments")
        if not self.reason.strip():
            raise ValueError("next action reason must be non-empty")

    @property
    def shell_command(self) -> str:
        return shlex.join(self.command)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["command"] = list(self.command)
        result["prerequisites"] = list(self.prerequisites)
        return result


@dataclass(frozen=True)
class CommandResult:
    command: str
    outcome: Outcome
    summary: str
    code: str
    next_actions: tuple[NextAction, ...] = ()
    terminal_reason: str | None = None
    operation_id: str | None = None
    state: str | None = None
    target: str | None = None
    profile: str | None = None
    release_id: str | None = None
    evidence: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command.strip() or not self.summary.strip() or not self.code.strip():
            raise ValueError("command, summary, and stable code are required")
        if not self.next_actions and not self.terminal_reason:
            raise ValueError("a result needs a next action or terminal-state reason")

    @property
    def exit_code(self) -> int:
        return self.outcome.exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "command": self.command,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "code": self.code,
            "operation": (
                {"id": self.operation_id, "state": self.state}
                if self.operation_id is not None
                else None
            ),
            "context": {
                "target": self.target,
                "profile": self.profile,
                "release_id": self.release_id,
            },
            "evidence": list(self.evidence),
            "payload": dict(self.payload),
            "next_actions": [action.to_dict() for action in self.next_actions],
            "terminal_reason": self.terminal_reason,
        }

    def render_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def render_human(self) -> str:
        lines = [self.summary]
        if self.next_actions:
            lines.append("")
            lines.append("Next:")
            for action in self.next_actions:
                mutation = " [mutating]" if action.mutating else ""
                lines.append(f"  {action.shell_command}{mutation} — {action.reason}")
                if action.prerequisites:
                    lines.append(f"    Requires: {', '.join(action.prerequisites)}")
        elif self.terminal_reason:
            lines.extend(("", f"Terminal: {self.terminal_reason}"))
        return "\n".join(lines)


def result_from_exception(command: str, error: BaseException) -> CommandResult:
    """Return a stable internal-error result without leaking implementation data."""

    return CommandResult(
        command=command,
        outcome=Outcome.INTERNAL_ERROR,
        summary=f"{command} failed because of an internal error.",
        code="III_INTERNAL_ERROR",
        terminal_reason=f"{type(error).__name__}: consult retained diagnostics",
    )

