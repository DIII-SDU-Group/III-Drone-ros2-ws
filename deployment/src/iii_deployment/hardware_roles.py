"""Shared hardware-role contracts, deterministic udev rules, and inspection."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping

from iii_deployment.contracts import (
    ContractError,
    ContractRegistry,
    canonical_json,
    content_identity,
)

MANIFEST_SCHEMA = "iii.hardware-role-manifest/v1"
INSPECTION_SCHEMA = "iii.hardware-inspection/v1"
SAFE_PROPERTIES = frozenset(
    {
        "DEVNAME",
        "DEVPATH",
        "ID_BUS",
        "ID_MODEL",
        "ID_MODEL_FROM_DATABASE",
        "ID_PATH",
        "ID_SERIAL_SHORT",
        "ID_USB_DRIVER",
        "ID_USB_INTERFACE_NUM",
        "ID_VENDOR_ID",
        "ID_MODEL_ID",
        "ID_V4L_CAPABILITIES",
    }
)


def _read_canonical(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} is missing or linked")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise ContractError(f"{label} is not canonical JSON")
    return value


def validate_manifest(
    manifest: Mapping[str, Any], registry: ContractRegistry
) -> dict[str, Any]:
    value = dict(manifest)
    registry.validate("hardware-role-manifest", value)
    if value["manifest_id"] != content_identity(
        {key: item for key, item in value.items() if key != "manifest_id"}
    ):
        raise ContractError("hardware-role manifest identity mismatch")
    roles = value["roles"]
    names = [item["role"] for item in roles]
    paths = [item["stable_path"] for item in roles]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ContractError("hardware roles must be uniquely sorted")
    if len(paths) != len(set(paths)):
        raise ContractError("hardware stable paths must be unique")
    requirements = value["requirements"]
    if requirements["required"] != sorted(
        role["role"] for role in roles if role["requirement"] == "required"
    ) or requirements["optional"] != sorted(
        role["role"] for role in roles if role["requirement"] == "optional"
    ):
        raise ContractError(
            "hardware requirement indexes differ from role declarations"
        )
    for role in roles:
        matcher = role["match"]
        allowlist = matcher["serial_allowlist"]
        if allowlist and not any(
            evidence.startswith("commissioning-record:")
            for evidence in role["matching_evidence"]
        ):
            raise ContractError(
                f"hardware role {role['role']} has an uncommissioned serial allowlist"
            )
        if matcher["subsystem"] == "tty" and not (
            matcher["vendor_id"] and matcher["product_id"]
        ):
            raise ContractError(f"tty role {role['role']} lacks USB vendor/product")
        if matcher["subsystem"] == "video4linux" and (
            matcher["v4l_index"] is None
            or matcher["properties"].get("ID_BUS") != "usb"
            or "ID_V4L_CAPABILITIES" not in matcher["properties"]
        ):
            raise ContractError(
                f"camera role {role['role']} lacks USB capture-node evidence"
            )
    retirement = value["legacy_rule_retirement"]
    if (retirement["state"] == "retired") != (
        retirement["evidence_record"] is not None
    ):
        raise ContractError("legacy-rule retirement is not evidence-bound")
    return value


def load_manifest(path: Path, registry: ContractRegistry) -> dict[str, Any]:
    return validate_manifest(
        _read_canonical(path, label="hardware-role manifest"), registry
    )


def _udev_clause(key: str, value: str) -> str:
    if any(character in value for character in {'"', "\n", "\r"}):
        raise ContractError("hardware matcher contains unsafe udev text")
    return f'{key}=="{value}"'


def generate_udev_rules(manifest: Mapping[str, Any]) -> bytes:
    """Render one stable, reviewable rules file without observations or learning."""

    lines = [
        "# Generated from iii.hardware-role-manifest/v1; DO NOT EDIT.",
        f"# manifest_id={manifest['manifest_id']}",
    ]
    for role in manifest["roles"]:
        matcher = role["match"]
        clauses = [_udev_clause("SUBSYSTEM", matcher["subsystem"])]
        if matcher["vendor_id"]:
            clauses.append(_udev_clause("ATTRS{idVendor}", matcher["vendor_id"]))
        if matcher["product_id"]:
            clauses.append(_udev_clause("ATTRS{idProduct}", matcher["product_id"]))
        if matcher["interface_number"]:
            clauses.append(
                _udev_clause("ENV{ID_USB_INTERFACE_NUM}", matcher["interface_number"])
            )
        serials = matcher["serial_allowlist"]
        if len(serials) > 1:
            raise ContractError(
                "multiple serial alternatives require separate deterministic roles"
            )
        if serials:
            clauses.append(_udev_clause("ATTRS{serial}", serials[0]))
        if matcher["v4l_index"] is not None:
            clauses.append(_udev_clause("ATTR{index}", matcher["v4l_index"]))
        for key, value in sorted(matcher["properties"].items()):
            clauses.append(_udev_clause(f"ENV{{{key}}}", value))
        link = role["stable_path"].removeprefix("/dev/")
        clauses.extend(
            [
                f'SYMLINK+="{link}"',
                f'GROUP="{role["group"]}"',
                f'MODE="{role["mode"]}"',
                'TAG+="systemd"',
            ]
        )
        lines.append(", ".join(clauses))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _property_map(raw: bytes) -> dict[str, str]:
    value: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        key, separator, item = line.partition("=")
        if separator and key in SAFE_PROPERTIES and "\x00" not in item:
            value[key] = item[:512]
    return value


def _attribute(path: Path, name: str) -> str | None:
    target = path / name
    try:
        if target.is_symlink() or not target.is_file():
            return None
        return target.read_text(encoding="utf-8").strip()[:256] or None
    except (OSError, UnicodeDecodeError):
        return None


def enumerate_devices(
    *,
    sys_root: Path = Path("/sys"),
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[dict[str, Any]]:
    """Collect only USB-role evidence; never return general host/environment data."""

    devices: list[dict[str, Any]] = []
    for subsystem, class_name, pattern in (
        ("tty", "tty", "tty*"),
        ("video4linux", "video4linux", "video*"),
    ):
        class_root = sys_root / "class" / class_name
        for entry in sorted(class_root.glob(pattern), key=lambda item: item.name):
            if entry.is_symlink():
                try:
                    resolved = entry.resolve(strict=True)
                except OSError:
                    continue
            else:
                resolved = entry
            devname = f"/dev/{entry.name}"
            try:
                completed = runner(
                    [
                        "/usr/bin/udevadm",
                        "info",
                        "--query=property",
                        "--path",
                        str(entry),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=2,
                )
                properties = _property_map(completed.stdout)
            except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
                continue
            # Pseudo terminals and platform UARTs are irrelevant to this USB contract.
            if properties.get("ID_BUS") != "usb":
                continue
            device = {
                "subsystem": subsystem,
                "device_node": properties.get("DEVNAME", devname),
                "sysfs_path": str(resolved),
                "id_bus": properties["ID_BUS"],
                "vendor_id": properties.get("ID_VENDOR_ID"),
                "product_id": properties.get("ID_MODEL_ID"),
                "serial": properties.get("ID_SERIAL_SHORT"),
                "interface_number": properties.get("ID_USB_INTERFACE_NUM"),
                "id_path": properties.get("ID_PATH"),
                "driver": properties.get("ID_USB_DRIVER"),
                "v4l_index": _attribute(resolved, "index"),
                "v4l_capabilities": properties.get("ID_V4L_CAPABILITIES"),
                "product_name": properties.get("ID_MODEL_FROM_DATABASE")
                or properties.get("ID_MODEL"),
            }
            if not device["device_node"].startswith("/dev/"):
                continue
            device["device_id"] = content_identity(device)
            devices.append(device)
    return sorted(devices, key=lambda item: item["device_id"])


def _matches(role: Mapping[str, Any], device: Mapping[str, Any]) -> bool:
    matcher = role["match"]
    if device["subsystem"] != matcher["subsystem"]:
        return False
    fields = {
        "vendor_id": "vendor_id",
        "product_id": "product_id",
        "interface_number": "interface_number",
        "v4l_index": "v4l_index",
    }
    if any(
        matcher[key] is not None and device[field] != matcher[key]
        for key, field in fields.items()
    ):
        return False
    if (
        matcher["serial_allowlist"]
        and device["serial"] not in matcher["serial_allowlist"]
    ):
        return False
    properties = {
        "ID_BUS": device.get("id_bus"),
        "ID_V4L_CAPABILITIES": device.get("v4l_capabilities"),
    }
    return all(
        properties.get(key) is not None
        and fnmatch.fnmatchcase(str(properties[key]), pattern)
        for key, pattern in matcher["properties"].items()
    )


def inspect_hardware(
    manifest: Mapping[str, Any],
    devices: Iterable[Mapping[str, Any]],
    *,
    profile: str,
    boot_id: str,
    captured_monotonic_ns: int,
    stable_path_target: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    if profile not in {"real", "opti_track", "hil"}:
        raise ContractError("hardware inspection requires an aircraft profile")
    sanitized = [dict(item) for item in devices]
    expected_fields = {
        "device_id",
        "subsystem",
        "device_node",
        "sysfs_path",
        "id_bus",
        "vendor_id",
        "product_id",
        "serial",
        "interface_number",
        "id_path",
        "driver",
        "v4l_index",
        "v4l_capabilities",
        "product_name",
    }
    if any(set(item) != expected_fields for item in sanitized):
        raise ContractError("hardware observation contains non-contract fields")
    if any(
        item["device_id"]
        != content_identity(
            {key: value for key, value in item.items() if key != "device_id"}
        )
        for item in sanitized
    ):
        raise ContractError("hardware device identity mismatch")
    sanitized.sort(key=lambda item: item["device_id"])
    resolved: dict[str, Any] = {}
    matched_ids: set[str] = set()
    for role in manifest["roles"]:
        matches = [item for item in sanitized if _matches(role, item)]
        ids = [item["device_id"] for item in matches]
        matched_ids.update(ids)
        state = (
            "present"
            if len(matches) == 1
            else "missing" if not matches else "ambiguous"
        )
        target = (
            stable_path_target(role["stable_path"])
            if stable_path_target
            else (matches[0]["device_node"] if len(matches) == 1 else None)
        )
        stable_ok = len(matches) == 1 and target == matches[0]["device_node"]
        requirement = "optional" if profile == "hil" else role["requirement"]
        resolved[role["role"]] = {
            "requirement": requirement,
            "state": state,
            "unambiguous": len(matches) == 1,
            "stable_path": role["stable_path"],
            "stable_path_ok": stable_ok,
            "matched_device_ids": ids,
        }
    accepted = all(
        item["state"] == "present" and item["stable_path_ok"]
        for item in resolved.values()
        if item["requirement"] == "required"
    )
    value: dict[str, Any] = {
        "schema": INSPECTION_SCHEMA,
        "inspection_id": "0" * 64,
        "manifest_id": manifest["manifest_id"],
        "profile": profile,
        "boot_id": boot_id,
        "captured_monotonic_ns": captured_monotonic_ns,
        "accepted": accepted,
        "roles": resolved,
        "devices": sanitized,
        "unmatched_device_ids": sorted(
            item["device_id"]
            for item in sanitized
            if item["device_id"] not in matched_ids
        ),
        "automatic_learning": False,
    }
    value["inspection_id"] = content_identity(
        {key: item for key, item in value.items() if key != "inspection_id"}
    )
    return value


class HardwareInspector:
    """Root-owned inspector used by receiver observation and activation health."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        registry: ContractRegistry,
        profile: str,
        enumerate_provider: Callable[[], list[dict[str, Any]]] = enumerate_devices,
        boot_id: Callable[[], str] = lambda: Path("/proc/sys/kernel/random/boot_id")
        .read_text(encoding="ascii")
        .strip(),
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        readlink: Callable[[str], str | None] | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.registry = registry
        self.profile = profile
        self.enumerate_provider = enumerate_provider
        self.boot_id = boot_id
        self.monotonic_ns = monotonic_ns
        self.readlink = readlink or self._readlink

    @staticmethod
    def _readlink(path: str) -> str | None:
        try:
            return os.path.realpath(path) if Path(path).is_symlink() else None
        except OSError:
            return None

    def inspect(self) -> dict[str, Any]:
        manifest = load_manifest(self.manifest_path, self.registry)
        report = inspect_hardware(
            manifest,
            self.enumerate_provider(),
            profile=self.profile,
            boot_id=self.boot_id(),
            captured_monotonic_ns=self.monotonic_ns(),
            stable_path_target=self.readlink,
        )
        self.registry.validate("hardware-inspection", report)
        return report

    def health_roles(self) -> Mapping[str, Mapping[str, Any]]:
        report = self.inspect()
        return {
            role: {
                "state": (
                    evidence["state"] if evidence["stable_path_ok"] else "missing"
                ),
                "unambiguous": evidence["unambiguous"] and evidence["stable_path_ok"],
            }
            for role, evidence in report["roles"].items()
        }


