#!/usr/bin/env python3
"""Emit the target-equivalent runtime and compiled ABI as one JSON record."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def _compiled_probe() -> dict[str, str]:
    process = subprocess.run(
        ["/usr/local/bin/iii-target-abi-probe"],
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in process.stdout.splitlines() if "=" in line)


def main() -> int:
    compiled = _compiled_probe()
    release = _os_release()
    python_version = platform.python_version()
    value = {
        "schema": "iii.target-abi-probe/v1",
        "target_id": os.environ["III_TARGET_ID"],
        "source_image_digest": os.environ["III_TARGET_IMAGE_PLATFORM_DIGEST"],
        "os": release["ID"],
        "os_version": release["VERSION_ID"],
        "os_codename": release["VERSION_CODENAME"],
        "architecture": platform.machine(),
        "dpkg_architecture": subprocess.run(
            ["dpkg", "--print-architecture"], check=True, capture_output=True, text=True
        ).stdout.strip(),
        "endianness": compiled["endianness"],
        "pointer_bits": int(compiled["pointer_bits"]),
        "ros": os.environ.get("ROS_DISTRO"),
        "python_version": python_version,
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "python_soabi": str(sysconfig.get_config_var("SOABI")),
        "libc_name": compiled["libc_name"],
        "libc_version": compiled["libc_version"],
        "compiler_id": compiled["compiler_id"],
        "compiler_version": compiled["compiler_version"],
        "compiler_target": compiled["compiler_target"],
    }
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
