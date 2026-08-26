"""Bounded root-owned runtime log retention maintenance entry point."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from .contracts import ContractError
from .log_lifecycle import LogPolicy, SessionLogStore


def plan_retention(
    *,
    log_root: Path,
    operational_policy: Path,
) -> tuple[SessionLogStore, dict]:
    if operational_policy.is_symlink() or not operational_policy.is_file():
        raise ContractError("operational policy is missing or linked")
    try:
        policy_value = json.loads(operational_policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read operational policy: {exc}") from exc
    policy = LogPolicy.from_operational_policy(policy_value)
    storage = policy_value["storage"]
    usage = shutil.disk_usage(log_root)
    reserve = max(
        int(storage["minimum_reserve_bytes"]),
        int(usage.total * float(storage["minimum_reserve_percent"]) / 100),
    )
    store = SessionLogStore(log_root, policy)
    plan = store.retention_plan(
        now=datetime.now(timezone.utc),
        filesystem_total_bytes=usage.total,
        filesystem_free_bytes=usage.free,
        deployment_reserve_bytes=reserve,
    )
    return store, plan


def main() -> int:
    parser = argparse.ArgumentParser(prog="iii-log-maintenance")
    parser.add_argument("--log-root", type=Path, default=Path("/var/log/iii"))
    parser.add_argument(
        "--operational-policy",
        type=Path,
        default=Path("/etc/iii/operational-policy.json"),
    )
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        store, plan = plan_retention(
            log_root=arguments.log_root,
            operational_policy=arguments.operational_policy,
        )
        removed = store.apply_retention(plan) if arguments.apply else []
    except (ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {"plan": plan, "applied": arguments.apply, "removed": removed},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