def validate_commissioning_sequence(
    manifest: Mapping[str, Any],
    reports: Mapping[str, Mapping[str, Any]],
    *,
    functional_evidence: Mapping[str, str],
) -> dict[str, Any]:
    """Fail closed over physical evidence; this function never changes policy."""

    phases = manifest["legacy_rule_retirement"]["required_phases"]
    if sorted(reports) != sorted(phases):
        raise ContractError("hardware commissioning phase set is incomplete")
    boot_ids: set[str] = set()
    inspection_ids: set[str] = set()
    for phase in phases:
        report = reports[phase]
        if (
            report.get("schema") != INSPECTION_SCHEMA
            or report.get("manifest_id") != manifest["manifest_id"]
            or report.get("accepted") is not True
            or report.get("automatic_learning") is not False
        ):
            raise ContractError(f"hardware commissioning phase {phase} was rejected")
        expected_id = content_identity(
            {key: item for key, item in report.items() if key != "inspection_id"}
        )
        if report["inspection_id"] != expected_id:
            raise ContractError(
                f"hardware commissioning phase {phase} identity mismatch"
            )
        boot_ids.add(str(report["boot_id"]))
        inspection_ids.add(str(report["inspection_id"]))
    if len(boot_ids) < 2:
        raise ContractError("hardware commissioning lacks reboot evidence")
    if len(inspection_ids) != len(phases):
        raise ContractError("hardware commissioning reuses a phase observation")
    required_roles = manifest["requirements"]["required"]
    if sorted(functional_evidence) != required_roles or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in functional_evidence.values()
    ):
        raise ContractError("hardware commissioning functional evidence is incomplete")
    value: dict[str, Any] = {
        "schema": "iii.hardware-commissioning-evaluation/v1",
        "evaluation_id": "0" * 64,
        "manifest_id": manifest["manifest_id"],
        "accepted": True,
        "phase_inspection_ids": {
            phase: reports[phase]["inspection_id"] for phase in phases
        },
        "functional_evidence": {
            role: functional_evidence[role] for role in required_roles
        },
        "automatic_learning": False,
    }
    value["evaluation_id"] = content_identity(
        {key: item for key, item in value.items() if key != "evaluation_id"}
    )
    return value
