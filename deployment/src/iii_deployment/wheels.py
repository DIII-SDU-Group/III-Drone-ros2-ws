"""Deterministic ARM64 wheel resolution and lock verification."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from .contracts import ContractError, ContractRegistry


TARGET_MARKER_ENVIRONMENT = {
    **default_environment(),
    "implementation_name": "cpython",
    "implementation_version": "3.12.3",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_python_implementation": "CPython",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.12.3",
    "python_version": "3.12",
    "sys_platform": "linux",
    "extra": "",
}


def requirements_identity(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContractError(f"cannot read Python requirements: {exc}") from exc


def direct_requirements(path: Path) -> list[Requirement]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(f"cannot read Python requirements: {exc}") from exc
    values: list[Requirement] = []
    for number, line in enumerate(lines, start=1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            requirement = Requirement(value)
        except ValueError as exc:
            raise ContractError(f"invalid Python requirement at line {number}: {exc}") from exc
        if requirement.url or requirement.marker or requirement.extras:
            raise ContractError(f"direct Python requirement must be a plain exact pin: {value}")
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or specs[0].version.endswith(".*"):
            raise ContractError(f"direct Python requirement is not exactly pinned: {value}")
        values.append(requirement)
    names = [canonicalize_name(value.name) for value in values]
    if not values or names != sorted(set(names)):
        raise ContractError("direct Python requirements must be nonempty, unique, and sorted")
    return values


def inspect_wheel(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
            if len(metadata_names) != 1 or len(wheel_names) != 1:
                raise ContractError(f"wheel has an ambiguous metadata layout: {path.name}")
            metadata = BytesParser(policy=compat32).parsebytes(archive.read(metadata_names[0]))
            wheel_metadata = BytesParser(policy=compat32).parsebytes(archive.read(wheel_names[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ContractError(f"cannot inspect wheel {path.name}: {exc}") from exc
    name = metadata.get("Name")
    version = metadata.get("Version")
    tags = sorted(set(wheel_metadata.get_all("Tag", [])))
    if not name or not version or not tags:
        raise ContractError(f"wheel metadata is incomplete: {path.name}")
    return {
        "name": canonicalize_name(name),
        "version": str(Version(version)),
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tags": tags,
        "requires": sorted(set(metadata.get_all("Requires-Dist", []))),
    }


def create_wheel_lock(
    wheelhouse: Path, requirements: Path, resolver: Mapping[str, Any],
    pip_version: str, imports: list[str],
) -> dict[str, Any]:
    pins = direct_requirements(requirements)
    wheels = sorted(
        (inspect_wheel(path) for path in wheelhouse.glob("*.whl")),
        key=lambda wheel: wheel["name"],
    )
    names = [wheel["name"] for wheel in wheels]
    if not wheels or names != sorted(set(names)):
        raise ContractError("resolved wheels must be nonempty, unique by distribution, and sorted")
    resolved = {wheel["name"]: wheel["version"] for wheel in wheels}
    for pin in pins:
        name = canonicalize_name(pin.name)
        if name not in resolved or Version(resolved[name]) not in pin.specifier:
            raise ContractError(f"resolver did not honor exact direct requirement: {pin}")
    lock = {
        "schema": "iii.python-wheel-lock/v1",
        "python_abi": "cp312",
        "platform": "manylinux_2_39_aarch64",
        "resolver": {
            "reference": resolver["reference"],
            "index_digest": resolver["index_digest"],
            "platform": resolver["platform"],
            "platform_digest": resolver["platform_digest"],
            "pip_version": pip_version,
        },
        "requirements_sha256": requirements_identity(requirements),
        "wheels": wheels,
        "imports": sorted(set(imports)),
    }
    return lock


def verify_wheel_lock(
    lock: Mapping[str, Any], requirements: Path, target: Mapping[str, Any],
    registry: ContractRegistry,
) -> None:
    registry.validate("python-wheel-lock", lock)
    if lock["requirements_sha256"] != requirements_identity(requirements):
        raise ContractError("Python wheel lock does not match requirements.in")
    expected_resolver = target["images"]["wheel_resolver"]
    for field in ("reference", "index_digest", "platform", "platform_digest"):
        if lock["resolver"][field] != expected_resolver[field]:
            raise ContractError(f"Python wheel lock resolver mismatch: {field}")
    wheels = lock["wheels"]
    names = [canonicalize_name(wheel["name"]) for wheel in wheels]
    if names != sorted(set(names)):
        raise ContractError("Python wheel lock distributions must be unique and sorted")
    resolved = {canonicalize_name(wheel["name"]): Version(wheel["version"]) for wheel in wheels}
    for pin in direct_requirements(requirements):
        name = canonicalize_name(pin.name)
        if name not in resolved or resolved[name] not in pin.specifier:
            raise ContractError(f"locked wheel does not satisfy direct requirement: {pin}")
    for wheel in wheels:
        for value in wheel["requires"]:
            try:
                dependency = Requirement(value)
            except ValueError as exc:
                raise ContractError(f"invalid locked wheel dependency {value!r}: {exc}") from exc
            if dependency.marker and not dependency.marker.evaluate(TARGET_MARKER_ENVIRONMENT):
                continue
            name = canonicalize_name(dependency.name)
            if name not in resolved or resolved[name] not in dependency.specifier:
                raise ContractError(
                    f"locked dependency closure is incomplete: {wheel['name']} requires {dependency}"
                )


def load_wheel_lock(
    path: Path, requirements: Path, target: Mapping[str, Any], registry: ContractRegistry,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load Python wheel lock: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("Python wheel lock must be an object")
    verify_wheel_lock(value, requirements, target, registry)
    return value


def verify_wheelhouse(wheelhouse: Path, lock: Mapping[str, Any]) -> None:
    expected = {wheel["filename"]: wheel["sha256"] for wheel in lock["wheels"]}
    actual = {path.name: path for path in wheelhouse.glob("*.whl") if path.is_file()}
    unexpected_entries = [path.name for path in wheelhouse.iterdir() if not path.is_file() or path.suffix != ".whl"]
    if unexpected_entries or set(actual) != set(expected):
        raise ContractError("wheelhouse contents do not exactly match the committed lock")
    for filename, sha256 in expected.items():
        if hashlib.sha256(actual[filename].read_bytes()).hexdigest() != sha256:
            raise ContractError(f"wheel hash mismatch: {filename}")
