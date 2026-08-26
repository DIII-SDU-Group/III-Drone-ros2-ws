"""Canonical target-definition identity and pre-transfer ABI compatibility."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from .contracts import ContractError, ContractRegistry, check_target_compatibility, content_identity


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load target definition: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("target definition must be a JSON object")
    return value


def _without(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def load_target_definition(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    value = _load_json(path)
    registry.validate("target-definition", value)
    baseline = value["host_baseline"]
    if content_identity(_without(baseline, "contract_id")) != baseline["contract_id"]:
        raise ContractError("target host-baseline content identity mismatch")
    if content_identity(_without(value, "definition_id")) != value["definition_id"]:
        raise ContractError("target-definition content identity mismatch")
    if value["sysroot"]["aircraft_derived"] is not False:
        raise ContractError("aircraft-derived sysroots are forbidden")
    if value["sysroot"]["seed_sha256"] != value["images"]["target_seed"]["platform_digest"].removeprefix("sha256:"):
        raise ContractError("sysroot seed does not match the pinned target platform image")
    if content_identity(_without(value["sysroot"], "content_id")) != value["sysroot"]["content_id"]:
        raise ContractError("target sysroot content identity mismatch")
    package_names = [package["name"] for package in value["sysroot"]["packages"]]
    if package_names != sorted(set(package_names)):
        raise ContractError("target sysroot packages must be unique and sorted")
    return value


def manifest_target(definition: Mapping[str, Any]) -> dict[str, Any]:
    target = definition["target"]
    return {
        "definition_id": definition["definition_id"],
        "target_id": target["target_id"],
        "os": target["os"],
        "os_version": target["os_version"],
        "architecture": target["architecture"],
        "python_abi": target["python"]["abi"],
        "host_baseline": definition["host_baseline"]["contract_id"],
        "ros": target["ros"]["distro"],
    }


def manifest_toolchain(definition: Mapping[str, Any]) -> dict[str, str]:
    return {
        "builder_digest": definition["images"]["builder"]["platform_digest"],
        "compiler": f"aarch64-linux-gnu-g++ {definition['toolchain']['compiler_version']}",
        "sysroot_sha256": definition["sysroot"]["content_id"],
    }


def _version(value: str, field: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
        raise ContractError(f"target probe {field} has an invalid numeric version")
    return tuple(int(part) for part in value.split("."))


def verify_target_probe(
    definition: Mapping[str, Any],
    probe: Mapping[str, Any],
    registry: ContractRegistry,
) -> None:
    registry.validate("target-abi-probe", probe)
    target = definition["target"]
    expected = {
        "target_id": target["target_id"],
        "source_image_digest": definition["images"]["target_seed"]["platform_digest"],
        "os": target["os"],
        "os_version": target["os_version"],
        "os_codename": target["os_codename"],
        "architecture": target["architecture"],
        "dpkg_architecture": target["dpkg_architecture"],
        "endianness": target["endianness"],
        "pointer_bits": target["pointer_bits"],
        "ros": target["ros"]["distro"],
        "python_abi": target["python"]["abi"],
        "python_soabi": target["python"]["soabi"],
        "libc_name": target["libc"]["name"],
        "compiler_id": "gcc",
        "compiler_target": definition["toolchain"]["target_triple"],
    }
    mismatches = [field for field, value in expected.items() if probe.get(field) != value]
    python_version = str(probe["python_version"])
    if not python_version.startswith(target["python"]["major_minor"] + "."):
        mismatches.append("python_version")
    libc = _version(str(probe["libc_version"]), "libc_version")
    if not (_version(target["libc"]["minimum"], "libc minimum") <= libc < _version(target["libc"]["maximum_exclusive"], "libc maximum")):
        mismatches.append("libc_version")
    compiler = _version(str(probe["compiler_version"]), "compiler_version")
    expected_compiler = _version(definition["toolchain"]["compiler_version"], "compiler version")
    if compiler < expected_compiler or compiler[0] != expected_compiler[0]:
        mismatches.append("compiler_version")
    if mismatches:
        raise ContractError("target ABI incompatibility: " + ", ".join(sorted(set(mismatches))))


def verify_release_target(
    manifest: Mapping[str, Any],
    definition: Mapping[str, Any],
    probe: Mapping[str, Any],
    registry: ContractRegistry,
) -> None:
    """Fail closed before transfer and again before activation."""

    expected_target = manifest_target(definition)
    check_target_compatibility(manifest, expected_target)
    if manifest["target"] != expected_target:
        raise ContractError("release manifest target metadata is not canonical")
    expected_toolchain = manifest_toolchain(definition)
    mismatches = [field for field, value in expected_toolchain.items() if manifest["toolchain"].get(field) != value]
    if mismatches:
        raise ContractError("release toolchain incompatibility: " + ", ".join(mismatches))
    verify_target_probe(definition, probe, registry)


def target_reference(definition: Mapping[str, Any]) -> dict[str, str]:
    return {
        "target_id": definition["target"]["target_id"],
        "definition_id": definition["definition_id"],
        "host_baseline": definition["host_baseline"]["contract_id"],
    }
