from __future__ import annotations

from pathlib import Path

import pytest

from iii_deployment.boot_baseline import inspect_boot, load_boot_profile
from iii_deployment.contracts import ContractError, ContractRegistry
from iii_deployment.hardware_roles import inspect_hardware, load_manifest
from iii_deployment.host_inspection import HostInspector

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ContractRegistry(ROOT / "schemas/v1")


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o644)


def _boot(tmp_path: Path, *, boot_id: str = "boot-a") -> dict:
    root = tmp_path / "root"
    _write(root / "boot/firmware/config.txt", b"[all]\ndtoverlay=vc4-kms-v3d\n")
    _write(root / "proc/cmdline", b"root=LABEL=writable rootwait\n")
    _write(root / "proc/device-tree/model", b"Raspberry Pi 5 Model B\x00")
    _write(root / "proc/device-tree/system/linux,revision", bytes.fromhex("00d04170"))
    _write(root / "proc/sys/kernel/random/boot_id", (boot_id + "\n").encode())
    return inspect_boot(
        load_boot_profile(ROOT / "boot/raspberry-pi-5-noble-arm64.json", REGISTRY),
        root=root,
        kernel_release="6.8.0-raspi",
        kernel_version="#1",
        architecture="aarch64",
    )


def _hardware(*, boot_id: str = "boot-a") -> dict:
    return inspect_hardware(
        load_manifest(ROOT / "hardware/shared-hardware-role-manifest.json", REGISTRY),
        [],
        profile="real",
        boot_id=boot_id,
        captured_monotonic_ns=1,
    )


class Inspector:
    def __init__(self, report):
        self.report = report

    def inspect(self):
        return self.report


def test_host_inspection_composes_independent_evidence_and_identity(tmp_path: Path):
    inspector = HostInspector(
        logical_target="drone",
        profile="real",
        hardware_inspector=Inspector(_hardware()),
        boot_inspector=Inspector(_boot(tmp_path)),
        registry=REGISTRY,
    )
    report = inspector.inspect()
    REGISTRY.validate("host-inspection", report)
    assert report["accepted"] is False
    assert report["hardware"]["accepted"] is False
    assert report["boot"]["accepted"] is True


def test_host_inspection_rejects_cross_boot_and_profile_mismatch(tmp_path: Path):
    with pytest.raises(ContractError, match="boot boundary"):
        HostInspector(
            logical_target="drone",
            profile="real",
            hardware_inspector=Inspector(_hardware(boot_id="boot-a")),
            boot_inspector=Inspector(_boot(tmp_path, boot_id="boot-b")),
            registry=REGISTRY,
        ).inspect()

    hardware = _hardware()
    hardware["profile"] = "opti_track"
    with pytest.raises(ContractError, match="profile differs"):
        HostInspector(
            logical_target="drone",
            profile="real",
            hardware_inspector=Inspector(hardware),
            boot_inspector=Inspector(_boot(tmp_path)),
            registry=REGISTRY,
        ).inspect()


def test_host_inspection_accepts_hil_without_physical_payloads(tmp_path: Path):
    hardware = inspect_hardware(
        load_manifest(ROOT / "hardware/shared-hardware-role-manifest.json", REGISTRY),
        [],
        profile="hil",
        boot_id="boot-a",
        captured_monotonic_ns=1,
    )
    report = HostInspector(
        logical_target="drone",
        profile="hil",
        hardware_inspector=Inspector(hardware),
        boot_inspector=Inspector(_boot(tmp_path)),
        registry=REGISTRY,
    ).inspect()
    assert report["accepted"] is True
    assert report["profile"] == "hil"
