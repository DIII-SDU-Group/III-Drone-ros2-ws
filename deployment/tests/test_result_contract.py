from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

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
def test_exit_codes_use_stable_families(outcome: Outcome) -> None:
    assert {candidate.exit_code for candidate in Outcome} == {0, 10, 20, 30, 31, 64, 70, 130}
    assert isinstance(outcome.exit_code, int)


def test_deployment_import_is_the_canonical_cli_type() -> None:
    from iii.result import CommandResult as CliCommandResult

    assert CommandResult is CliCommandResult


def test_package_policy_import_does_not_eagerly_require_cli(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root / "deployment" / "src")}
    process = subprocess.run(
        [sys.executable, "-S", "-c", "import iii_deployment; assert 'iii.result' not in __import__('sys').modules"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
