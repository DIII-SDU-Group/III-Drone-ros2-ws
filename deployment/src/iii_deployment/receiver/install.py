"""Root-only initial receiver installer used by Ansible convergence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.receiver.config import RECEIVER_UPDATE_TRUST_PATH
from iii_deployment.receiver.update import ReceiverSlotStore


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-receiver-initial-install")
    parser.add_argument("--bundle", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ContractError("initial receiver installation requires root")
        result = ReceiverSlotStore(
            Path("/"),
            trust=RECEIVER_UPDATE_TRUST_PATH,
            registry=ContractRegistry(
                Path("/opt/iii/receiver/bootstrap/share/iii-deployment/schemas/v1")
            ),
        ).install_initial(arguments.bundle)
    except (ContractError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
