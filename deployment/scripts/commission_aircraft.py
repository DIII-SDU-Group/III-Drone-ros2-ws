#!/usr/bin/env python3
"""Seal explicit physical commissioning matrix results and their artifacts."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deployment/src"))

from iii_deployment.verification.record import main


if __name__ == "__main__":
    raise SystemExit(
        main(["--root", str(ROOT), *sys.argv[1:]], expected_level="physical")
    )
