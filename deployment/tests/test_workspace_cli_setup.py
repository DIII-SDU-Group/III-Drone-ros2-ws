from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_cli_path_exposes_cli_and_workspace_deployment_library() -> None:
    completed = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'PYTHONPATH=""; source setup/cli_path.bash; printf "%s" "$PYTHONPATH"',
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    entries = completed.stdout.split(":")
    assert str(ROOT / "tools/III-Drone-CLI") in entries
    assert str(ROOT / "deployment/src") in entries
