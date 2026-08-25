#!/usr/bin/env python3
"""Trusted dependency-light baseline check for every editable III repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.repository_root.resolve()
    errors: list[str] = []
    process = subprocess.run(["git", "diff", "--check"], cwd=root, capture_output=True, text=True, check=False)
    if process.returncode:
        errors.append(process.stdout.strip() or "git diff --check failed")
    package_xml = root / "package.xml"
    if package_xml.exists():
        try:
            package = ET.parse(package_xml).getroot()
            if package.tag != "package" or package.findtext("name") is None:
                errors.append("package.xml lacks package/name")
        except ET.ParseError as exc:
            errors.append(f"package.xml: {exc}")
    python_roots = sorted(
        path for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "__init__.py").is_file()
    )
    for python_root in python_roots:
        for source_path in sorted(python_root.rglob("*.py")):
            try:
                compile(source_path.read_bytes(), str(source_path.relative_to(root)), "exec")
            except (OSError, SyntaxError) as exc:
                errors.append(f"Python syntax check failed for {source_path.relative_to(root)}: {exc}")
    if not package_xml.exists() and not (root / "setup.py").exists():
        errors.append("editable III repository has neither package.xml nor setup.py")
    result = {
        "schema": "iii.editable-repository-check/v1",
        "outcome": "pass" if not errors else "failed",
        "repository": root.name,
        "checks": ["git-diff", "package-xml", "python-compile"],
        "errors": errors,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not errors else 30


if __name__ == "__main__":
    raise SystemExit(main())
