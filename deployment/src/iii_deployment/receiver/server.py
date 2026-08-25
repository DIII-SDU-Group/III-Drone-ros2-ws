"""Receiver service entry point; refuses unsafe ad-hoc execution."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-deployment-receiver")
    parser.add_argument("--config", required=True)
    parser.add_argument("--foreground", action="store_true")
    parser.parse_args()
    parser.error("receiver service implementation is not yet configured by Ansible")
    return 64

