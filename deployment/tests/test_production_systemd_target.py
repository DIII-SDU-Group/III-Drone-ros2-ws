from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from iii_deployment.contracts import canonical_json, content_identity


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "iii-ansible-target-test:24.04-amd64"
UNIT_CONTRACT = json.loads(
    (ROOT / "deployment/systemd/unit-contract.json").read_text(encoding="utf-8")
)


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=check, text=True, capture_output=True)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def _release(root: Path, label: str, *, broken: bool = False) -> tuple[str, dict]:
    manifest = {
        "schema_version": "1",
        "manifest_type": "release",
        "release_id": "0" * 64,
        "build_label": label,
        "target": {
            "definition_id": "a" * 64,
            "host_baseline": "b" * 64,
            "host_unit_contract": UNIT_CONTRACT["contract_id"],
        },
        "profiles": [{"id": "real", "bootable": True}],
    }
    manifest["release_id"] = content_identity(
        {key: item for key, item in manifest.items() if key != "release_id"}
    )
    release = root / "opt/iii/releases" / manifest["release_id"]
    _write(release / "release-manifest.json", manifest)
    if not broken:
        wrapper = release / "bin/iii-release-env"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(
            "#!/bin/sh\n"
            'root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"\n'
            'export PYTHONPATH="$root/python/cp312/site-packages"\n'
            'exec "$@"\n'
        )
        wrapper.chmod(0o755)
        modules = {
            "iii_drone_supervision/system_daemon.py": "system-daemon",
            "iii_drone_runtime/api/main.py": "runtime-api",
        }
        for relative, service in modules.items():
            module = release / "python/cp312/site-packages" / relative
            module.parent.mkdir(parents=True, exist_ok=True)
            for package in (module.parent, module.parent.parent):
                (package / "__init__.py").touch()
            module.write_text(
                "import os,pathlib,time\n"
                f"pathlib.Path('/run/iii/{service}-release').write_text(os.environ['III_RELEASE_ID'])\n"
                "while True: time.sleep(0.1)\n"
            )
    selector = {
        "schema": "iii.activation-selector/v1",
        "selector_id": "0" * 64,
        "release_id": manifest["release_id"],
        "release_path": f"/opt/iii/releases/{manifest['release_id']}",
        "configuration_checkpoint_id": "d" * 64,
        "configuration_checkpoint_path": "/var/lib/iii/configuration/checkpoints/"
        + "d" * 64,
        "configuration_schema_version": 1,
        "mission_catalog_hash": "sha256:" + "e" * 64,
        "profile": "real",
    }
    selector["selector_id"] = content_identity(
        {key: item for key, item in selector.items() if key != "selector_id"}
    )
    return manifest["release_id"], selector


def _wait(container: str, command: str, *, attempts: int = 100) -> None:
    for _attempt in range(attempts):
        result = _run(
            ["docker", "exec", container, "bash", "-lc", command], check=False
        )
        if result.returncode == 0:
            return
        time.sleep(0.1)
    raise AssertionError(f"target condition did not settle: {command}: {result.stderr}")


