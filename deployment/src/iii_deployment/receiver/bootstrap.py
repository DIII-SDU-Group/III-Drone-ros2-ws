"""Stable Ansible-owned receiver recovery bootstrap entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import time

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.receiver.config import (
    READINESS_PATH,
    SOCKET_PATH,
    RECEIVER_UPDATE_TRUST_PATH,
    assert_production_root,
)
from iii_deployment.receiver.update import (
    ReceiverRecoveryBootstrap,
    ReceiverSlotStore,
    _read_canonical,
)

BOOTSTRAP_SCHEMA_ROOT = Path(
    "/opt/iii/receiver/bootstrap/share/iii-deployment/schemas/v1"
)
CANDIDATE_UNIT = "iii-deployment-receiver-candidate.service"


def _restart_candidate() -> None:
    try:
        READINESS_PATH.unlink(missing_ok=True)
        subprocess.run(
            ["/usr/bin/systemctl", "restart", CANDIDATE_UNIT],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError(
            f"stable bootstrap could not start receiver candidate: {exc}"
        ) from exc


def _readiness() -> dict:
    try:
        value = _read_canonical(READINESS_PATH, label="receiver readiness")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(0.25)
        try:
            connection.connect(str(SOCKET_PATH))
        finally:
            connection.close()
        return value
    except (ContractError, OSError):
        return {}


def _stop_candidate() -> None:
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "stop", CANDIDATE_UNIT],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError(
            f"stable bootstrap could not stop receiver candidate: {exc}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-receiver-bootstrap")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    mode.add_argument("--prepare", action="store_true")
    arguments = parser.parse_args()
    try:
        assert_production_root()
        registry = ContractRegistry(BOOTSTRAP_SCHEMA_ROOT)
        slots = ReceiverSlotStore(
            Path("/"),
            trust=RECEIVER_UPDATE_TRUST_PATH,
            registry=registry,
        )
        bootstrap = ReceiverRecoveryBootstrap(
            slots,
            monotonic=time.monotonic,
            boot_id=lambda: Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="ascii")
            .strip(),
            restart_receiver=lambda: None,
            readiness_probe=_readiness,
            wait_tick=lambda: time.sleep(0.25),
        )
        if arguments.prepare:
            inactive = bootstrap.prepare_staging()
            print(
                json.dumps(
                    {
                        "schema": "iii.receiver-update-prepare/v1",
                        "inactive_slot": inactive,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 0

        bootstrap.restart_receiver = _restart_candidate
        try:
            result = bootstrap.apply() if arguments.apply else bootstrap.reconcile()
        finally:
            _stop_candidate()
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if result["stage"] in {"committed", "staged", "reverted"} else 1
    except ContractError as exc:
        parser.error(str(exc))
    return 64
