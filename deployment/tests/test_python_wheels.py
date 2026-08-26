from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.target import load_target_definition
from iii_deployment.wheels import (
    direct_requirements, load_wheel_lock, verify_wheel_lock, verify_wheelhouse,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ContractRegistry(ROOT / "deployment/schemas/v1")
REQUIREMENTS = ROOT / "deployment/python/requirements.in"
TARGET = load_target_definition(
    ROOT / "deployment/targets/v1/raspberry-pi-5-noble-arm64.json", REGISTRY
)


def _lock() -> dict:
    return json.loads((ROOT / "deployment/python-wheel-lock.json").read_text())


def test_committed_wheel_lock_binds_requirements_resolver_and_dependency_closure() -> None:
    lock = load_wheel_lock(
        ROOT / "deployment/python-wheel-lock.json", REQUIREMENTS, TARGET, REGISTRY
    )
    assert [requirement.name.lower() for requirement in direct_requirements(REQUIREMENTS)] == [
        "fastapi", "httpx", "pydantic", "pyserial", "pyyaml", "uvicorn", "websockets", "zeroconf"
    ]
    assert len(lock["wheels"]) >= 8
    assert any(wheel["name"] == "pydantic-core" for wheel in lock["wheels"])


@pytest.mark.parametrize("mutation, message", [
    (lambda lock: lock.__setitem__("requirements_sha256", "0" * 64), "requirements.in"),
    (lambda lock: lock["resolver"].__setitem__("platform_digest", "sha256:" + "0" * 64), "contract rejected"),
    (lambda lock: lock["wheels"].pop(), "direct requirement"),
])
def test_wheel_lock_tampering_fails_closed(mutation, message: str) -> None:
    lock = deepcopy(_lock())
    mutation(lock)
    with pytest.raises(ContractError, match=message):
        verify_wheel_lock(lock, REQUIREMENTS, TARGET, REGISTRY)


def test_wheelhouse_requires_exact_filenames_and_hashes(tmp_path: Path) -> None:
    lock = {"wheels": [{"filename": "demo.whl", "sha256": "0" * 64}]}
    (tmp_path / "demo.whl").write_bytes(b"payload")
    with pytest.raises(ContractError, match="hash mismatch"):
        verify_wheelhouse(tmp_path, lock)
    (tmp_path / "extra.txt").write_text("unexpected")
    with pytest.raises(ContractError, match="exactly match"):
        verify_wheelhouse(tmp_path, lock)
