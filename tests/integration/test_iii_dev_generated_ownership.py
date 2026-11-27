from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
CONTAINER_HELPER = ROOT / "scripts/workspace/lib/iii_dev_container.sh"


def test_generated_ownership_repair_is_root_only_and_scoped(tmp_path):
    calls = tmp_path / "docker-calls"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$III_TEST_DOCKER_CALLS\"\n"
        "case \"$1\" in\n"
        "  info) exit 0 ;;\n"
        "  ps) printf 'container-id\\tcontainer-name\\n' ;;\n"
        "  exec) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    script = f"""
set -euo pipefail
export III_DEV_DOCKER_BIN={fake_docker}
export III_DEV_WORKSPACE_ROOT={ROOT}
export III_DEV_CONTAINER_WORKSPACE=/home/iii/ws
export III_TEST_DOCKER_CALLS={calls}
source {CONTAINER_HELPER}
iii_dev_repair_generated_ownership
"""
    subprocess.run(["bash", "-c", script], check=True)

    recorded = calls.read_text(encoding="utf-8")
    assert "exec --user root --workdir /home/iii/ws container-id" in recorded
    assert "build install log" in recorded
    assert "-uid 0" in recorded
    assert "chown" in recorded
    assert "III_DEV_CONTAINER_USER" not in recorded


def test_agent_container_examples_run_as_the_devcontainer_user():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert 'docker exec --user iii "$CONTAINER_ID" bash -lc' in guide