@pytest.mark.target
@pytest.mark.skipif(
    os.environ.get("III_RUN_SYSTEMD_UNIT_TEST") != "1",
    reason="set III_RUN_SYSTEMD_UNIT_TEST=1 for privileged native-systemd tests",
)
def test_boot_restart_failure_recovery_and_release_switch(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    if _run(["docker", "image", "inspect", IMAGE], check=False).returncode != 0:
        _run(
            [
                "docker",
                "build",
                "--tag",
                IMAGE,
                str(ROOT / "deployment/ansible/tests/target"),
            ]
        )
    payload = tmp_path / "payload"
    unit_root = payload / "etc/systemd/system"
    unit_root.mkdir(parents=True)
    for name in (
        "iii-system-daemon.service",
        "iii-runtime-api.service",
        "iii.target",
    ):
        shutil.copy2(ROOT / "deployment/systemd" / name, unit_root / name)
    (unit_root / "iii-deployment-receiver.service").write_text(
        "[Unit]\nDescription=Test receiver\n"
        "[Service]\nExecStart=/bin/sleep infinity\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    launcher = payload / "usr/libexec/iii/iii-release-launch"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "deployment/host/iii-release-launch", launcher)
    launcher.chmod(0o755)
    runtime_env = payload / "etc/iii/runtime.env"
    runtime_env.parent.mkdir(parents=True)
    _write(payload / "etc/iii/host-unit-contract.json", UNIT_CONTRACT)
    runtime_env.write_text(
        "III_SYSTEM_PROFILE=real\nIII_RUNTIME_API_PROFILE=real\n"
        "III_RECEIVER_CLOCK_STATE_PATH=/var/lib/iii/deployment/clock-state.json\n"
    )
    secret = payload / "etc/iii/secrets/runtime-api.env"
    secret.parent.mkdir(parents=True)
    secret.write_text(
        "III_RUNTIME_API_BROWSER_PASSWORD=test-only\n"
        "III_RUNTIME_API_CLI_TOKEN=test-only\n"
    )
    release_a, selector_a = _release(payload, "a")
    release_b, selector_b = _release(payload, "b")
    _broken, selector_broken = _release(payload, "broken", broken=True)
    selector_path = payload / "var/lib/iii/deployment/active-selector.json"
    _write(selector_path, selector_a)
    report = {
        "schema": "iii.host-baseline-report/v1",
        "state": "converged",
        "baseline_id": "b" * 64,
        "unit_contract_id": UNIT_CONTRACT["contract_id"],
        "target_definition_id": "a" * 64,
    }
    _write(payload / "var/lib/iii/deployment/host-baseline-report.json", report)
    current = payload / "opt/iii/current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(Path("releases") / release_a)

    container = "iii-systemd-unit-" + os.urandom(6).hex()
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--privileged",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/run/lock",
                "--name",
                container,
                IMAGE,
            ]
        )
        _wait(container, "test -S /run/systemd/private")
        _run(["docker", "cp", str(payload) + "/.", container + ":/"])
        setup = (
            "id iii >/dev/null 2>&1 || useradd --uid 1100 --create-home iii; "
            "install -d -o iii -g iii -m 0750 /run/iii /run/iii/clock-flush "
            "/var/log/iii /var/lib/iii/configuration; "
            "chmod -R a+rX /opt/iii/releases /var/lib/iii/deployment /etc/iii; "
            "systemctl daemon-reload; "
            "systemctl enable --now ssh.service; "
            "systemctl enable iii-deployment-receiver.service iii-system-daemon.service "
            "iii-runtime-api.service iii.target; "
            "systemctl start iii.target"
        )
        _run(["docker", "exec", container, "bash", "-lc", setup])
        _wait(
            container,
            "systemctl is-active --quiet iii-system-daemon.service iii-runtime-api.service",
        )
        _wait(container, f'test "$(cat /run/iii/system-daemon-release)" = {release_a}')
        _run(
            [
                "docker",
                "exec",
                container,
                "systemctl",
                "kill",
                "--signal=KILL",
                "iii-system-daemon.service",
            ]
        )
        assert (
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "systemctl",
                    "is-active",
                    "iii-runtime-api.service",
                ]
            ).stdout.strip()
            == "active"
        )
        assert (
            _run(
                ["docker", "exec", container, "systemctl", "is-active", "ssh.service"]
            ).stdout.strip()
            == "active"
        )
        _wait(container, "systemctl is-active --quiet iii-system-daemon.service")

        def switch(selector: dict) -> None:
            local = tmp_path / "selector.json"
            _write(local, selector)
            _run(
                [
                    "docker",
                    "cp",
                    str(local),
                    container + ":/var/lib/iii/deployment/active-selector.json",
                ]
            )
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "ln",
                    "-sfn",
                    "releases/" + selector["release_id"],
                    "/opt/iii/current",
                ]
            )

        _run(["docker", "exec", container, "systemctl", "stop", "iii.target"])
        switch(selector_broken)
        assert (
            _run(
                ["docker", "exec", container, "systemctl", "start", "iii.target"],
                check=False,
            ).returncode
            != 0
        )
        _wait(container, "systemctl is-failed --quiet iii-system-daemon.service")
        assert (
            _run(
                [
                    "docker",
                    "exec",
                    container,
                    "systemctl",
                    "is-active",
                    "iii-deployment-receiver.service",
                ]
            ).stdout.strip()
            == "active"
        )
        assert (
            _run(
                ["docker", "exec", container, "systemctl", "is-active", "ssh.service"]
            ).stdout.strip()
            == "active"
        )
        switch(selector_b)
        _run(["docker", "exec", container, "systemctl", "reset-failed"])
        _run(["docker", "exec", container, "systemctl", "start", "iii.target"])
        _wait(container, f'test "$(cat /run/iii/runtime-api-release)" = {release_b}')

        _run(["docker", "restart", container])
        _wait(container, "test -S /run/systemd/private")
        _wait(
            container,
            "systemctl is-active --quiet iii-deployment-receiver.service "
            "iii-system-daemon.service iii-runtime-api.service",
        )
    finally:
        _run(["docker", "rm", "--force", container], check=False)
