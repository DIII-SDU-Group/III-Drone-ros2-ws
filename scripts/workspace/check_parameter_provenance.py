#!/usr/bin/env python3
"""Prove that a runtime flies the installed tracked parameter defaults.

Qualification must run on the parameters the repository tracks. A living
configuration can drift from them in two ways: its profile selector points at
a saved snapshot, or reconciliation preserved old local values inside its
`tracked/default.yaml`. Either way a run would qualify parameters that no
commit describes. This check runs on the host that owns the living
configuration (devcontainer for SIM, Pi for HIL), prints a JSON report, and
exits non-zero on any drift.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import yaml

# Runtime profile -> (parameter family, selector scope); mirrors
# iii_drone_configuration.installed_contracts.EXPECTED_PROFILE_MAP.
PROFILE_FAMILY = {"sim": ("sim", "sim"), "hil": ("sim", "hil"),
                  "real": ("real", "real"), "opti_track": ("real", "opti_track")}
TRACKED_SELECTION = "tracked/default.yaml"


def parameters(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    return document["/**"]["ros__parameters"]


def judge(living_root: Path, contract_root: Path, profile: str) -> dict[str, Any]:
    family, scope = PROFILE_FAMILY[profile]
    failures: list[str] = []
    report: dict[str, Any] = {"profile": profile, "parameter_family": family,
                              "selector_scope": scope, "living_root": str(living_root)}
    selector_path = living_root / "profiles" / f"{scope}.yaml"
    try:
        selected = yaml.safe_load(selector_path.read_text())["active_parameter_set"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        return {**report, "accepted": False, "failures": [f"unreadable selector {selector_path}: {exc}"]}
    report["active_parameter_set"] = selected
    if selected != TRACKED_SELECTION:
        failures.append(f"{scope} selects {selected!r}, not {TRACKED_SELECTION!r}")
    try:
        living = parameters(living_root / "parameter_sets" / scope / selected)
        tracked = parameters(contract_root / "tracked_defaults" / family / "default.yaml")
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        return {**report, "accepted": False, "failures": failures + [f"unreadable parameter set: {exc}"]}
    drift = {key: {"living": living.get(key), "tracked": tracked.get(key)}
             for key in sorted(set(living) | set(tracked)) if living.get(key) != tracked.get(key)}
    report["drifted_parameters"] = drift
    if drift:
        failures.append(f"{len(drift)} parameters differ from the installed tracked default: "
                        + ", ".join(list(drift)[:8]))
    report["failures"] = failures
    report["accepted"] = not failures
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", choices=sorted(PROFILE_FAMILY), required=True)
    parser.add_argument("--living-root", type=Path)
    parser.add_argument("--contract-root", type=Path)
    args = parser.parse_args(argv)
    living_root = args.living_root or Path(os.environ.get(
        "III_CONFIGURATION_STATE_ROOT", Path(os.environ["CONFIG_BASE_DIR"]) / "iii_drone"))
    if args.contract_root is not None:
        contract_root = args.contract_root
    else:
        from iii_drone_configuration import resolve_installed_contract_root
        contract_root = resolve_installed_contract_root()
    report = judge(living_root.expanduser(), contract_root, args.profile)
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
