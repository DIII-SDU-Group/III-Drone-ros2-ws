from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_hardware_observations_are_non_authoritative_and_exact() -> None:
    value = json.loads((ROOT / "deployment/hardware/legacy-observations.json").read_text(encoding="utf-8"))
    assert value["source"]["commit"] == "4ab4ae76013ba3ff904189777e7c97af107d94e1"
    assert value["source"]["branch"] == "v2.2-staging"
    assert value["authority"] == "historical-observation-only"
    assert {item["serial"] for item in value["observations"]} == {"00DEEC69", None}


def test_retirement_map_rejects_legacy_runtime_ownership() -> None:
    text = (ROOT / "docs/legacy-deployment-retirement.md").read_text(encoding="utf-8")
    assert "no migration; `III-Drone-Supervision/system_spec.py` remains authoritative" in text
    assert "signed Q131 matrix" in text
    assert "History must never be deleted or rewritten" in text

