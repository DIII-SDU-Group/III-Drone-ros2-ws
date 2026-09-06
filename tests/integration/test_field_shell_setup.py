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
test "$III_RUNTIME_API_URL" = http://iii.local:8765
test "$III_RUNTIME_API_TOKEN_FILE" = "$HOME/.config/iii/credentials/runtime-api.token"
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


def test_hil_shell_binds_runtime_controls_to_aircraft_without_local_fallback(
    tmp_path: Path,
) -> None:
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    command = """
set -eu
source setup/setup_hil.bash
test "$CLI_CONFIGURATION" = remote
test "$III_SYSTEM_PROFILE" = hil
test "$III_DEFAULT_TARGET" = hil
test "$III_RUNTIME_API_URL" = http://iii.local:8765
test "$III_RUNTIME_API_TOKEN_FILE" = "$HOME/.config/iii/credentials/runtime-api.token"
test -z "${GZ_IP:-}"
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


def test_remote_runtime_binding_preserves_explicit_operator_overrides(
    tmp_path: Path,
) -> None:
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "III_RUNTIME_API_URL": "https://runtime.example.test",
        "III_RUNTIME_API_TOKEN_FILE": str(tmp_path / "explicit.token"),
    }
    command = """
set -eu
source setup/setup_field.bash
test "$III_RUNTIME_API_URL" = https://runtime.example.test
test "$III_RUNTIME_API_TOKEN_FILE" = "$HOME/explicit.token"
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


def test_field_shell_uses_canonical_workstation_trust_without_overriding_explicit_values(
    tmp_path: Path,
) -> None:
    trust = tmp_path / ".config/iii/keys/signing/trusted-signers.json"
    trust.parent.mkdir(parents=True)
    trust.write_text("{}\n")
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "III_GC_TRUSTED_SIGNERS": "/operator/gc-trust.json",
    }
    command = """
set -eu
source setup/setup_field.bash
test "$III_RELEASE_TRUSTED_SIGNERS" = "$HOME/.config/iii/keys/signing/trusted-signers.json"
test "$III_GC_TRUSTED_SIGNERS" = /operator/gc-trust.json
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
