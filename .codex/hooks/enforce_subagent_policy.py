#!/usr/bin/env python3

import json
import re
import sys
from pathlib import Path


def deny(reason: str) -> None:
    print(  # noqa: T201 -- hook protocol requires JSON on stdout.
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def allow(updated_input=None) -> None:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }

    if updated_input is not None:
        output["hookSpecificOutput"]["updatedInput"] = updated_input

    print(json.dumps(output))  # noqa: T201 -- hook protocol requires JSON on stdout.
    sys.exit(0)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def configured_agents(root: Path) -> set[str]:
    """Read agent names from repository .codex/agents/*.toml."""
    agents = set()
    agent_dir = root / ".codex" / "agents"

    for path in agent_dir.glob("*.toml"):
        text = path.read_text(encoding="utf-8")

        match = re.search(
            r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$',
            text,
        )

        if match:
            agents.add(match.group(1))

    return agents


payload = json.load(sys.stdin)
tool_input = payload.get("tool_input") or {}

requested = tool_input.get("agent_type")
current_agent_id = payload.get("agent_id")

root = repo_root()
repo_agents = configured_agents(root)

if not repo_agents:
    deny("No repository subagents were found under .codex/agents/.")

# Never permit implicit/generic spawning.
if not requested:
    deny(
        "Repository policy forbids generic/inherited subagents. "
        "Retry with an explicit agent_type defined under .codex/agents/. "
        f"Available roles: {', '.join(sorted(repo_agents))}. "
        "If agent_type is unavailable in the current spawn interface, "
        "do not spawn a fallback agent."
    )

if requested not in repo_agents:
    deny(
        f"Subagent type '{requested}' is not a repository-defined agent. "
        f"Allowed roles: {', '.join(sorted(repo_agents))}."
    )

# Repository roles define their own model/reasoning settings.
if "model" in tool_input or "reasoning_effort" in tool_input:
    deny(
        "Do not override model or reasoning_effort during spawn. "
        f"Use the configuration defined for '{requested}' in .codex/agents/."
    )

# Presence of agent_id means this spawn originates from a subagent rather
# than the root coordinator.
if current_agent_id:
    deny("Nested delegation is forbidden: only the parent may spawn agents.")

# Configured specialist agents should start with fresh context.
# This also avoids accidentally forking the entire expensive parent history.
if tool_input.get("fork_turns") != "none":
    updated = dict(tool_input)
    updated["fork_turns"] = "none"
    allow(updated)

allow()
