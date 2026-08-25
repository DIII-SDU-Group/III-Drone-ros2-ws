"""Immutable operational policy loading and anti-weakening checks."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
import json

from .contracts import ContractError, ContractRegistry, content_identity


def load_operational_policy(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid operational policy: {exc}") from exc
    registry.validate("operational-policy", policy)
    return policy


def policy_reference(policy: Mapping[str, Any]) -> dict[str, str]:
    return {"schema_version": str(policy["schema_version"]), "sha256": content_identity(policy)}


def merge_stricter_policy(
    baseline: Mapping[str, Any], overrides: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    """Apply overrides, relying on floor/ceiling schema constraints to reject weakening."""

    merged = deepcopy(dict(baseline))
    for group, values in overrides.items():
        if group == "schema_version" or group not in merged or not isinstance(values, Mapping):
            raise ContractError(f"unsupported policy override group {group!r}")
        unknown = set(values) - set(merged[group])
        if unknown:
            raise ContractError(f"unsupported policy override values in {group}: {sorted(unknown)}")
        merged[group].update(values)
    registry.validate("operational-policy", merged)
    return merged

