from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/workspace/iii_ground_control.sh"


def _run(
    tmp_path: Path,
    *,
    curl_body: str,
    compose_ids: str = "gc-1\n",
    configured_services: str = "proxy\nfrontend\n",
    running_services: str = "proxy\nfrontend\n",
    compose_config_rc: int = 0,
    compose_up_rc: int = 0,
    foreign_project: bool = False,
    script_path: Path = SCRIPT,
    sleep_body: str = "#!/bin/bash\nexit 0\n",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.jsonl"
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$*" in\n'
        "  *'ps -a --filter label=com.docker.compose.project='* ) printf '%s' \"$FAKE_COMPOSE_IDS\" ;;\n"
        '  *\'inspect --format \'* ) [[ "${FAKE_FOREIGN_PROJECT:-0}" == 1 ]] && echo /another/checkout/docker-compose.prod.yml || echo "$FAKE_COMPOSE_FILE" ;;\n'
        "  *' config --services'*) printf '%s' \"$FAKE_CONFIGURED_SERVICES\" ;;\n"
        "  *' config --quiet'*) exit \"$FAKE_COMPOSE_CONFIG_RC\" ;;\n"
        "  *' ps --services --status running'*) printf '%s' \"$FAKE_RUNNING_SERVICES\" ;;\n"
        "  *' ps --all --quiet'*) printf '%s' \"$FAKE_COMPOSE_IDS\" ;;\n"
        "  *' ps --quiet'*) printf '%s' \"$FAKE_COMPOSE_IDS\" ;;\n"
        "  *' up '* ) exit \"$FAKE_COMPOSE_UP_RC\" ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(curl_body, encoding="utf-8")
    (bin_dir / "sleep").write_text(sleep_body, encoding="utf-8")
    for executable in ("docker", "curl", "sleep"):
        (bin_dir / executable).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_COMPOSE_IDS": compose_ids,
        "FAKE_CONFIGURED_SERVICES": configured_services,
        "FAKE_RUNNING_SERVICES": running_services,
        "FAKE_COMPOSE_CONFIG_RC": str(compose_config_rc),
        "FAKE_COMPOSE_UP_RC": str(compose_up_rc),
        "FAKE_FOREIGN_PROJECT": "1" if foreign_project else "0",
        "FAKE_COMPOSE_FILE": str(ROOT / "src/III-Drone-GC/docker-compose.prod.yml"),
        "III_GC_LOG_DIR": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        [str(script_path), "start"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    result.docker_commands = (
        docker_log.read_text(encoding="utf-8").splitlines()
        if docker_log.exists()
        else []
    )
    return result


def test_start_returns_without_compose_up_when_both_endpoints_are_healthy(tmp_path):
    result = _run(tmp_path, curl_body="#!/bin/bash\nexit 0\n")
    assert result.returncode == 0, result.stderr
    assert any("config --quiet" in command for command in result.docker_commands)
    assert not any(" up " in f" {command} " for command in result.docker_commands)
    assert "already ready" in result.stdout


def test_start_does_not_accept_healthy_foreign_ports_without_owned_containers(tmp_path):
    result = _run(
        tmp_path,
        compose_ids="",
        running_services="",
        curl_body="#!/bin/bash\nexit 0\n",
    )
    assert result.returncode == 3
    assert any(" up -d --build" in f" {command} " for command in result.docker_commands)
    assert "already ready" not in result.stdout


def test_start_preserves_foreign_same_named_compose_project(tmp_path):
    result = _run(
        tmp_path,
        curl_body="#!/bin/bash\nexit 0\n",
        foreign_project=True,
    )
    assert result.returncode == 3
    assert "belongs to another Compose checkout" in result.stderr
    assert not any(" up " in f" {command} " for command in result.docker_commands)
    assert not any(" down " in f" {command} " for command in result.docker_commands)


def test_installed_launcher_recognizes_legacy_project_from_its_source_checkout(
    tmp_path,
):
    install_root = tmp_path / "install"
    script_path = install_root / "workspace/scripts/workspace/iii_ground_control.sh"
    script_path.parent.mkdir(parents=True)
    script_path.write_bytes(SCRIPT.read_bytes())
    script_path.chmod(0o755)
    (install_root / "install.json").write_text(
        json.dumps({"checkout": {"checkout": str(ROOT)}}), encoding="utf-8"
    )
    result = _run(tmp_path, curl_body="#!/bin/bash\nexit 0\n", script_path=script_path)
    assert result.returncode == 0, result.stderr
    assert "already ready" in result.stdout


def test_default_log_directory_is_outside_replaceable_install_snapshot(tmp_path):
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path)
    environment.pop("III_GC_LOG_DIR", None)
    environment.pop("XDG_STATE_HOME", None)
    result = subprocess.run(
        [str(SCRIPT), "start", "--dry-run"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert f"Logs: {tmp_path}/.local/state/iii/ground-control" in result.stdout


def test_start_propagates_compose_config_failure_without_starting_or_waiting(tmp_path):
    result = _run(
        tmp_path,
        curl_body="#!/bin/bash\necho curl-called >&2\nexit 0\n",
        compose_config_rc=17,
    )
    assert result.returncode == 17
    assert "Compose configuration validation failed" in result.stderr
    assert not any(" up -d" in f" {command} " for command in result.docker_commands)


def test_start_propagates_compose_up_failure_without_endpoint_waits(tmp_path):
    result = _run(
        tmp_path,
        compose_ids="",
        running_services="",
        curl_body="#!/bin/bash\necho curl-called >&2\nexit 0\n",
        compose_up_rc=19,
    )
    assert result.returncode == 19
    assert "Compose project startup failed" in result.stderr
    assert "curl-called" not in result.stderr


def test_owned_query_is_exact_project_container_presence(tmp_path):
    result = _run(tmp_path, curl_body="#!/bin/bash\nexit 0\n")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_LOG": str(tmp_path / "owned.jsonl"),
        "FAKE_COMPOSE_IDS": "gc-1\n",
        "FAKE_COMPOSE_FILE": str(ROOT / "src/III-Drone-GC/docker-compose.prod.yml"),
    }
    owned = subprocess.run(
        [str(SCRIPT), "owned"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert owned.returncode == 0
    empty_env = {**env, "FAKE_COMPOSE_IDS": ""}
    empty = subprocess.run(
        [str(SCRIPT), "owned"], cwd=ROOT, env=empty_env, capture_output=True, text=True
    )
    assert empty.returncode == 1


def test_start_reaps_parallel_probes_and_captures_logs_once_on_combined_failure(
    tmp_path,
):
    result = _run(
        tmp_path,
        curl_body=(
            "#!/bin/bash\n"
            'case "$*" in\n'
            "  *8780*) exit 1 ;;\n"
            "  *5173*) exit 0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        ),
    )
    assert result.returncode == 3
    assert "GC proxy" in result.stderr
    assert "operator interface" not in result.stderr
    log_commands = [
        command for command in result.docker_commands if " logs " in f" {command} "
    ]
    assert len(log_commands) == 1


def test_start_reports_both_endpoint_failures_in_stable_order(tmp_path):
    result = _run(
        tmp_path,
        compose_ids="",
        curl_body="#!/bin/bash\nexit 1\n",
    )
    assert result.returncode == 3
    assert (
        "Ground-control startup failed: GC proxy operator interface did not become reachable."
        in result.stderr
    )
    log_commands = [
        command for command in result.docker_commands if " logs " in f" {command} "
    ]
    assert len(log_commands) == 1


def test_start_waits_for_both_endpoint_probes_concurrently(tmp_path):
    started = time.monotonic()
    result = _run(
        tmp_path,
        compose_ids="gc-1\n",
        running_services="proxy\nfrontend\n",
        curl_body="#!/bin/bash\n/usr/bin/sleep 0.25\nexit 0\n",
        sleep_body='#!/bin/bash\n/usr/bin/sleep "$@"\n',
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 0.7


def test_start_requires_every_configured_service_before_fast_path(tmp_path):
    result = _run(
        tmp_path,
        configured_services="proxy\nfrontend\nworker\n",
        running_services="proxy\nfrontend\n",
        curl_body="#!/bin/bash\nexit 0\n",
    )
    assert result.returncode == 3
    assert any(" up -d --build" in f" {command} " for command in result.docker_commands)
    assert "already ready" not in result.stdout


def test_exact_project_mutations_are_serialized_by_the_gc_lock(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    started = tmp_path / "started"
    release = tmp_path / "release"
    stopped = tmp_path / "stopped"
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$FAKE_DOCKER_LOG"\n'
        'case "$*" in\n'
        "  *' config --services'*) printf 'proxy\\nfrontend\\n' ;;\n"
        "  *' ps --services --status running'*) [[ -e \"$FAKE_STARTED\" ]] && printf 'proxy\\nfrontend\\n' ;;\n"
        "  *' ps --all --quiet'*) ;;\n"
        '  *\' up \'* ) touch "$FAKE_STARTED"; while [[ ! -e "$FAKE_RELEASE" ]]; do /bin/sleep 0.02; done ;;\n'
        "  *' down '*) touch \"$FAKE_STOPPED\" ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    for executable in ("docker", "curl"):
        (bin_dir / executable).chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_STARTED": str(started),
        "FAKE_RELEASE": str(release),
        "FAKE_STOPPED": str(stopped),
        "III_GC_LOCK_DIR": str(tmp_path / "locks"),
    }
    starter = subprocess.Popen(
        [str(SCRIPT), "start"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.02)
    assert started.exists()
    stopper = subprocess.Popen(
        [str(SCRIPT), "stop"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.2)
    assert stopper.poll() is None
    assert not stopped.exists()
    release.touch()
    start_result = starter.communicate(timeout=5)
    stop_result = stopper.communicate(timeout=5)
    assert starter.returncode == 0, start_result
    assert stopper.returncode == 0, stop_result
    assert stopped.exists()


def _dry_run_with_environment(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "ground-control.env"
    env_file.write_text(content, encoding="utf-8")
    return subprocess.run(
        [str(SCRIPT), "start", "--dry-run"],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path), "III_GC_ENV_FILE": str(env_file)},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_aircraft_profiles_require_a_pinned_identity(tmp_path):
    for profile in ("real", "opti_track"):
        directory = tmp_path / profile
        directory.mkdir()
        unpinned = _dry_run_with_environment(directory, f"III_GC_EXPECTED_PROFILE={profile}\n")
        assert unpinned.returncode == 2, unpinned.stdout
        assert (
            f"{profile} profile requires III_GC_EXPECTED_RUNTIME_ID and III_GC_EXPECTED_SYSTEM_ID"
            in unpinned.stderr
        )
        pinned = _dry_run_with_environment(
            directory,
            "III_GC_EXPECTED_RUNTIME_ID=iii-runtime\n"
            "III_GC_EXPECTED_SYSTEM_ID=iii-drone\n"
            f"III_GC_EXPECTED_PROFILE={profile}\n",
        )
        assert pinned.returncode == 0, pinned.stderr
        assert "Would start production ground control" in pinned.stdout


def test_virtual_profiles_need_no_pinned_identity(tmp_path):
    for profile in ("hil", "sim"):
        directory = tmp_path / profile
        directory.mkdir()
        result = _dry_run_with_environment(directory, f"III_GC_EXPECTED_PROFILE={profile}\n")
        assert result.returncode == 0, result.stderr
