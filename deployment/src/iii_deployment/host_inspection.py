"""Receiver-owned composition of independently validated host evidence."""

from __future__ import annotations

from typing import Any

from iii_deployment.contracts import ContractError, ContractRegistry, content_identity


class HostInspector:
    def __init__(
        self,
        *,
        logical_target: str,
        profile: str,
        hardware_inspector: Any,
        boot_inspector: Any,
        registry: ContractRegistry,
    ) -> None:
        if logical_target != "drone" or profile not in {"real", "opti_track"}:
            raise ContractError("host inspector target binding is invalid")
        self.logical_target = logical_target
        self.profile = profile
        self.hardware_inspector = hardware_inspector
        self.boot_inspector = boot_inspector
        self.registry = registry

    def inspect(self) -> dict[str, Any]:
        hardware = self.hardware_inspector.inspect()
        boot = self.boot_inspector.inspect()
        self.registry.validate("hardware-inspection", hardware)
        self.registry.validate("boot-inspection", boot)
        if hardware["profile"] != self.profile:
            raise ContractError("hardware inspection profile differs from host target")
        if hardware["boot_id"] != boot["boot_id"]:
            raise ContractError("host inspection crossed a boot boundary")
        value: dict[str, Any] = {
            "schema": "iii.host-inspection/v1",
            "inspection_id": "0" * 64,
            "logical_target": self.logical_target,
            "profile": self.profile,
            "boot_id": boot["boot_id"],
            "accepted": hardware["accepted"] is True and boot["accepted"] is True,
            "hardware": hardware,
            "boot": boot,
        }
        value["inspection_id"] = content_identity(
            {key: item for key, item in value.items() if key != "inspection_id"}
        )
        self.registry.validate("host-inspection", value)
        return value
