#!/usr/bin/env python3
"""Verify an installed environment exactly matches a hash-lock plus local wheels."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re


PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \\]+)(?:[ \\]|$)")


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def expected_distributions(lock: Path, local: list[str]) -> dict[str, str]:
    if lock.is_symlink() or not lock.is_file():
        raise ValueError("Python dependency lock must be a real file")
    expected: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            expected[canonical_name(match.group(1))] = match.group(2)
    for item in local:
        match = PIN.fullmatch(item)
        if not match:
            raise ValueError(f"local distribution pin is invalid: {item}")
        expected[canonical_name(match.group(1))] = match.group(2)
    if not expected:
        raise ValueError("Python dependency lock contains no exact pins")
    return expected


def installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            raise ValueError("installed distribution lacks a name")
        canonical = canonical_name(name)
        if canonical in installed:
            raise ValueError(f"installed distribution is duplicated: {canonical}")
        installed[canonical] = distribution.version
    return installed


def verify(lock: Path, local: list[str]) -> None:
    expected = expected_distributions(lock, local)
    installed = installed_distributions()
    missing = sorted(set(expected) - set(installed))
    unexpected = sorted(set(installed) - set(expected))
    changed = sorted(
        name
        for name in set(expected) & set(installed)
        if expected[name] != installed[name]
    )
    if missing or unexpected or changed:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if changed:
            details.append(
                "version="
                + ",".join(
                    f"{name}:{installed[name]}!={expected[name]}" for name in changed
                )
            )
        raise ValueError("installed Python environment differs from lock: " + "; ".join(details))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--local", action="append", default=[])
    args = parser.parse_args()
    verify(args.lock, args.local)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
