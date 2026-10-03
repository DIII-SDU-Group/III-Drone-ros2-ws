"""Offline tests for the parameter provenance check."""

from __future__ import annotations

from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_parameter_provenance as provenance  # noqa: E402


def write_set(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"/**": {"ros__parameters": values}}))


def roots(tmp_path: Path, *, scope: str, selected: str, living: dict, tracked: dict, family: str = "sim"):
    living_root, contract_root = tmp_path / "living", tmp_path / "contract"
    (living_root / "profiles").mkdir(parents=True)
    (living_root / "profiles" / f"{scope}.yaml").write_text(
        yaml.safe_dump({"version": 1, "active_parameter_set": selected}))
    write_set(living_root / "parameter_sets" / scope / selected, living)
    write_set(contract_root / "tracked_defaults" / family / "default.yaml", tracked)
    return living_root, contract_root


DEFAULTS = {"/control/maneuver_controller/cable_takeoff_use_mpc": False, "/mission/timeout_ms": 500}


def test_tracked_selection_matching_the_installed_default_is_accepted(tmp_path: Path) -> None:
    living, contract = roots(tmp_path, scope="hil", selected="tracked/default.yaml",
                             living=dict(DEFAULTS), tracked=dict(DEFAULTS))
    report = provenance.judge(living, contract, "hil")
    assert report["accepted"], report["failures"]
    assert report["parameter_family"] == "sim" and report["drifted_parameters"] == {}


def test_selected_snapshot_is_rejected(tmp_path: Path) -> None:
    living, contract = roots(tmp_path, scope="sim", selected="snapshots/runtime_parameters_1.yaml",
                             living=dict(DEFAULTS), tracked=dict(DEFAULTS))
    report = provenance.judge(living, contract, "sim")
    assert not report["accepted"]
    assert "not 'tracked/default.yaml'" in report["failures"][0]


def test_preserved_local_values_in_tracked_default_are_rejected(tmp_path: Path) -> None:
    drifted = {**DEFAULTS, "/control/maneuver_controller/cable_takeoff_use_mpc": True, "/extra": 1}
    living, contract = roots(tmp_path, scope="sim", selected="tracked/default.yaml",
                             living=drifted, tracked=dict(DEFAULTS))
    report = provenance.judge(living, contract, "sim")
    assert not report["accepted"]
    assert report["drifted_parameters"]["/control/maneuver_controller/cable_takeoff_use_mpc"] == {
        "living": True, "tracked": False}
    assert report["drifted_parameters"]["/extra"] == {"living": 1, "tracked": None}


def test_missing_selector_is_rejected(tmp_path: Path) -> None:
    report = provenance.judge(tmp_path / "none", tmp_path / "contract", "real")
    assert not report["accepted"] and "unreadable selector" in report["failures"][0]
