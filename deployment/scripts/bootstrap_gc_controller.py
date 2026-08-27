#!/usr/bin/env python3
"""Create a versioned repository-managed controller and re-exec ``iii``.

This entry point intentionally uses only the Python standard library.  It is the
small bootstrap boundary for a clean supported Ubuntu clone; all host mutations
remain behind the subsequently retained ``iii gc provision`` operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

EXCLUDES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
OFFLINE_SCHEMA = "iii.gc-offline-cache/v1"
OFFLINE_ROLES = frozenset(
    {
        "ansible-controller-wheelhouse",
        "gc-runtime-wheelhouse",
        "gc-container-images",
        "arm64-builder-image",
        "apt-packages",
    }
)
PIP_VERSION = "26.2"
PIP_WHEEL = f"pip-{PIP_VERSION}-py3-none-any.whl"
PIP_WHEEL_SHA256 = "931c303696af6fa3417112103b1cad26890e5a07eccb5b99783700e33f2b8aad"
PIP_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/62/36/"
    "a3aed958d60531cb442b7ab4596cda7b3621cfb916f8ae1d6769795c7dc1/" + PIP_WHEEL
)


def _workspace() -> Path:
    root = Path(__file__).resolve().parents[2]
    if (
        not (root / ".git").exists()
        or not (root / "tools/III-Drone-CLI/bin/iii").is_file()
    ):
        raise SystemExit(
            "GC controller bootstrap must run from the III workspace clone"
        )
    return root


def _identity(root: Path) -> str:
    digest = hashlib.sha256()
    paths = []
    for tree in (root / "deployment", root / "tools/III-Drone-CLI"):
        for path in tree.rglob("*"):
            if path.is_symlink():
                raise SystemExit(f"controller source contains a symbolic link: {path}")
            if path.is_file():
                paths.append(path)
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(
            part in EXCLUDES or part.endswith(".egg-info") or part.endswith(".pyc")
            for part in relative.parts
        ):
            continue
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(format(path.stat().st_mode & 0o777, "04o").encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _controller_lock(root: Path) -> Path:
    locks = {
        (3, 10): root / "deployment/ansible/controller-requirements-py310.txt",
        (3, 12): root / "deployment/ansible/controller-requirements-py312.txt",
    }
    lock = locks.get(sys.version_info[:2])
    if lock is None or lock.is_symlink() or not lock.is_file():
        raise SystemExit(
            "GC controller bootstrap requires stock Ubuntu Python 3.10 or 3.12"
        )
    return lock


def _clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PIP_")
        and key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def _run(argv: list[str], *, environment: dict[str, str] | None = None) -> None:
    # Bootstrap diagnostics are never allowed to contaminate structured CLI
    # stdout after the final exec.
    subprocess.run(
        argv,
        check=True,
        env=environment,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise SystemExit("offline controller wheelhouse archive is unsafe")
        archive.extractall(destination, members=members)


def _copy_build_source(source: Path, destination: Path) -> Path:
    """Copy a local Python project without mutating its authenticated source."""
    if source.is_symlink() or not source.is_dir():
        raise SystemExit(f"controller build source is unsafe: {source}")

    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in EXCLUDES or name.endswith((".egg-info", ".pyc"))
        }

    for directory, directories, files in os.walk(source, topdown=True):
        base = Path(directory)
        directories[:] = [
            name
            for name in directories
            if name not in EXCLUDES and not name.endswith(".egg-info")
        ]
        for name in [*directories, *files]:
            path = base / name
            if name in EXCLUDES or name.endswith((".egg-info", ".pyc")):
                continue
            if path.is_symlink():
                raise SystemExit(f"controller build source contains a link: {path}")
            if not (path.is_file() or path.is_dir()):
                raise SystemExit(
                    f"controller build source contains a special file: {path}"
                )
    shutil.copytree(source, destination, ignore=ignored)
    return destination


def _platform_id() -> str:
    try:
        values = {}
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot identify GC bootstrap platform: {exc}") from exc
    machine = {"amd64": "x86_64", "x86_64": "x86_64"}.get(platform.machine())
    if (
        values.get("ID") != "ubuntu"
        or values.get("VERSION_ID")
        not in {
            "22.04",
            "24.04",
        }
        or machine != "x86_64"
    ):
        raise SystemExit("GC controller bootstrap supports Ubuntu 22.04/24.04 x86_64")
    return f"ubuntu-{values['VERSION_ID']}-x86_64"


def _offline_manifest(cache_root: Path) -> dict:
    resolved = cache_root.expanduser().resolve()
    if cache_root.is_symlink() or resolved.is_symlink() or not resolved.is_dir():
        raise SystemExit("prepared-offline cache must be a real directory")
    manifest_path = resolved / "gc-offline-cache.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SystemExit("prepared-offline cache manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read prepared-offline cache: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != OFFLINE_SCHEMA:
        raise SystemExit("prepared-offline cache schema is unsupported")
    if manifest.get("platform_id") != _platform_id():
        raise SystemExit("prepared-offline cache platform differs from this host")
    unsigned = {key: value for key, value in manifest.items() if key != "cache_id"}
    observed_id = hashlib.sha256(
        json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    if manifest.get("cache_id") != observed_id:
        raise SystemExit("prepared-offline cache identity mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise SystemExit("prepared-offline cache artifact inventory is invalid")
    roles = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise SystemExit("prepared-offline cache artifact entry is invalid")
        role = item.get("role")
        relative = PurePosixPath(str(item.get("path", "")))
        if (
            role not in OFFLINE_ROLES
            or role in roles
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise SystemExit("prepared-offline cache role or path is invalid")
        artifact = resolved.joinpath(*relative.parts)
        current = resolved
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SystemExit(f"prepared-offline artifact path is linked: {role}")
        if (
            not artifact.is_file()
            or _sha256(artifact) != item.get("sha256")
            or artifact.stat().st_size != item.get("size")
        ):
            raise SystemExit(f"prepared-offline artifact failed verification: {role}")
        roles.append(role)
    if frozenset(roles) != OFFLINE_ROLES:
        raise SystemExit("prepared-offline cache role inventory is incomplete")
    return manifest


def _offline_wheelhouse(cache_root: Path, scratch: Path, manifest: dict) -> Path:
    item = next(
        (
            value
            for value in manifest.get("artifacts", [])
            if value.get("role") == "ansible-controller-wheelhouse"
        ),
        None,
    )
    if not isinstance(item, dict):
        raise SystemExit("prepared-offline cache lacks the controller wheelhouse")
    relative = PurePosixPath(str(item.get("path", "")))
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SystemExit("prepared-offline controller artifact path is unsafe")
    archive = cache_root.joinpath(*relative.parts)
    if (
        not archive.is_file()
        or archive.is_symlink()
        or _sha256(archive) != item.get("sha256")
        or archive.stat().st_size != item.get("size")
    ):
        raise SystemExit("prepared-offline controller artifact failed verification")
    destination = scratch / "wheelhouse"
    destination.mkdir(mode=0o700)
    _safe_extract(archive, destination)
    if not (destination / PIP_WHEEL).is_file():
        raise SystemExit(
            f"prepared-offline controller wheelhouse lacks pinned {PIP_WHEEL}"
        )
    return destination


def _download_pip_wheel(destination: Path) -> Path:
    wheel = destination / PIP_WHEEL
    request = urllib.request.Request(
        PIP_WHEEL_URL,
        headers={"User-Agent": "III-GC-controller-bootstrap/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, wheel.open(
            "xb"
        ) as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise SystemExit(f"cannot fetch the pinned pip bootstrap wheel: {exc}") from exc
    if _sha256(wheel) != PIP_WHEEL_SHA256:
        wheel.unlink(missing_ok=True)
        raise SystemExit("pinned pip bootstrap wheel failed SHA-256 verification")
    return wheel


def _create_environment(destination: Path, *, pip_wheel: Path) -> Path:
    # ``--without-pip`` works on stock Ubuntu's Python stdlib even when the
    # separately packaged ensurepip/python3-venv seed is absent.  The selected
    # pip wheel is authenticated above or by the complete offline-cache identity.
    _run(
        [sys.executable, "-m", "venv", "--without-pip", str(destination)],
        environment=_clean_environment(),
    )
    python = destination / "bin/python"
    environment = _clean_environment()
    environment["PYTHONPATH"] = str(pip_wheel)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            str(pip_wheel),
        ],
        environment=environment,
    )
    pip = destination / "bin/pip"
    if not pip.is_file():
        raise SystemExit("pinned pip bootstrap did not create the controller installer")
    return pip


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def bootstrap(cache: Path | None) -> Path:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise SystemExit("GC controller bootstrap supports only Linux x86_64")
    root = _workspace()
    controller_lock = _controller_lock(root)
    identity = _identity(root)
    offline_manifest = None
    if cache is not None:
        offline_manifest = _offline_manifest(cache)
        cache_id = str(offline_manifest["cache_id"])
        identity = hashlib.sha256(
            (identity + ":" + cache_id).encode("ascii")
        ).hexdigest()
    base = Path.home() / ".local/share/iii/controller"
    environments = base / "environments"
    environments.mkdir(parents=True, exist_ok=True, mode=0o700)
    if environments.is_symlink() or not environments.is_dir():
        raise SystemExit("controller environment root is unsafe")
    os.chmod(environments, 0o700)
    destination = environments / identity
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SystemExit("selected controller environment path is unsafe")
    if (
        not (destination / "bin/iii").is_file()
        or not (destination / "bin/ansible-playbook").is_file()
    ):
        if destination.exists():
            quarantine = environments / f".{identity}.invalid-{os.getpid()}"
            if quarantine.exists() or quarantine.is_symlink():
                raise SystemExit("controller quarantine path already exists")
            os.replace(destination, quarantine)
        try:
            with tempfile.TemporaryDirectory(prefix="iii-gc-controller-") as temporary:
                scratch = Path(temporary)
                if cache is None:
                    wheelhouse = None
                    pip_wheel = _download_pip_wheel(scratch)
                else:
                    wheelhouse = _offline_wheelhouse(
                        cache.resolve(), scratch, offline_manifest
                    )
                    pip_wheel = wheelhouse / PIP_WHEEL
                pip = _create_environment(destination, pip_wheel=pip_wheel)
                if cache is None:
                    _run(
                        [
                            str(pip),
                            "install",
                            "--isolated",
                            "--disable-pip-version-check",
                            "--require-hashes",
                            "--requirement",
                            str(controller_lock),
                        ],
                        environment=_clean_environment(),
                    )
                    source_root = scratch / "sources"
                    source_root.mkdir(mode=0o700)
                    cli_source = _copy_build_source(
                        root / "tools/III-Drone-CLI", source_root / "cli"
                    )
                    deployment_source = _copy_build_source(
                        root / "deployment", source_root / "deployment"
                    )
                    argv = [
                        str(pip),
                        "install",
                        "--isolated",
                        "--disable-pip-version-check",
                        "--no-build-isolation",
                        "--no-deps",
                        str(cli_source),
                        str(deployment_source),
                    ]
                    _run(argv, environment=_clean_environment())
                else:
                    _run(
                        [
                            str(pip),
                            "install",
                            "--isolated",
                            "--no-index",
                            "--find-links",
                            str(wheelhouse),
                            "--require-hashes",
                            "--requirement",
                            str(controller_lock),
                        ],
                        environment=_clean_environment(),
                    )
                    _run(
                        [
                            str(pip),
                            "install",
                            "--isolated",
                            "--no-index",
                            "--find-links",
                            str(wheelhouse),
                            "--no-deps",
                            "iii==0.2.0",
                            "iii-deployment==0.1.0",
                        ],
                        environment=_clean_environment(),
                    )
            _run(
                [
                    str(destination / "bin/python"),
                    str(root / "deployment/scripts/verify_python_lock.py"),
                    "--lock",
                    str(controller_lock),
                    "--local",
                    "iii==0.2.0",
                    "--local",
                    "iii-deployment==0.1.0",
                ],
                environment=_clean_environment(),
            )
            (destination / "iii-controller-id").write_text(
                identity + "\n", encoding="utf-8"
            )
            os.chmod(destination / "iii-controller-id", 0o600)
        except BaseException:
            if destination.exists() and not destination.is_symlink():
                failed = environments / f".{identity}.failed-{os.getpid()}"
                if not failed.exists() and not failed.is_symlink():
                    os.replace(destination, failed)
            raise
    link = base / "venv"
    if link.exists() and not link.is_symlink():
        raise SystemExit("controller venv selector exists but is not a symbolic link")
    if not link.is_symlink() or link.resolve() != destination:
        _replace_link(link, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-cache", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    forwarded = list(args.arguments)
    if forwarded and forwarded[0] == "--":
        forwarded.pop(0)
    if not forwarded:
        forwarded = ["gc", "provision", "--help"]
    environment = bootstrap(args.offline_cache)
    os.environ["III_GC_CONTROLLER_BOOTSTRAPPED"] = "1"
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        os.environ.pop(key, None)
    os.execv(environment / "bin/iii", [str(environment / "bin/iii"), *forwarded])
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
