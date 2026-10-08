from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "install_gc.py"
SPEC = spec_from_file_location("install_gc", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
install_gc = module_from_spec(SPEC)
sys.modules[SPEC.name] = install_gc
SPEC.loader.exec_module(install_gc)
CLI_PACKAGE_ROOT = SCRIPT.parents[1] / "tools/III-Drone-CLI"
sys.path.insert(0, str(CLI_PACKAGE_ROOT))
from iii.qgc import _unit_selects_binary


def _write(path: Path, contents: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    path.chmod(mode)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for relative in (
        "tools/III-Drone-CLI/iii",
        "src/III-Drone-Contracts/iii_drone_contracts",
        "src/III-Drone-GC/docker",
        "src/III-Drone-GC/frontend",
        "scripts/workspace",
        "deps",
    ):
        (root / relative).mkdir(parents=True)
    _write(root / "tools/III-Drone-CLI/setup.py", "from setuptools import setup\n")
    _write(root / "tools/III-Drone-CLI/iii/__init__.py", "")
    _write(
        root / "tools/III-Drone-CLI/dirty-checkout-change.py",
        "preserve installed source\n",
    )
    _write(root / "tools/III-Drone-CLI/.git", "gitdir: ../.git/modules/cli\n")
    _write(root / "src/III-Drone-Contracts/setup.py", "from setuptools import setup\n")
    _write(root / "src/III-Drone-Contracts/iii_drone_contracts/__init__.py", "")
    _write(root / "src/III-Drone-GC/docker-compose.prod.yml", "services: {}\n")
    _write(root / "src/III-Drone-GC/docker/proxy.Dockerfile", "FROM scratch\n")
    _write(root / "src/III-Drone-GC/frontend/Dockerfile", "FROM scratch\n")
    _write(root / "src/III-Drone-GC/frontend/package.json", "{}\n")
    _write(root / "src/III-Drone-GC/frontend/package-lock.json", "{}\n")
    _write(root / "src/III-Drone-GC/dirty-local-change.txt", "preserve me\n")
    _write(
        root / "scripts/workspace/iii_ground_control.sh", "#!/usr/bin/env bash\n", 0o755
    )
    pin = {
        "architecture": "x86_64",
        "sha256": "",
        "source_commit": "e0816c957602789200ae5ba0af45217f0f2f1db4",
        "size": 0,
        "url": "https://example.invalid/QGroundControl.AppImage",
        "version": "test",
    }
    (root / "deps/qgroundcontrol.json").write_text(json.dumps(pin), encoding="utf-8")
    return root


def _configure(monkeypatch, tmp_path: Path, root: Path, asset: Path):
    monkeypatch.setattr(install_gc, "REPO_ROOT", root)
    monkeypatch.setattr(install_gc, "PIN_PATH", root / "deps/qgroundcontrol.json")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("III_GC_INSTALL_ROOT", str(tmp_path / "install root"))
    raw = asset.read_bytes()
    pin = json.loads((root / "deps/qgroundcontrol.json").read_text(encoding="utf-8"))
    pin.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (root / "deps/qgroundcontrol.json").write_text(json.dumps(pin), encoding="utf-8")
    monkeypatch.setattr(install_gc, "load_pin", lambda: pin)

    def source_identity():
        hashes = {}
        for relative in install_gc.SNAPSHOT_PATHS:
            source = root / relative
            if source.is_dir():
                hashes[relative.as_posix()] = install_gc._tree_hash(source)[0]
            else:
                hashes[relative.as_posix()] = install_gc._sha256(source)
        return {
            "checkout": str(root),
            "revision": "a" * 40,
            "dirty": True,
            "status_porcelain": [" M src/III-Drone-GC/dirty-local-change.txt"],
            "content_sha256": hashes,
        }

    monkeypatch.setattr(install_gc, "_source_identity", source_identity)


def _fake_run(monkeypatch):
    commands = []

    def fake_run(command, *, check=True, environment=None):
        commands.append((list(command), environment))
        if len(command) >= 4 and command[1:3] == ["-m", "venv"]:
            venv = Path(command[3])
            _write(venv / "bin/python", "#!/bin/sh\nexit 0\n", 0o755)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "-m" in command and "pip" in command and "install" in command:
            _write(
                Path(command[0]).parent / "iii",
                f"#!{command[0]}\necho native-iii\n",
                0o755,
            )
        if command[-1:] == ["--help"]:
            assert Path(command[0]).is_file()
        if command[:2] == ["docker", "compose"]:
            assert "up" not in command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(install_gc, "_run", fake_run)
    return commands


def test_qgc_manifest_pins_confirmed_linux_asset_and_source_commit():
    pin = json.loads((SCRIPT.parents[1] / "deps/qgroundcontrol.json").read_text())

    assert pin == {
        "architecture": "x86_64",
        "sha256": "06969c67ef58ea063def0a8271447a1cc385438c4a7df36813315b4475146737",
        "size": 180816376,
        "source_commit": "e0816c957602789200ae5ba0af45217f0f2f1db4",
        "url": "https://github.com/mavlink/qgroundcontrol/releases/download/v5.0.8/QGroundControl-x86_64.AppImage",
        "version": "5.0.8",
    }


@pytest.mark.parametrize("profile", ["dev", "deploy"])
def test_install_stages_verified_asset_cli_and_buildable_snapshot_without_starting_services(
    monkeypatch, tmp_path, profile
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    commands = _fake_run(monkeypatch)

    plan = install_gc.install(profile, qgc_asset=asset)

    install_root = Path(plan["install_root"])
    assert (
        install_root / "qgc/QGroundControl.AppImage"
    ).read_bytes() == asset.read_bytes()
    assert json.loads((install_root / "qgroundcontrol.json").read_text()) == json.loads(
        (root / "deps/qgroundcontrol.json").read_text()
    )
    metadata = json.loads((install_root / "install.json").read_text())
    assert metadata["profile"] == profile
    assert metadata["checkout"]["revision"] == "a" * 40
    assert metadata["checkout"]["dirty"] is True
    snapshot = install_root / "workspace"
    assert (
        snapshot / "src/III-Drone-GC/dirty-local-change.txt"
    ).read_text() == "preserve me\n"
    assert (
        snapshot / "tools/III-Drone-CLI/dirty-checkout-change.py"
    ).read_text() == "preserve installed source\n"
    assert not (snapshot / "tools/III-Drone-CLI/.git").exists()
    assert (snapshot / "source-manifest.json").is_file()
    assert Path(plan["ui_launcher"]).is_file()
    assert Path(plan["host_cli"]).is_file()
    assert os.access(plan["host_cli"], os.X_OK)
    assert str(install_root / "venv/bin/iii") in Path(plan["host_cli"]).read_text()
    unit = Path(plan["qgc_service"])
    assert f'ExecStart="{plan["qgc"]}"' in unit.read_text()
    assert "KillSignal=SIGINT\nTimeoutStopSec=20\n" in unit.read_text()
    cli_script = install_root / "venv/bin/iii"
    assert cli_script.read_text().splitlines()[0] == f"#!{install_root}/venv/bin/python"
    assert "III_GC_INSTALL_ROOT" in Path(plan["host_cli"]).read_text()
    systemd_show = {"ExecStart": f"{{ path={plan['qgc']} ; argv[]={plan['qgc']} }}"}
    assert _unit_selects_binary(systemd_show, plan["qgc"])
    compose_calls = [
        command
        for command, _environment in commands
        if command[:2] == ["docker", "compose"]
    ]
    assert any(command[-2:] == ["config", "--quiet"] for command in compose_calls)
    assert any(command[-1:] == ["build"] for command in compose_calls)
    assert all("up" not in command for command in compose_calls)
    assert [
        command[-1]
        for command, _environment in commands
        if command[:2] == ["systemctl", "--user"]
    ] == ["daemon-reload"]
    pip_call = next(
        command
        for command, _environment in commands
        if "pip" in command and "install" in command
    )
    staged_contracts, staged_cli = map(Path, pip_call[-2:])
    assert staged_contracts.as_posix().endswith("/workspace/src/III-Drone-Contracts")
    assert staged_cli.as_posix().endswith("/workspace/tools/III-Drone-CLI")
    assert staged_contracts.parents[1] == staged_cli.parents[1]


def test_checkout_change_during_staging_is_rejected_before_promotion(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    baseline = install_gc._source_identity()
    calls = 0

    def identity_changes_at_final_check():
        nonlocal calls
        calls += 1
        if calls < 3:
            return baseline
        changed = dict(baseline)
        changed["content_sha256"] = {
            **baseline["content_sha256"],
            "tools/III-Drone-CLI": "f" * 64,
        }
        return changed

    monkeypatch.setattr(install_gc, "_source_identity", identity_changes_at_final_check)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    _fake_run(monkeypatch)

    with pytest.raises(
        install_gc.InstallError, match="checkout inputs changed.*refusing promotion"
    ):
        install_gc.install("dev", qgc_asset=asset)

    assert calls == 3
    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()
    assert not (Path.home() / ".local/bin/iii").exists()
    assert not (Path.home() / ".config/systemd/user/iii-qgc.service").exists()


def test_rerun_replaces_managed_install_without_duplicate_host_paths(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    _fake_run(monkeypatch)

    first = install_gc.install("dev", qgc_asset=asset)
    operator_log = (
        Path.home() / ".local/state/iii/ground-control/ground-control-test.log"
    )
    _write(operator_log, "preserve operator evidence\n")
    second = install_gc.install("dev", qgc_asset=asset)

    assert first["install_root"] == second["install_root"]
    assert Path(second["host_cli"]).is_file()
    assert "managed by III GC installer" in Path(second["host_cli"]).read_text()
    assert Path(second["qgc_service"]).read_text().count("[Service]") == 1
    assert not list(Path(second["install_root"]).parent.glob(".gc.previous-*"))
    assert operator_log.read_text() == "preserve operator evidence\n"


def test_existing_unmanaged_install_root_is_preserved(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    commands = _fake_run(monkeypatch)
    occupied = Path(os.environ["III_GC_INSTALL_ROOT"])
    _write(occupied / "important.txt", "keep this\n")

    with pytest.raises(install_gc.InstallError, match="unmanaged install root"):
        install_gc.install("dev", qgc_asset=asset)

    assert (occupied / "important.txt").read_text() == "keep this\n"
    assert not (Path.home() / ".local/bin/iii").exists()
    assert commands == []


def _legacy_cli_text() -> str:
    return (
        "#!/usr/bin/python3\n"
        "# EASY-INSTALL-ENTRY-SCRIPT: 'iii','console_scripts','iii'\n"
        "import re\nimport sys\n"
        "__requires__ = 'iii'\n"
        "from importlib.metadata import distribution\n"
        "def importlib_load_entry_point(spec, group, name):\n"
        "    return next(ep.load() for ep in distribution('iii').entry_points)\n"
        "load_entry_point = importlib_load_entry_point\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(load_entry_point('iii', 'console_scripts', 'iii')())\n"
    )


def _legacy_unit_text(home: Path) -> str:
    root = home / ".local/share/iii"
    qgc = root / "gc-applications/qgc/current/QGroundControl.AppImage"
    runtime_iii = root / "gc-runtime/venv/bin/iii"
    return (
        "[Unit]\n"
        "Description=III pinned host-native QGroundControl\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target network-online.target\n"
        f"ConditionPathIsExecutable={qgc}\n\n"
        "[Service]\nType=simple\n"
        "Environment=APPIMAGE_EXTRACT_AND_RUN=1\n"
        f"ExecStartPre={runtime_iii} qgc status --require-selected --output=json\n"
        f"ExecStart={qgc}\n"
        f"ExecStopPost={root}/gc-runtime/venv/bin/iii-qgc-config-clean-exit\n"
        "Restart=on-failure\nRestartSec=3\nTimeoutStopSec=20\n"
    )


def test_known_legacy_entry_script_and_active_unit_are_backed_up_and_migrated(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    commands = _fake_run(monkeypatch)
    home = Path.home()
    old_cli = home / ".local/bin/iii"
    old_unit = home / ".config/systemd/user/iii-qgc.service"
    old_cli.parent.mkdir(parents=True, exist_ok=True)
    old_unit.parent.mkdir(parents=True, exist_ok=True)
    _write(old_cli, _legacy_cli_text(), 0o755)
    legacy_unit = _legacy_unit_text(home)
    _write(old_unit, legacy_unit)

    plan = install_gc.install("dev", qgc_asset=asset)

    backup_dirs = list((home / ".local/share/iii/gc-migration-backups").iterdir())
    assert len(backup_dirs) == 1
    backup = backup_dirs[0]
    assert (backup / "iii").read_text() == _legacy_cli_text()
    assert (backup / "iii-qgc.service").read_text() == legacy_unit
    assert json.loads((backup / "backup.json").read_text())["files"].keys() == {
        "iii",
        "iii-qgc.service",
    }
    assert (
        Path(plan["host_cli"])
        .read_text()
        .startswith("#!/bin/sh\n# managed by III GC installer")
    )
    assert 'ExecStart="' + plan["qgc"] + '"' in Path(plan["qgc_service"]).read_text()
    systemctl = [
        command
        for command, _environment in commands
        if command[:2] == ["systemctl", "--user"]
    ]
    assert systemctl == [["systemctl", "--user", "daemon-reload"]]


def test_cli_wrapper_clears_inherited_pythonpath_and_overwrites_install_root(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    _fake_run(monkeypatch)
    plan = install_gc.install("deploy", qgc_asset=asset)
    cli_script = Path(plan["cli"])
    _write(
        cli_script,
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'env': {key: os.environ.get(key) for key in "
        "('PYTHONPATH', 'III_GC_INSTALL_ROOT', 'III_GC_INSTALL_PROFILE')}, "
        "'args': sys.argv[1:]}))\n",
        0o755,
    )

    completed = subprocess.run(
        [plan["host_cli"], "system", "status"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": "/contaminated/workspace",
            "III_GC_INSTALL_ROOT": "/wrong/install",
        },
    )

    actual = json.loads(completed.stdout)
    assert actual["env"]["PYTHONPATH"] is None
    assert actual["env"]["III_GC_INSTALL_ROOT"] == plan["install_root"]
    assert actual["env"]["III_GC_INSTALL_PROFILE"] == "deploy"
    assert actual["args"] == ["system", "status"]


def test_unrelated_user_cli_or_unit_is_preserved_and_rejected(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    home = Path.home()
    old_cli = home / ".local/bin/iii"
    old_cli.parent.mkdir(parents=True, exist_ok=True)
    _write(old_cli, "#!/bin/sh\necho unrelated\n", 0o755)

    with pytest.raises(install_gc.InstallError, match="unmanaged path"):
        install_gc.install("dev", qgc_asset=asset)
    assert old_cli.read_text() == "#!/bin/sh\necho unrelated\n"
    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()

    old_cli.unlink()
    unit = home / ".config/systemd/user/iii-qgc.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    _write(unit, "[Unit]\nDescription=Unrelated service\n")
    with pytest.raises(install_gc.InstallError, match="unmanaged unit"):
        install_gc.install("dev", qgc_asset=asset)
    assert "Unrelated service" in unit.read_text()
    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()


def test_post_promotion_failure_rolls_back_files_and_install_root(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)
    commands = _fake_run(monkeypatch)
    base_run = install_gc._run
    reload_count = 0

    def fail_first_reload(command, *, check=True, environment=None):
        nonlocal reload_count
        if command[:2] == ["systemctl", "--user"]:
            reload_count += 1
            if reload_count == 1:
                commands.append((list(command), environment))
                raise install_gc.InstallError(
                    "command failed (1): systemctl --user daemon-reload\nreload failed"
                )
        return base_run(command, check=check, environment=environment)

    monkeypatch.setattr(install_gc, "_run", fail_first_reload)
    home = Path.home()
    old_cli = home / ".local/bin/iii"
    old_unit = home / ".config/systemd/user/iii-qgc.service"
    _write(old_cli, _legacy_cli_text(), 0o755)
    legacy_unit = _legacy_unit_text(home)
    _write(old_unit, legacy_unit)

    with pytest.raises(install_gc.InstallError, match="reload failed"):
        install_gc.install("dev", qgc_asset=asset)

    assert old_cli.read_text() == _legacy_cli_text()
    assert old_unit.read_text() == legacy_unit
    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()
    assert [
        command
        for command, _environment in commands
        if command[:2] == ["systemctl", "--user"]
    ] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_dry_run_reports_stable_paths_without_creating_install_root(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "unused.asset"
    asset.write_bytes(b"not used for dry-run")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(
        install_gc,
        "check_prerequisites",
        lambda: pytest.fail("dry-run checked host deps"),
    )

    plan = install_gc.install("deploy", dry_run=True)

    assert plan["profile"] == "deploy"
    assert plan["qgc"].endswith("/qgc/QGroundControl.AppImage")
    assert plan["cli"].endswith("/venv/bin/iii")
    assert plan["qgc_service"].endswith("/.config/systemd/user/iii-qgc.service")
    assert not Path(plan["install_root"]).exists()


def test_missing_prerequisite_fails_before_any_install_mutation(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    asset = tmp_path / "qgc.AppImage"
    asset.write_bytes(b"test pinned AppImage")
    _configure(monkeypatch, tmp_path, root, asset)
    monkeypatch.setattr(
        install_gc,
        "check_prerequisites",
        lambda: (_ for _ in ()).throw(
            install_gc.InstallError("Docker Compose is unavailable")
        ),
    )

    with pytest.raises(install_gc.InstallError, match="Docker Compose"):
        install_gc.install("dev", qgc_asset=asset)

    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()
    assert not (Path.home() / ".local/bin/iii").exists()


def test_qgc_hash_and_size_mismatch_are_rejected_before_promotion(
    monkeypatch, tmp_path
):
    root = _repo(tmp_path)
    asset = tmp_path / "corrupt.asset"
    asset.write_bytes(b"corrupt")
    _configure(monkeypatch, tmp_path, root, asset)
    pin = json.loads((root / "deps/qgroundcontrol.json").read_text())
    pin["sha256"] = "0" * 64
    pin["size"] = len(asset.read_bytes()) + 1
    monkeypatch.setattr(install_gc, "load_pin", lambda: pin)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)

    with pytest.raises(install_gc.InstallError, match="size mismatch"):
        install_gc.install("dev", qgc_asset=asset)

    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()


def test_qgc_digest_mismatch_is_rejected_when_size_matches(monkeypatch, tmp_path):
    root = _repo(tmp_path)
    asset = tmp_path / "corrupt.asset"
    asset.write_bytes(b"wrong bytes")
    _configure(monkeypatch, tmp_path, root, asset)
    pin = json.loads((root / "deps/qgroundcontrol.json").read_text())
    pin["sha256"] = "0" * 64
    monkeypatch.setattr(install_gc, "load_pin", lambda: pin)
    monkeypatch.setattr(install_gc, "check_prerequisites", lambda: None)

    with pytest.raises(install_gc.InstallError, match="SHA256 mismatch"):
        install_gc.install("deploy", qgc_asset=asset)

    assert not Path(os.environ["III_GC_INSTALL_ROOT"]).exists()


def test_installed_ground_control_launcher_uses_snapshot_root_override(tmp_path):
    launcher = Path(__file__).with_name("iii_ground_control.sh")
    installed_launcher = tmp_path / "installed/scripts/workspace/iii_ground_control.sh"
    installed_launcher.parent.mkdir(parents=True)
    installed_launcher.write_bytes(launcher.read_bytes())
    installed_launcher.chmod(0o755)
    configured_root = tmp_path / "stable snapshot root"
    completed = subprocess.run(
        [str(installed_launcher), "start", "--dry-run"],
        check=True,
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "III_GC_WORKSPACE_ROOT": str(configured_root),
            "III_GC_LOCK_PATH": str(tmp_path / "gc.lock"),
        },
    )

    assert (
        f"Compose file: {configured_root}/src/III-Drone-GC/docker-compose.prod.yml"
        in completed.stdout
    )
