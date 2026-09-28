#!/usr/bin/env python3
"""Select a reachable Pi IPv4 for the workstation HIL transport."""

import hashlib
import ipaddress
import os
from pathlib import Path
import socket
import sys


def _ipv4(value: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        return None


def _recorded_address(path: Path, key: str) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return _ipv4(line.split("=", 1)[1])
    except OSError:
        pass
    return None


def resolve(host: str, state_dir: Path, instance: str = "0") -> tuple[str, str] | None:
    candidates: list[str] = []
    # An owner record belongs to the canonical HIL target only. An explicit
    # alternate hostname must not silently inherit that session's address.
    if host == "iii.local" and instance.isdecimal():
        owner = state_dir / f".iii-hil-owner-{instance}.env"
        candidates.append(_recorded_address(owner, "pi_address") or "")
    cache = state_dir / (".iii-hil-peer-" + hashlib.sha256(host.encode()).hexdigest()[:16])
    candidates.append(_recorded_address(cache, "address") or "")
    try:
        candidates.extend(
            item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        )
    except socket.gaierror:
        pass
    for peer in dict.fromkeys(filter(None, candidates)):
        if not _ipv4(peer):
            continue
        try:
            with socket.create_connection((peer, 22), timeout=0.75):
                pass
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((peer, 9))
                source = route.getsockname()[0]
            if not source or source == "0.0.0.0":
                continue
        except OSError:
            continue
        # This is only a routing hint. Every use probes it again; the HIL
        # coordinator still verifies the remote runtime identity over SSH.
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_name(cache.name + f".{os.getpid()}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(f"address={peer}\n")
            os.replace(temporary, cache)
        except OSError:
            pass
        return peer, source
    return None


if __name__ == "__main__":
    answer = resolve(sys.argv[1], Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "0")
    if answer:
        print(*answer)
