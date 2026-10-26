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


def resolve(
    host: str, state_dir: Path, instance: str = "0",
    diagnostics: list[str] | None = None,
) -> tuple[str, str] | None:
    """Return ``(peer, workstation_source)`` or ``None``.

    When ``diagnostics`` is given, it receives one reason per rejected
    candidate (and for a failed name lookup) so callers can explain a failure.
    """
    notes = diagnostics if diagnostics is not None else []
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
    except socket.gaierror as exc:
        notes.append(f"name resolution for {host} failed: {exc}")
    unique = list(dict.fromkeys(filter(None, candidates)))
    if not unique:
        notes.append(f"no IPv4 candidates for {host} (recorded peers or DNS/mDNS)")
    for peer in unique:
        if not _ipv4(peer):
            notes.append(f"{peer}: not an IPv4 address")
            continue
        try:
            with socket.create_connection((peer, 22), timeout=0.75):
                pass
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((peer, 9))
                source = route.getsockname()[0]
            if not source or source == "0.0.0.0":
                notes.append(f"{peer}: no workstation source route")
                continue
        except OSError as exc:
            notes.append(f"{peer}: SSH port 22 unreachable ({exc})")
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


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: resolve_hil_peer.py HOST STATE_DIR [INSTANCE]", file=sys.stderr)
        return 2
    diagnostics: list[str] = []
    answer = resolve(argv[1], Path(argv[2]), argv[3] if len(argv) > 3 else "0", diagnostics)
    if answer:
        print(*answer)
        return 0
    # Keep stdout empty (callers parse it); explain the failure on stderr.
    print(
        f"HIL peer resolution for {argv[1]} found no reachable IPv4: "
        + ("; ".join(diagnostics) or "no candidates"),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
