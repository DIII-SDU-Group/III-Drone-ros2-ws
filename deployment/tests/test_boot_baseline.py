from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from iii_deployment.boot_baseline import (
    inspect_boot,
    load_boot_profile,
    validate_boot_profile,
)
from iii_deployment.contracts import ContractError, ContractRegistry, content_identity

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ContractRegistry(ROOT / "schemas/v1")
PROFILE_PATH = ROOT / "boot/raspberry-pi-5-noble-arm64.json"


def _write(path: Path, raw: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(raw)
    path.chmod(mode)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    _write(
        root / "boot/firmware/config.txt",
        b"# Ubuntu stock defaults\n[all]\n"
        b"initramfs initrd.img followkernel\n"
        b"dtoverlay=vc4-kms-v3d\ninclude usercfg.txt\n"
        b"[pi4]\nover_voltage=6\n"
        b"[pi3+]\ndtoverlay=vc4-kms-v3d,cma-128\n",
    )
    _write(
        root / "boot/firmware/usercfg.txt",
        b"[pi5]\ndtparam=audio=on\n",
    )
    _write(
        root / "proc/cmdline",
        b"root=LABEL=writable rootwait quiet cryptkey=do-not-expose\n",
        mode=0o444,
    )
    _write(
        root / "proc/device-tree/model",
        b"Raspberry Pi 5 Model B Rev 1.0\x00",
        mode=0o444,
    )
    _write(
        root / "proc/device-tree/system/linux,revision",
        bytes.fromhex("00d04170"),
        mode=0o444,
    )
    _write(root / "proc/sys/kernel/random/boot_id", b"boot-fixture\n", mode=0o444)
    return root


def _profile() -> dict:
    return load_boot_profile(PROFILE_PATH, REGISTRY)


def test_stock_pi5_profile_schema_identity_and_effective_inspection(tmp_path: Path):
    profile = _profile()
    report = inspect_boot(
        profile,
        root=_root(tmp_path),
        kernel_release="6.8.0-1030-raspi",
        kernel_version="#33-Ubuntu SMP PREEMPT_DYNAMIC",
        architecture="aarch64",
    )
    REGISTRY.validate("boot-inspection", report)
    assert report["accepted"] is True
    assert report["drift"] == []
    assert report["firmware"]["model"].startswith("Raspberry Pi 5")
    assert report["firmware"]["revision_hex"] == "00d04170"
    assert any(
        item["key"] == "over_voltage" and item["active"] is False
        for item in report["firmware"]["directives"]
    )
    assert any(
        item["key"] == "initramfs"
        and item["value"] == "initrd.img followkernel"
        and item["active"] is True
        for item in report["firmware"]["directives"]
    )
    secret = next(
        item for item in report["command_line"]["tokens"] if item["key"] == "cryptkey"
    )
    assert secret == {"key": "cryptkey", "value": "<redacted>", "redacted": True}
    assert "do-not-expose" not in str(report)


def test_active_tuning_missing_rootwait_architecture_and_permissions_are_drift(
    tmp_path: Path,
):
    root = _root(tmp_path)
    _write(
        root / "boot/firmware/usercfg.txt",
        b"[pi5]\nforce_turbo=1\n",
    )
    _write(root / "proc/cmdline", b"root=LABEL=writable init=/bin/sh\n", mode=0o444)
    (root / "boot/firmware/config.txt").chmod(0o666)
    report = inspect_boot(
        _profile(),
        root=root,
        kernel_release="6.8.0",
        kernel_version="#1",
        architecture="x86_64",
    )
    assert report["accepted"] is False
    assert report["drift"] == sorted(report["drift"])
    joined = "\n".join(report["drift"])
    assert "force_turbo" in joined
    assert "rootwait" in joined
    assert "init=/bin/sh" in joined
    assert "writable outside root" in joined
    assert "architecture" in joined


def test_managed_setting_and_overlay_are_checked_without_rewriting_stock(
    tmp_path: Path,
):
    profile = deepcopy(_profile())
    profile["firmware"]["managed_settings"] = {"dtparam": "audio=off"}
    profile["firmware"]["managed_overlays"] = ["gpio-fan"]
    report = inspect_boot(
        profile,
        root=_root(tmp_path),
        kernel_release="6.8.0",
        kernel_version="#1",
        architecture="aarch64",
    )
    assert report["accepted"] is False
    assert "managed firmware setting dtparam differs" in report["drift"]
    assert "required device-tree overlays are absent: gpio-fan" in report["drift"]


def test_config_include_escape_cycle_and_symlink_fail_closed(tmp_path: Path):
    profile = _profile()
    root = _root(tmp_path)
    _write(root / "boot/firmware/config.txt", b"include ../outside.txt\n")
    report = inspect_boot(
        profile,
        root=root,
        kernel_release="6.8.0",
        kernel_version="#1",
        architecture="aarch64",
    )
    assert any("unsafe" in item for item in report["drift"])

    _write(root / "boot/firmware/config.txt", b"include loop.txt\n")
    _write(root / "boot/firmware/loop.txt", b"include config.txt\n")
    report = inspect_boot(
        profile,
        root=root,
        kernel_release="6.8.0",
        kernel_version="#1",
        architecture="aarch64",
    )
    assert any("cycle" in item for item in report["drift"])

    (root / "boot/firmware/config.txt").unlink()
    (root / "boot/firmware/config.txt").symlink_to("usercfg.txt")
    report = inspect_boot(
        profile,
        root=root,
        kernel_release="6.8.0",
        kernel_version="#1",
        architecture="aarch64",
    )
    assert any("linked" in item for item in report["drift"])


def test_profile_rejects_identity_tampering_and_forbidden_managed_tuning():
    profile = deepcopy(_profile())
    profile["firmware"]["managed_settings"] = {"force_turbo": "1"}
    profile["profile_id"] = content_identity(
        {key: value for key, value in profile.items() if key != "profile_id"}
    )
    with pytest.raises(ContractError, match="forbidden tuning"):
        validate_boot_profile(profile, REGISTRY)

    profile = deepcopy(_profile())
    profile["profile_id"] = "0" * 64
    with pytest.raises(ContractError, match="identity"):
        validate_boot_profile(profile, REGISTRY)

    profile = deepcopy(_profile())
    profile["firmware"]["managed_settings"] = {"dtparam": "audio=off\nforce_turbo=1"}
    profile["profile_id"] = content_identity(
        {key: value for key, value in profile.items() if key != "profile_id"}
    )
    with pytest.raises(ContractError, match="boot-profile contract rejected"):
        validate_boot_profile(profile, REGISTRY)


def test_application_and_self_update_policies_cannot_mutate_boot() -> None:
    policy = json.loads((ROOT / "receiver-policy.json").read_text())
    required = {
        "/boot",
        "/etc/iii/boot-profile.json",
        "/etc/iii/boot-baseline.json",
    }
    assert required <= set(policy["normal_release_forbidden_paths"])
    assert required <= set(policy["self_update_forbidden_paths"])
    assert not any(
        path == "/boot" or path.startswith("/boot/")
        for path in policy["normal_release_mutable_paths"]
    )


def test_physical_sd_recovery_runbook_preserves_destructive_safety_gates() -> None:
    runbook = (ROOT.parent / "docs/raspberry-pi-boot-baseline.md").read_text()
    normalized = " ".join(runbook.split())
    for required in (
        "iii host image inspect",
        "iii host image write",
        "iii host provision check",
        "iii host provision apply",
        "iii host inspect",
        "sudo fsck.vfat -n",
        "sudo e2fsck -fn",
        "/dev/disk/by-id/",
        "typed physical-device confirmation",
        "--accept-data-loss",
        "fresh signed commissioning record",
    ):
        assert required in normalized
    assert "Never practice against the workstation system disk" in normalized
