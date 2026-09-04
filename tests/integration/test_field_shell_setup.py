from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_field_shell_starts_cleanly_without_a_ros_install_overlay() -> None:
    environment = {
        "HOME": os.environ["HOME"],
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    command = """
set -eu
source setup/setup_field.bash
test "$CLI_CONFIGURATION" = remote
test "$SIMULATION" = false
test "$III_SYSTEM_PROFILE" = real
test "$III_ENVIRONMENT_PROFILE" = field
test "$III_DEFAULT_TARGET" = real
test -z "${GZ_IP:-}"
test "$(command -v iii)" = "$PWD/tools/III-Drone-CLI/bin/iii"
python3 -c 'import iii_drone_contracts.configuration_capture'
iii --help >/dev/null
"""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_workspace_cli_precedes_a_stale_user_installation(tmp_path: Path) -> None:
    stale_bin = tmp_path / ".local" / "bin"
    stale_bin.mkdir(parents=True)
    stale_iii = stale_bin / "iii"
    stale_iii.write_text("#!/bin/sh\nexit 99\n")
    stale_iii.chmod(0o755)
    environment = {
        "HOME": str(tmp_path),
        "PATH": f"{stale_bin}:/usr/local/bin:/usr/bin:/bin",
    }
    command = """
set -eu
source setup/setup_field.bash
test "$(command -v iii)" = "$PWD/tools/III-Drone-CLI/bin/iii"
iii --help >/dev/null
"""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
