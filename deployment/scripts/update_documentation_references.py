#!/usr/bin/env python3
"""Regenerate reviewed CLI-help and deployment-schema documentation."""

from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))
sys.path.insert(0, str(ROOT / "tools/III-Drone-CLI"))
sys.path.insert(0, str(ROOT / "src/III-Drone-Contracts"))

from iii_deployment.verification.documentation import generated_references, load_policy


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    policy = load_policy(ROOT / "deployment/documentation-policy.json")
    for path, content in generated_references(ROOT, policy).items():
        _atomic(path, content)
