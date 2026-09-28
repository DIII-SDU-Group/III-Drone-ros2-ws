#!/usr/bin/env python3
"""Install the checkout-pinned III ground-computer operator stack."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import subprocess
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = REPO_ROOT / "deps" / "qgroundcontrol.json"
SNAPSHOT_PATHS = (
    Path("tools/III-Drone-CLI"),
    Path("src/III-Drone-GC"),
    Path("src/III-Drone-Contracts"),
    Path("scripts/workspace/iii_ground_control.sh"),
)
IGNORE_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "*.egg-info",
    "*.pyc",
}


class InstallError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, text=True, capture_output=True, check=False, env=environment
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise InstallError(
            f"command failed ({completed.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return completed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hash(root: Path) -> tuple[str, list[dict[str, str]]]:
    records = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name
            not in {
                ".git",
                "__pycache__",
                "node_modules",
                "dist",
                "build",
                ".pytest_cache",
            }
            and not name.endswith(".egg-info")
        ]
        for filename in filenames:
            if filename in {".git", ".package-lock.json"} or filename.endswith(".pyc"):
                continue
            path = Path(directory) / filename
            if path.is_file():
                records.append(
                    {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}
                )
    records.sort(key=lambda record: record["path"])
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), records


def _ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in IGNORE_NAMES or name.endswith(".egg-info") or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def load_pin() -> dict[str, Any]:
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read QGroundControl pin {PIN_PATH}: {exc}") from exc
    required = {"version", "url", "sha256", "size", "architecture", "source_commit"}
    if not required.issubset(pin):
        raise InstallError(
            f"QGroundControl pin is missing keys: {sorted(required - pin.keys())}"
        )
    if pin["architecture"] != "x86_64" or pin["size"] <= 0:
        raise InstallError(
            "QGroundControl pin must specify a positive x86_64 asset size"
        )
    return pin


def install_root_from_environment(
    environment: dict[str, str] | os._Environ[str]
) -> Path:
    configured = environment.get("III_GC_INSTALL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    data_home_value = environment.get("XDG_DATA_HOME") or str(
        Path.home() / ".local/share"
    )
    data_home = Path(data_home_value)
    return (data_home / "iii" / "gc").expanduser().resolve()


def check_prerequisites() -> None:
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise InstallError("the standalone GC installer supports Linux x86_64 only")
    if sys.version_info < (3, 10):
        raise InstallError("Python 3.10 or newer is required")
    for relative in (
        Path("tools/III-Drone-CLI/setup.py"),
        Path("src/III-Drone-Contracts/setup.py"),
        *SNAPSHOT_PATHS,
    ):
        if not (REPO_ROOT / relative).exists():
            raise InstallError(
                f"required checkout input is missing: {REPO_ROOT / relative}"
            )
    try:
        import venv  # noqa: F401
    except ImportError as exc:
        raise InstallError("the Python venv module is required") from exc
    for executable in ("git", "docker", "curl", "ssh", "systemctl"):
        if shutil.which(executable) is None:
            raise InstallError(f"required executable is missing: {executable}")
    for command in (
        ["docker", "compose", "version"],
        ["docker", "info"],
        ["systemctl", "--user", "show-environment"],
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
    ):
        _run(command)


def _source_identity() -> dict[str, Any]:
    revision_result = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    status_result = _run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )
    hashes = {}
    for relative in SNAPSHOT_PATHS:
        tree_hash, _records = (
            _tree_hash(REPO_ROOT / relative)
            if (REPO_ROOT / relative).is_dir()
            else (
                _sha256(REPO_ROOT / relative),
                [],
            )
        )
        hashes[relative.as_posix()] = tree_hash
    return {
        "checkout": str(REPO_ROOT),
        "revision": revision_result.stdout.strip(),
        "dirty": bool(status_result.stdout.strip()),
        "status_porcelain": status_result.stdout.splitlines(),
        "content_sha256": hashes,
    }


def _verify_source_identity(expected: dict[str, Any]) -> None:
    if _source_identity() != expected:
        raise InstallError("checkout inputs changed during install; refusing promotion")


def _copy_snapshot(
    destination: Path, expected_identity: dict[str, Any]
) -> dict[str, Any]:
    destination.mkdir(parents=True)
    source_records: dict[str, Any] = {}
    for relative in SNAPSHOT_PATHS:
        source = REPO_ROOT / relative
        target = destination / relative
        if not source.exists():
            raise InstallError(f"snapshot input is missing: {source}")
        if source.is_dir():
            shutil.copytree(source, target, ignore=_ignore)
            tree_hash, records = _tree_hash(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            tree_hash, records = _sha256(target), [
                {"path": target.name, "sha256": _sha256(target)}
            ]
        expected_hash = expected_identity["content_sha256"].get(relative.as_posix())
        if tree_hash != expected_hash:
            raise InstallError(
                f"checkout input changed while staging {relative}; refusing promotion"
            )
        source_records[relative.as_posix()] = {"sha256": tree_hash, "files": records}
    manifest = {"files": source_records, **expected_identity}
    (destination / "source-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _write_text(path: Path, contents: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(contents, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def _verify_qgc(asset: Path, pin: dict[str, Any]) -> None:
    actual_size = asset.stat().st_size
    actual_hash = _sha256(asset)
    if actual_size != pin["size"]:
        raise InstallError(
            f"QGroundControl size mismatch: expected {pin['size']}, got {actual_size}"
        )
    if actual_hash.lower() != pin["sha256"].lower():
        raise InstallError(
            f"QGroundControl SHA256 mismatch: expected {pin['sha256']}, got {actual_hash}"
        )


def _download_qgc(
    pin: dict[str, Any], destination: Path, local_asset: Path | None
) -> None:
    if local_asset:
        shutil.copy2(local_asset, destination)
    else:
        _run(
            [
                "curl",
                "--fail",
                "--location",
                "--retry",
                "3",
                pin["url"],
                "--output",
                str(destination),
            ]
        )
    _verify_qgc(destination, pin)
    destination.chmod(0o755)


def _write_qgc_service(path: Path, install_root: Path) -> None:
    appimage = install_root / "qgc" / "QGroundControl.AppImage"
    systemd_path = str(appimage).replace("\\", "\\\\").replace('"', '\\"')
    contents = (
        "[Unit]\n"
        "Description=III pinned host-native QGroundControl\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=APPIMAGE_EXTRACT_AND_RUN=1\n"
        f'ExecStart="{systemd_path}"\n'
        "KillSignal=SIGINT\n"
        "TimeoutStopSec=20\n"
        "Restart=no\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    _write_text(path, contents)


LEGACY_CLI_MARKER = "# EASY-INSTALL-ENTRY-SCRIPT: 'iii','console_scripts','iii'"
MANAGED_CLI_MARKER = "# managed by III GC installer"


def _classify_cli(path: Path, expected_target: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    if path.is_symlink() and path.resolve(strict=False) == expected_target.resolve(
        strict=False
    ):
        return "managed"
    if path.is_file():
        contents = path.read_text(encoding="utf-8", errors="replace")
        if contents.startswith("#!/") and MANAGED_CLI_MARKER in contents:
            return "managed"
        if (
            contents.startswith("#!/usr/bin/python3\n")
            and LEGACY_CLI_MARKER in contents
            and "__requires__ = 'iii'" in contents
            and "load_entry_point('iii', 'console_scripts', 'iii')" in contents
        ):
            return "legacy"
    raise InstallError(f"refusing to replace an existing unmanaged path: {path}")


def _unit_lines(contents: str) -> dict[str, str]:
    return {
        key: value
        for line in contents.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for key, value in [line.split("=", 1)]
    }


def _classify_unit(path: Path, install_root: Path, home: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "absent"
    values = _unit_lines(path.read_text(encoding="utf-8"))
    new_exec = values.get("ExecStart", "").strip().strip('"')
    if (
        values.get("Description") == "III pinned host-native QGroundControl"
        and values.get("ExecStart")
        == f'"{install_root / "qgc/QGroundControl.AppImage"}"'
    ) or (
        values.get("Description") == "III pinned QGroundControl"
        and new_exec == str(install_root / "qgc/QGroundControl.AppImage")
    ):
        return "managed"

    old_root = home / ".local/share/iii"
    old_qgc = old_root / "gc-applications/qgc/current/QGroundControl.AppImage"
    old_runtime = old_root / "gc-runtime/venv/bin/iii"
    expected_legacy = {
        "Description": "III pinned host-native QGroundControl",
        "ConditionPathIsExecutable": str(old_qgc),
        "Environment": "APPIMAGE_EXTRACT_AND_RUN=1",
        "ExecStart": str(old_qgc),
        "ExecStartPre": f"{old_runtime} qgc status --require-selected --output=json",
        "ExecStopPost": f"{old_root}/gc-runtime/venv/bin/iii-qgc-config-clean-exit",
    }
    if all(values.get(key) == expected for key, expected in expected_legacy.items()):
        return "legacy"
    raise InstallError(f"refusing to replace an existing unmanaged unit: {path}")


def _cli_wrapper(install_root: Path, profile: str) -> str:
    cli = install_root / "venv/bin/iii"
    return (
        "#!/bin/sh\n"
        f"{MANAGED_CLI_MARKER}\n"
        "unset PYTHONPATH\n"
        f"export III_GC_INSTALL_ROOT={shlex.quote(str(install_root))}\n"
        f"export III_GC_INSTALL_PROFILE={shlex.quote(profile)}\n"
        f'exec {shlex.quote(str(cli))} "$@"\n'
    )


def _write_cli_wrapper(path: Path, install_root: Path, profile: str) -> None:
    _write_text(path, _cli_wrapper(install_root, profile), mode=0o755)


def _capture_path(path: Path, destination: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        destination.symlink_to(os.readlink(path))
    else:
        shutil.copy2(path, destination)
    return True


def _restore_path(path: Path, saved_path: Path, existed: bool) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    if existed:
        os.replace(saved_path, path)


def _preserve_legacy_files(
    *, cli_path: Path, unit_path: Path, cli_kind: str, unit_kind: str, home: Path
) -> Path | None:
    legacy_files = {}
    if cli_kind == "legacy":
        legacy_files["iii"] = cli_path
    if unit_kind == "legacy":
        legacy_files["iii-qgc.service"] = unit_path
    if not legacy_files:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = home / ".local/share/iii/gc-migration-backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    records = {}
    for name, source in legacy_files.items():
        destination = backup / name
        shutil.copy2(source, destination)
        records[name] = {"original_path": str(source), "sha256": _sha256(destination)}
    (backup / "backup.json").write_text(
        json.dumps(
            {"schema": "iii.gc-legacy-backup/v1", "files": records},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return backup


def _prepare_venv(venv_path: Path, cli_source: Path, contracts_source: Path) -> None:
    _run([sys.executable, "-m", "venv", str(venv_path)])
    python = venv_path / "bin" / "python"
    _run([str(python), "-m", "pip", "install", str(contracts_source), str(cli_source)])
    _run([str(venv_path / "bin" / "iii"), "--help"])


def _rebase_venv(venv_path: Path, staged_root: Path, install_root: Path) -> None:
    """Replace staging paths in generated venv metadata and launch scripts."""

    old = str(staged_root).encode()
    new = str(install_root).encode()
    for path in venv_path.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        contents = path.read_bytes()
        if b"\0" not in contents and old in contents:
            path.write_bytes(contents.replace(old, new))


def _compose(stage_workspace: Path, profile: str) -> None:
    compose_file = stage_workspace / "src/III-Drone-GC/docker-compose.prod.yml"
    base = [
        "docker",
        "compose",
        "-p",
        f"iii-ground-control-{profile}",
        "-f",
        str(compose_file),
    ]
    environment = {**os.environ, "III_GC_IMAGE_TAG": profile}
    # Config validation and image build are the entire install-time runtime
    # boundary: never start the browser UI or QGroundControl here.
    _run(base + ["config", "--quiet"], environment=environment)
    _run(base + ["build"], environment=environment)


def _promote(staged_root: Path, install_root: Path) -> Path | None:
    backup = install_root.with_name(f".{install_root.name}.previous-{os.getpid()}")
    moved_old = False
    try:
        if install_root.exists():
            os.replace(install_root, backup)
            moved_old = True
        os.replace(staged_root, install_root)
    except BaseException:
        if moved_old and not install_root.exists():
            os.replace(backup, install_root)
        raise
    return backup if moved_old else None


def _verify_managed_install_root(install_root: Path) -> None:
    """Never replace a directory that this installer did not create."""

    if not install_root.exists() and not install_root.is_symlink():
        return
    if install_root.is_symlink() or not install_root.is_dir():
        raise InstallError(
            f"refusing to replace an unmanaged install root: {install_root}"
        )
    manifest = install_root / "install.json"
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            f"refusing to replace an unmanaged install root: {install_root} ({exc})"
        ) from exc
    paths = metadata.get("paths") if isinstance(metadata, dict) else None
    if (
        metadata.get("schema") != "iii.gc-install/v1"
        or not isinstance(paths, dict)
        or paths.get("install_root") != str(install_root)
        or paths.get("cli") != str(install_root / "venv/bin/iii")
        or paths.get("qgc") != str(install_root / "qgc/QGroundControl.AppImage")
    ):
        raise InstallError(
            f"refusing to replace an unmanaged install root: {install_root}"
        )


def install(
    profile: str, *, dry_run: bool = False, qgc_asset: Path | None = None
) -> dict[str, Any]:
    if profile not in {"dev", "deploy"}:
        raise InstallError("profile must be dev or deploy")
    pin = load_pin()
    environment = os.environ
    install_root = install_root_from_environment(environment)
    home = Path.home()
    host_cli = home / ".local/bin/iii"
    user_unit = home / ".config/systemd/user/iii-qgc.service"
    source_identity = (
        _source_identity()
        if not dry_run
        else {"revision": "current checkout", "dirty": "recorded at install"}
    )
    plan = {
        "profile": profile,
        "checkout": str(REPO_ROOT),
        "install_root": str(install_root),
        "qgc": str(install_root / "qgc/QGroundControl.AppImage"),
        "cli": str(install_root / "venv/bin/iii"),
        "ui_launcher": str(
            install_root / "workspace/scripts/workspace/iii_ground_control.sh"
        ),
        "host_cli": str(host_cli),
        "qgc_service": str(user_unit),
        "qgc_version": pin["version"],
        "revision": source_identity["revision"],
        "dirty_checkout": source_identity["dirty"],
        "image_action": "build only; no containers or GUI processes are started",
    }
    if dry_run:
        return plan

    _verify_managed_install_root(install_root)
    cli_kind = _classify_cli(host_cli, install_root / "venv/bin/iii")
    unit_kind = _classify_unit(user_unit, install_root, home)
    print(
        "[1/6] Checking Linux x86_64, Docker Compose, Python venv, Git and user systemd",
        file=sys.stderr,
    )
    check_prerequisites()
    local_asset = qgc_asset.expanduser().resolve() if qgc_asset else None
    if local_asset and not local_asset.is_file():
        raise InstallError(f"local QGroundControl asset does not exist: {local_asset}")
    install_root.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=f".{install_root.name}.stage-", dir=install_root.parent)
    )
    staged_root = staging_parent / install_root.name
    try:
        staged_root.mkdir()
        (staged_root / "qgc").mkdir()
        qgc_path = staged_root / "qgc/QGroundControl.AppImage"
        print("[2/6] Acquiring and verifying pinned QGroundControl", file=sys.stderr)
        _download_qgc(pin, qgc_path, local_asset)
        shutil.copy2(PIN_PATH, staged_root / "qgroundcontrol.json")
        print(
            "[3/6] Creating checkout-bound GUI and source identity snapshot",
            file=sys.stderr,
        )
        _copy_snapshot(staged_root / "workspace", source_identity)
        print(
            "[4/6] Installing the checkout CLI and Contracts in an isolated venv",
            file=sys.stderr,
        )
        _prepare_venv(
            staged_root / "venv",
            staged_root / "workspace/tools/III-Drone-CLI",
            staged_root / "workspace/src/III-Drone-Contracts",
        )
        _rebase_venv(staged_root / "venv", staged_root, install_root)
        install_metadata = {
            "schema": "iii.gc-install/v1",
            "profile": profile,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "checkout": source_identity,
            "qgroundcontrol": pin,
            "paths": plan,
        }
        (staged_root / "install.json").write_text(
            json.dumps(install_metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "[5/6] Validating Compose and building GUI images (no services started)",
            file=sys.stderr,
        )
        _compose(staged_root / "workspace", profile)

        staged_unit = staging_parent / "iii-qgc.service"
        _write_qgc_service(staged_unit, install_root)
        staged_cli = staging_parent / "iii"
        _write_cli_wrapper(staged_cli, install_root, profile)
        # Detect changes before preserving or preparing any host paths.
        _verify_source_identity(source_identity)
        legacy_backup = _preserve_legacy_files(
            cli_path=host_cli,
            unit_path=user_unit,
            cli_kind=cli_kind,
            unit_kind=unit_kind,
            home=home,
        )
        rollback_cli = staging_parent / "rollback-iii"
        rollback_unit = staging_parent / "rollback-iii-qgc.service"
        old_cli_existed = _capture_path(host_cli, rollback_cli)
        old_unit_existed = _capture_path(user_unit, rollback_unit)
        host_cli.parent.mkdir(parents=True, exist_ok=True)
        user_unit.parent.mkdir(parents=True, exist_ok=True)
        _verify_source_identity(source_identity)
        _verify_managed_install_root(install_root)
        previous_root = _promote(staged_root, install_root)
        try:
            os.replace(staged_cli, host_cli)
            os.replace(staged_unit, user_unit)
            print(
                "[6/6] Refreshing the user QGroundControl service definition",
                file=sys.stderr,
            )
            _run(["systemctl", "--user", "daemon-reload"])
        except BaseException as install_error:
            rollback_errors = []

            def rollback(action: Any) -> None:
                try:
                    action()
                except BaseException as rollback_error:
                    rollback_errors.append(str(rollback_error))

            rollback(lambda: _restore_path(host_cli, rollback_cli, old_cli_existed))
            rollback(lambda: _restore_path(user_unit, rollback_unit, old_unit_existed))

            def restore_install_root() -> None:
                if install_root.is_dir():
                    shutil.rmtree(install_root)
                elif install_root.exists() or install_root.is_symlink():
                    install_root.unlink()
                if previous_root is not None and previous_root.exists():
                    os.replace(previous_root, install_root)

            rollback(restore_install_root)
            rollback(lambda: _run(["systemctl", "--user", "daemon-reload"]))
            if rollback_errors:
                raise InstallError(
                    f"install failed ({install_error}); rollback also failed: "
                    + "; ".join(rollback_errors)
                ) from install_error
            raise
        if previous_root is not None and previous_root.exists():
            shutil.rmtree(previous_root)
        if legacy_backup is not None:
            print(
                f"Preserved legacy CLI/service files at {legacy_backup}",
                file=sys.stderr,
            )
        print(f"Installed profile={profile} root={install_root}", file=sys.stderr)
        return plan
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("dev", "deploy"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show install paths and actions without mutation",
    )
    parser.add_argument(
        "--qgc-asset",
        type=Path,
        help="use a local AppImage and verify it against the pin",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = install(args.profile, dry_run=args.dry_run, qgc_asset=args.qgc_asset)
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"GC install failed: {exc}", file=sys.stderr)
        return 2
    print(
        "Ground-computer install plan:"
        if args.dry_run
        else "Ground-computer install complete:"
    )
    for key, value in plan.items():
        print(f"  {key}: {value}")
    if args.dry_run:
        print("  no files, services, Docker images, or GUI processes were changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
