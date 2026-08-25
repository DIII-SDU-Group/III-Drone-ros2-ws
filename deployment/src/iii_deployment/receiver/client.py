"""Unprivileged fixed receiver client placeholder; no shell command transport."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-deploymentctl")
    parser.add_argument("action", choices=("status",))
    parser.parse_args()
    parser.error("receiver socket is not configured; run `iii host inspect`")
    return 64

