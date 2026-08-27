from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.target
ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(
    os.environ.get("III_RUN_GC_ANSIBLE_TARGET_TEST") != "1",
    reason="set III_RUN_GC_ANSIBLE_TARGET_TEST=1 for Ubuntu GC matrix convergence",
)
@pytest.mark.parametrize("version", ["22.04", "24.04"])
def test_gc_online_matrix_second_run_drift_separation_and_permissions(
    tmp_path: Path, version: str
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    script = r"""
set -Eeuo pipefail
trap 'for log in /tmp/ansible-*.log; do test -f "$log" && { echo "== $log =="; tail -n 100 "$log"; }; done' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get update >/tmp/apt.log
apt-get install -y --no-install-recommends git openssh-client openssl python3 python3-venv sudo >/tmp/apt-install.log
useradd --create-home --user-group --shell /bin/bash gcuser
printf '%s\n' 'gcuser ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/gcuser
chmod 0440 /etc/sudoers.d/gcuser
su - gcuser -c 'git config --global --add safe.directory /workspace'
python3 -m venv /opt/ansible
/opt/ansible/bin/pip install --disable-pip-version-check ansible-core==2.17.14 >/tmp/pip.log
uid="$(id -u gcuser)"
gid="$(id -g gcuser)"
run_playbook() {
  mode="$1"
  result="$2"
  offline="${3:-false}"
  check_arg=""
  cache_id="online"
  install_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  artifacts='{}'
  offline_cache=""
  if [ "$offline" = true ]; then
    cache_id="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    install_id="dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    artifacts='{"apt-packages":"/unused/apt.tar","ansible-controller-wheelhouse":"/unused/controller.tar","gc-runtime-wheelhouse":"/unused/runtime.tar","gc-container-images":"/unused/gc-images.tar","arm64-builder-image":"/unused/builder.tar"}'
    offline_cache="/unused/cache"
  fi
  if [ "$mode" = check ]; then check_arg="--check"; fi
  su - gcuser -c "cd /workspace/deployment/ansible && \
    ANSIBLE_CONFIG=/workspace/deployment/ansible/ansible.cfg \
    ANSIBLE_NOCOLOR=1 III_ANSIBLE_RESULT_PATH=$result \
    III_ANSIBLE_CHECK_MODE=$([ \"$mode\" = check ] && echo 1 || echo 0) \
    /opt/ansible/bin/ansible-playbook --inventory localhost, --connection local \
    --extra-vars '{\"iii_gc_user\":\"gcuser\",\"iii_gc_uid\":$uid,\"iii_gc_gid\":$gid,\"iii_gc_home\":\"/home/gcuser\",\"iii_gc_workspace\":\"/workspace\",\"iii_gc_platform_id\":\"ubuntu-$III_VERSION-x86_64\",\"iii_gc_offline\":$offline,\"iii_gc_offline_cache\":\"$offline_cache\",\"iii_gc_policy\":\"/workspace/deployment/gc-host-policy.json\",\"iii_gc_application_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"iii_gc_install_id\":\"$install_id\",\"iii_gc_cache_id\":\"$cache_id\",\"iii_gc_offline_artifacts\":$artifacts,\"iii_gc_test_mode\":true}' \
    --diff $check_arg playbooks/gc-converge.yml" >/tmp/ansible-$mode.log
}
run_playbook apply /tmp/first.json
run_playbook check /tmp/second.json
test "$(python3 -c 'import json; print(json.load(open("/tmp/second.json"))["totals"]["changed"])')" = 0
rm /home/gcuser/.config/iii/gc.env
run_playbook check /tmp/drift.json
test "$(python3 -c 'import json; print(json.load(open("/tmp/drift.json"))["categories"]["operational"]["changed"])')" -gt 0
run_playbook apply /tmp/repair.json
run_playbook check /tmp/final.json
test "$(python3 -c 'import json; print(json.load(open("/tmp/final.json"))["totals"]["changed"])')" = 0
run_playbook apply /tmp/offline-first.json true
run_playbook check /tmp/offline-second.json true
test "$(python3 -c 'import json; print(json.load(open("/tmp/offline-second.json"))["totals"]["changed"])')" = 0
test "$(stat -c %a /home/gcuser/.config/iii/identity/machine-id)" = 600
test "$(stat -c %a /home/gcuser/.config/iii/keys/ssh/id_ed25519)" = 600
test -L /home/gcuser/.config/systemd/user/graphical-session.target.wants/iii-gc.target
grep -Fq 'PartOf=graphical-session.target' /home/gcuser/.config/systemd/user/iii-gc.target
! grep -Fq 'iii-gc-browser.service' /home/gcuser/.config/systemd/user/iii-gc.target
! grep -Riq 'qgroundcontrol' /home/gcuser/.config/systemd/user/iii-gc.target
cp /tmp/first.json /evidence/first.json
cp /tmp/second.json /evidence/second.json
cp /tmp/drift.json /evidence/drift.json
cp /tmp/final.json /evidence/final.json
cp /tmp/offline-first.json /evidence/offline-first.json
cp /tmp/offline-second.json /evidence/offline-second.json
chmod 0644 /evidence/*.json
"""
    evidence = tmp_path / version
    evidence.mkdir()
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--env",
            f"III_VERSION={version}",
            "--volume",
            f"{ROOT}:/workspace:ro",
            "--volume",
            f"{evidence}:/evidence",
            f"ubuntu:{version}",
            "bash",
            "-lc",
            script,
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stdout
    second = json.loads((evidence / "second.json").read_text())
    drift = json.loads((evidence / "drift.json").read_text())
    final = json.loads((evidence / "final.json").read_text())
    offline_second = json.loads((evidence / "offline-second.json").read_text())
    assert second["totals"]["changed"] == 0
    assert drift["categories"]["operational"]["changed"] > 0
    assert final["totals"]["changed"] == 0
    assert offline_second["totals"]["changed"] == 0
    assert set(final["categories"]) == {"application", "development", "operational"}
