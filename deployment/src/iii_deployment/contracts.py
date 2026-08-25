"""Schema loading, canonical identities, and release classification policy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from jsonschema import Draft7Validator, FormatChecker


class ContractError(ValueError):
    pass


SEMVER = re.compile(r"^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_identity(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class ContractRegistry:
    def __init__(self, schema_root: Path) -> None:
        self.schema_root = schema_root

    def schema(self, name: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ContractError(f"invalid schema name: {name!r}")
        path = self.schema_root / f"{name}.schema.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load schema {name!r}: {exc}") from exc

    def validate(self, name: str, value: Mapping[str, Any]) -> None:
        validator = Draft7Validator(self.schema(name), format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(value), key=lambda error: tuple(str(part) for part in error.path))
        if errors:
            details = "; ".join(
                f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
                for error in errors
            )
            raise ContractError(f"{name} contract rejected: {details}")


def classify_release(manifest: Mapping[str, Any], *, requested: str) -> str:
    """Return effective class, failing closed if qualified authority is invalid."""

    if requested not in {"qualified", "field-development"}:
        raise ContractError(f"unknown requested release class {requested!r}")
    if requested == "field-development":
        return requested
    source = manifest["source"]
    qualification = manifest["qualification"]
    signing = manifest["signing"]
    version = manifest.get("version")
    failures: list[str] = []
    if source["branch"] != "release":
        failures.append("workspace branch is not release")
    if not source["clean"] or source["tracked_patch_sha256"] is not None or source["untracked"]:
        failures.append("source state is not clean")
    if any(module["state"] != "clean" for module in source["submodules"]):
        failures.append("governed submodule state is not clean")
    if not manifest["dependency_lock"]["verified"]:
        failures.append("dependency lock is not verified")
    if not version or not SEMVER.fullmatch(version):
        failures.append("strict SemVer tag is missing")
    if not qualification["tag_on_release"]:
        failures.append("tag reachability from release is unproven")
    if not qualification["tests_complete"] or not qualification["evidence_complete"]:
        failures.append("required tests/evidence are incomplete")
    if not qualification["explicit_action"]:
        failures.append("explicit qualified action is absent")
    if signing["authority"] != "ci-qualified":
        failures.append("signer lacks CI qualification authority")
    if failures:
        raise ContractError("qualified classification refused: " + "; ".join(failures))
    return "qualified"


def check_target_compatibility(manifest: Mapping[str, Any], target: Mapping[str, Any]) -> None:
    expected = manifest["target"]
    mismatches = [
        field for field in ("target_id", "os", "os_version", "architecture", "python_abi", "host_baseline", "ros")
        if target.get(field) != expected.get(field)
    ]
    if mismatches:
        raise ContractError("target incompatibility: " + ", ".join(mismatches))


ALLOWED_STATUS_TRANSITIONS = {
    None: {"qualified"},
    "qualified": {"withdrawn", "unsafe"},
    "withdrawn": {"unsafe"},
    "unsafe": set(),
}


def validate_status_transition(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> None:
    previous_status = previous and previous["status"]
    if current["status"] not in ALLOWED_STATUS_TRANSITIONS[previous_status]:
        raise ContractError(f"non-monotonic status transition {previous_status!r} -> {current['status']!r}")
    if previous is None:
        if current["sequence"] != 1 or current["previous_statement"] is not None:
            raise ContractError("first status statement must have sequence 1 and no predecessor")
        return
    if current["release_id"] != previous["release_id"] or current["version"] != previous["version"]:
        raise ContractError("status transition changed release identity")
    if current["sequence"] != previous["sequence"] + 1:
        raise ContractError("status sequence is not contiguous")
    predecessor = current["previous_statement"] or {}
    if predecessor.get("statement_id") != previous["statement_id"]:
        raise ContractError("status predecessor does not identify previous statement")
    if predecessor.get("sha256") != content_identity(previous):
        raise ContractError("status predecessor checksum mismatch")

