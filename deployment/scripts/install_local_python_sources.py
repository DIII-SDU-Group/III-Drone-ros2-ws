#!/usr/bin/env python3
"""Install local Python projects from isolated copies, never from the clone."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

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


def _copy_source(source: Path, destination: Path) -> Path:
    resolved = source.resolve()
    if source.is_symlink() or resolved.is_symlink() or not resolved.is_dir():
        raise ValueError(f"local Python source is unsafe: {source}")
    for directory, directories, files in os.walk(resolved, topdown=True):
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
                raise ValueError(f"local Python source contains a link: {path}")
            if not (path.is_file() or path.is_dir()):
                raise ValueError(f"local Python source contains a special file: {path}")

    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in EXCLUDES or name.endswith((".egg-info", ".pyc"))
        }

    shutil.copytree(resolved, destination, ignore=ignored)
    return destination


def install(pip: Path, sources: list[Path]) -> None:
    resolved_pip = pip.resolve()
    if (
        pip.is_symlink()
        or not resolved_pip.is_file()
        or not os.access(resolved_pip, os.X_OK)
    ):
        raise ValueError("managed pip executable is missing or unsafe")
    if not sources:
        raise ValueError("at least one local Python source is required")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PIP_")
        and key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    with tempfile.TemporaryDirectory(prefix="iii-python-sources-") as temporary:
        root = Path(temporary)
        copies = [
            _copy_source(source, root / f"source-{index}")
            for index, source in enumerate(sources)
        ]
        subprocess.run(
            [
                str(resolved_pip),
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--no-build-isolation",
                "--no-deps",
                *(str(path) for path in copies),
            ],
            check=True,
            env=environment,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pip", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    arguments = parser.parse_args()
    install(arguments.pip, arguments.sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
