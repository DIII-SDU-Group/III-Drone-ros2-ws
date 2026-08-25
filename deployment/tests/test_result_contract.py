from __future__ import annotations

import json

import pytest

from iii_deployment.result import CommandResult, NextAction, Outcome


def test_human_and_json_use_same_next_action() -> None:
    action = NextAction(("iii", "deploy", "status", "op id"), "Reattach safely.")
    result = CommandResult(
        command="iii deploy field",
        outcome=Outcome.INTERRUPTED,
        summary="Detached from accepted operation.",
        code="III_OPERATION_DETACHED",
        operation_id="op id",
        state="accepted",
        next_actions=(action,),
    )
    assert action.shell_command in result.render_human()
    assert json.loads(result.render_json())["next_actions"][0]["command"] == list(action.command)
    assert result.exit_code == 130


def test_result_requires_a_next_action_or_terminal_reason() -> None:
    with pytest.raises(ValueError, match="next action"):
        CommandResult("iii test", Outcome.SUCCESS, "done", "DONE")


@pytest.mark.parametrize("outcome", list(Outcome))
def test_exit_codes_are_stable_and_unique(outcome: Outcome) -> None:
    codes = {candidate.exit_code for candidate in Outcome}
    assert len(codes) == len(Outcome)
    assert isinstance(outcome.exit_code, int)

