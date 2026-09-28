from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_field_shell_starts_cleanly_without_a_ros_install_overlay(tmp_path: Path) -> None:
    # Hermetic HOME: a native GC install on the developer host would
    # otherwise (correctly) take precedence over the checkout CLI.
    environment = {
        "HOME": str(tmp_path),
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
test -z "${III_RUNTIME_API_TOKEN_FILE:-}"  # Runtime API is intentionally unauthenticated
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
test "$III_HIL_PI_ENDPOINT" = iii.local
test -z "$III_HIL_PI_ADDRESS"
test "$III_RUNTIME_API_HOST" = iii.local
test "$III_SSH_HOST" = iii.local
test "$III_RUNTIME_API_URL" = http://iii.local:8765
test -z "${III_RUNTIME_API_TOKEN_FILE:-}"
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


def test_remote_runtime_binding_preserves_explicit_url_and_drops_stale_token(
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
# A stale token-file setting must not turn commands into credential lookups.
test -z "${III_RUNTIME_API_TOKEN_FILE:-}"
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


def test_field_shell_exports_no_release_signing_trust(tmp_path: Path) -> None:
    # Developer field deployment has no release signing or trust stores
    # (ADR 0010); the field shell must not reintroduce those settings.
    environment = {"HOME": str(tmp_path), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    command = """
set -eu
source setup/setup_field.bash
test -z "${III_RELEASE_TRUSTED_SIGNERS:-}"
test -z "${III_GC_TRUSTED_SIGNERS:-}"
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


def test_managed_gc_installer_wrapper_precedes_the_checkout_cli(tmp_path: Path) -> None:
    managed_bin = tmp_path / ".local" / "bin"
    managed_bin.mkdir(parents=True)
    managed_iii = managed_bin / "iii"
    managed_iii.write_text("#!/bin/sh\n# managed by III GC installer\nexit 0\n")
    managed_iii.chmod(0o755)
    environment = {"HOME": str(tmp_path), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    command = """
set -eu
source setup/setup_field.bash
test "$(command -v iii)" = "$HOME/.local/bin/iii"
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
    assert result.stderr == ""
