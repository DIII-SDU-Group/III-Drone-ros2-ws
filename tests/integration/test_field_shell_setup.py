from __future__ import annotations

from pathlib import Path
import shutil
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
# The shell owns import paths; interpreter packages (pydantic v2) belong to
# the CLI's own environment, which `iii --help` below exercises.
case ":$PYTHONPATH:" in *":$PWD/src/III-Drone-Contracts:"*) ;; *) exit 1 ;; esac
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


def _stub_hil_peer_resolver(tmp_path: Path, *, route: str | None) -> tuple[Path, Path]:
    """Shadow ``python3`` so setup_hil.bash never probes DNS/mDNS or SSH.

    Only the resolve_hil_peer.py invocation is intercepted (its arguments are
    recorded); every other python3 call is delegated to the real interpreter.
    ``route`` is the resolver's stdout ("peer source"); ``None`` simulates an
    unreachable aircraft.
    """
    real_python = shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")
    assert real_python
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "resolver-args"
    outcome = (
        f"echo '{route}'; exit 0"
        if route is not None
        else "echo 'HIL peer resolution for stub failed' >&2; exit 1"
    )
    shim = bin_dir / "python3"
    shim.write_text(
        "#!/bin/bash\n"
        'case "${1:-}" in\n'
        "  */scripts/workspace/resolve_hil_peer.py)\n"
        f"    shift; printf '%s\\n' \"$@\" > '{record}'; {outcome} ;;\n"
        "esac\n"
        f'exec {real_python} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir, record


def _hil_environment(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        # Hermetic: never read the host's onboard runtime env or peer caches.
        "III_HIL_ONBOARD_RUNTIME_ENV": str(tmp_path / "absent-runtime.env"),
        "III_HIL_PEER_STATE_DIR": str(tmp_path / "peer-state"),
    }


def test_hil_shell_binds_runtime_controls_to_aircraft_without_local_fallback(
    tmp_path: Path,
) -> None:
    # TEST-NET addresses: the resolver result is only a transport routing hint.
    bin_dir, record = _stub_hil_peer_resolver(tmp_path, route="192.0.2.10 192.0.2.20")
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
# The resolved peer pins the DDS/PX4 transport route but never replaces the
# aircraft hostname used for runtime controls.
test "$III_HIL_RESOLVED_PI_ADDRESS" = 192.0.2.10
test "$III_HIL_WORKSTATION_ADDRESS" = 192.0.2.20
"""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=_hil_environment(tmp_path, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert record.read_text(encoding="utf-8").splitlines() == [
        "iii.local",
        str(tmp_path / "peer-state"),
        "0",
    ]


def test_hil_shell_reports_unresolved_peer_without_failing(tmp_path: Path) -> None:
    bin_dir, _record = _stub_hil_peer_resolver(tmp_path, route=None)
    command = """
set -eu
source setup/setup_hil.bash
test "$III_RUNTIME_API_URL" = http://iii.local:8765
test -z "${III_HIL_RESOLVED_PI_ADDRESS:-}"
"""
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=ROOT,
        env=_hil_environment(tmp_path, bin_dir),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (
        "no reachable Pi IPv4 was resolved for iii.local; continuing with the hostname"
        in result.stderr
    )


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
